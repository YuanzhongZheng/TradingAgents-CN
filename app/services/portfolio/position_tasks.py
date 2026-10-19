"""PortfolioService.PositionTaskMixin。"""
from app.services.portfolio.support import (
    asyncio,
    uuid,
    datetime,
    timedelta,
    tz,
    Dict,
    Any,
    Optional,
    ObjectId,
    get_mongo_db,
    _use_stock_engine,
    _use_stock_engine,
    PortfolioAnalysisStatus,
    PositionSnapshot,
    AIAnalysisResult,
    PositionResponse,
    PositionAnalysisRequest,
    PositionAnalysisReport,
    now_tz,
    logger,
    convert_market_code_to_name,
)

class PositionTaskMixin:

    def _get_currency_by_market(self, market: str) -> str:
        """根据市场获取货币"""
        currency_map = {
            "CN": "CNY",
            "HK": "HKD",
            "US": "USD"
        }
        return currency_map.get(market, "CNY")

    # ==================== 单股持仓分析 ====================

    async def get_position_by_id(self, user_id: str, position_id: str) -> Optional[PositionResponse]:
        """根据ID获取持仓"""
        try:
            obj_id = ObjectId(position_id)
        except Exception:
            return None

        position = await self.db[self.positions_collection].find_one(
            {"_id": obj_id, "user_id": user_id}
        )
        if position:
            return await self._enrich_position(position, "real")
        return None

    async def analyze_position(
        self,
        user_id: str,
        position_id: str,
        params: PositionAnalysisRequest
    ) -> PositionAnalysisReport:
        """单股持仓分析

        流程：
        1. 检查是否有缓存的单股分析报告（只读取缓存，不创建新任务）
        2. 如果有缓存，使用缓存的报告；如果没有缓存，使用空报告
        3. 将分析报告（或空报告）+ 持仓信息发给持仓分析专用LLM
        4. 生成个性化的持仓研究观察（非交易建议）
        """
        analysis_id = str(uuid.uuid4())

        # 获取持仓数据
        position = await self.get_position_by_id(user_id, position_id)
        if not position:
            report = PositionAnalysisReport(
                analysis_id=analysis_id,
                user_id=user_id,
                position_id=position_id,
                status=PortfolioAnalysisStatus.FAILED,
                error_message="持仓不存在"
            )
            return report

        # 构建持仓快照
        holding_days = None
        if position.buy_date:
            holding_days = (datetime.now() - datetime.fromisoformat(position.buy_date.replace('Z', '+00:00').split('+')[0])).days

        # 计算仓位占比（如果提供了资金总量）
        position_pct = None
        if params.total_capital and params.total_capital > 0 and position.market_value:
            position_pct = round((position.market_value / params.total_capital) * 100, 2)

        snapshot = PositionSnapshot(
            code=position.code,
            name=position.name,
            market=position.market,
            quantity=position.quantity,
            cost_price=position.cost_price,
            current_price=position.current_price,
            market_value=position.market_value,
            unrealized_pnl=position.unrealized_pnl,
            unrealized_pnl_pct=position.unrealized_pnl_pct,
            industry=position.industry,
            holding_days=holding_days,
            total_capital=params.total_capital,
            position_pct=position_pct
        )

        report = PositionAnalysisReport(
            analysis_id=analysis_id,
            user_id=user_id,
            position_id=position_id,
            position_snapshot=snapshot,
            status=PortfolioAnalysisStatus.PROCESSING
        )

        try:
            start_time = datetime.now()

            # 将市场代码转换为中文名称（用于缓存查询）
            market_name = convert_market_code_to_name(position.market, position.code)

            # 检查是否有缓存的单股分析报告（只读取缓存，不创建新任务）
            logger.info(f"📊 [持仓分析] 检查单股分析报告缓存 - {position.code} (市场: {position.market} -> {market_name})")
            stock_analysis_report = await self._get_cached_stock_analysis_report(
                stock_code=position.code,
                market=market_name,
                cache_hours=24  # 使用24小时内的缓存
            )

            # 如果没有缓存报告，创建空结构，直接进行持仓分析
            if not stock_analysis_report:
                logger.info(f"ℹ️ [持仓分析] 未找到缓存报告，直接进行持仓分析 - {position.code}")
                stock_analysis_report = {
                    "task_id": None,
                    "reports": {},
                    "decision": {},
                    "summary": "",
                    "recommendation": "",
                    "source": "none"
                }
            else:
                logger.info(f"✅ [持仓分析] 找到缓存报告，将作为参考 - {position.code}, 来源: {stock_analysis_report.get('source', 'unknown')}")

            # 结合持仓信息进行持仓分析
            logger.info(f"📊 [持仓分析] 开始持仓专用分析 - {position.code}")

            # 根据配置选择分析引擎（USE_STOCK_ENGINE=true 时使用工作流引擎v2.0）
            if _use_stock_engine():
                logger.info(f"🚀 [持仓分析] 使用工作流引擎v2.0进行分析")
                ai_result = await self._call_position_ai_analysis_workflow(
                    snapshot=snapshot,
                    params=params,
                    stock_analysis_report=stock_analysis_report,
                    user_id=user_id
                )
            else:
                logger.info(f"🤖 [持仓分析] 使用传统LLM分析")
                ai_result = await self._call_position_ai_analysis_v2(
                    snapshot=snapshot,
                    params=params,
                    stock_analysis_report=stock_analysis_report,
                    user_id=user_id
                )

            execution_time = (datetime.now() - start_time).total_seconds()

            report.ai_analysis = ai_result
            report.execution_time = execution_time
            report.status = PortfolioAnalysisStatus.COMPLETED
            # 保存单股分析报告的引用
            report.stock_analysis_task_id = stock_analysis_report.get("task_id")
            logger.info(f"✅ 单股持仓分析完成: {position.code}, 建议: {ai_result.action}")

        except Exception as e:
            logger.error(f"❌ 单股持仓分析失败: {e}", exc_info=True)
            report.status = PortfolioAnalysisStatus.FAILED
            report.error_message = str(e)

        # 保存报告
        report_dict = report.model_dump(by_alias=True, exclude={"id"})
        await self.db[self.position_analysis_collection].insert_one(report_dict)
        return report

    async def create_position_analysis_task(
        self,
        user_id: str,
        code: str,
        market: str,
        params: "PositionAnalysisByCodeRequest"
    ) -> Dict[str, Any]:
        """创建持仓分析任务（异步模式）

        立即返回任务信息，后台执行分析
        """
        import asyncio
        from app.models.portfolio import PositionAnalysisByCodeRequest

        analysis_id = str(uuid.uuid4())

        # 检查是否已有正在进行的分析
        existing = await self.db[self.position_analysis_collection].find_one({
            "user_id": user_id,
            "position_id": f"{code}_{market}",
            "status": {"$in": ["pending", "processing"]}
        })

        if existing:
            return {
                "analysis_id": existing.get("analysis_id"),
                "code": code,
                "market": market,
                "status": existing.get("status"),
                "message": "已有分析任务正在进行中",
                "created_at": existing.get("created_at").isoformat() if existing.get("created_at") else None
            }

        # 检查是否有最近完成的分析（30分钟内）
        # 🔥 使用 now_tz() 确保时区一致
        recent_cutoff = now_tz() - timedelta(minutes=30)
        recent_completed = await self.db[self.position_analysis_collection].find_one({
            "user_id": user_id,
            "position_id": f"{code}_{market}",
            "status": "completed",
            "created_at": {"$gte": recent_cutoff}
        }, sort=[("created_at", -1)])

        if recent_completed:
            return {
                "analysis_id": recent_completed.get("analysis_id"),
                "code": code,
                "market": market,
                "status": "completed",
                "message": "已有最近的分析报告，可直接查看",
                "created_at": recent_completed.get("created_at").isoformat() if recent_completed.get("created_at") else None
            }

        # 创建新的分析任务记录
        report = PositionAnalysisReport(
            analysis_id=analysis_id,
            user_id=user_id,
            position_id=f"{code}_{market}",
            status=PortfolioAnalysisStatus.PENDING
        )

        report_dict = report.model_dump(by_alias=True, exclude={"id"})
        await self.db[self.position_analysis_collection].insert_one(report_dict)

        # 在后台启动分析任务
        asyncio.create_task(self._execute_position_analysis_background(
            user_id=user_id,
            analysis_id=analysis_id,
            code=code,
            market=market,
            params=params
        ))

        return {
            "analysis_id": analysis_id,
            "code": code,
            "market": market,
            "status": "pending",
            "message": "分析任务已创建，预计需要2-5分钟",
            "created_at": report.created_at.isoformat()
        }

    async def _execute_position_analysis_background(
        self,
        user_id: str,
        analysis_id: str,
        code: str,
        market: str,
        params: "PositionAnalysisByCodeRequest"
    ):
        """后台执行持仓分析"""
        try:
            # 更新状态为处理中
            await self.db[self.position_analysis_collection].update_one(
                {"analysis_id": analysis_id},
                {"$set": {"status": "processing"}}
            )

            # 执行实际分析
            report = await self.analyze_position_by_code(user_id, code, market, params)

            # 更新分析结果
            update_data = {
                "status": report.status.value,
                "position_snapshot": report.position_snapshot.model_dump() if report.position_snapshot else None,
                "ai_analysis": report.ai_analysis.model_dump() if report.ai_analysis else None,
                "execution_time": report.execution_time,
                "error_message": report.error_message,
                "stock_analysis_task_id": report.stock_analysis_task_id
            }

            await self.db[self.position_analysis_collection].update_one(
                {"analysis_id": analysis_id},
                {"$set": update_data}
            )

            logger.info(f"✅ 后台持仓分析完成: {code}, analysis_id={analysis_id}")

        except Exception as e:
            logger.error(f"❌ 后台持仓分析失败: {code}, error={e}", exc_info=True)
            await self.db[self.position_analysis_collection].update_one(
                {"analysis_id": analysis_id},
                {"$set": {"status": "failed", "error_message": str(e)}}
            )

    async def get_position_analysis_by_id(
        self,
        user_id: str,
        analysis_id: str
    ) -> Optional[Dict[str, Any]]:
        """根据分析ID获取分析报告"""
        report = await self.db[self.position_analysis_collection].find_one({
            "user_id": user_id,
            "analysis_id": analysis_id
        })

        if not report:
            return None

        return self._format_position_analysis_report(report)

    async def get_latest_position_analysis(
        self,
        user_id: str,
        code: str,
        market: str,
        position_type: str = "real"
    ) -> Optional[Dict[str, Any]]:
        """获取某只股票的最新分析报告
        
        Args:
            user_id: 用户ID
            code: 股票代码
            market: 市场类型
            position_type: 持仓类型 (real/simulated)
        """
        # 🔥 根据 position_type 查询对应的分析报告
        query = {
            "user_id": user_id,
            "position_id": f"{code}_{market}",
            "position_type": position_type  # 添加 position_type 筛选条件
        }
        
        report = await self.db[self.position_analysis_collection].find_one(
            query,
            sort=[("created_at", -1)]
        )

        if not report:
            return None

        return self._format_position_analysis_report(report)

    def _format_position_analysis_report(self, report: Dict) -> Dict[str, Any]:
        """格式化分析报告为响应格式"""
        snapshot = report.get("position_snapshot") or {}
        ai_analysis = report.get("ai_analysis") or {}
        price_targets = ai_analysis.get("price_targets") or {}
        
        # 处理action_reason：如果是JSON格式，转换为Markdown
        action_reason_raw = ai_analysis.get("action_reason")
        action_reason_formatted = action_reason_raw
        
        if action_reason_raw:
            logger.info(f"📊 [格式化报告] 原始action_reason类型: {type(action_reason_raw)}, 长度: {len(str(action_reason_raw)) if action_reason_raw else 0}")
            
            import json
            advice_json = None
            
            # 如果是字符串，尝试解析JSON
            if isinstance(action_reason_raw, str):
                action_reason_str = action_reason_raw.strip()
                logger.info(f"📊 [格式化报告] action_reason前100字符: {action_reason_str[:100]}")
                
                # 检查是否包含JSON代码块或JSON格式
                if "```json" in action_reason_str or (action_reason_str.startswith("{") and ("analysis_summary" in action_reason_str or "neutral_operation" in action_reason_str)):
                    try:
                        # 提取JSON
                        if "```json" in action_reason_str:
                            json_start = action_reason_str.find("```json") + 7
                            json_end = action_reason_str.find("```", json_start)
                            if json_end > json_start:
                                json_str = action_reason_str[json_start:json_end].strip()
                                logger.info(f"📊 [格式化报告] 从代码块提取JSON，长度: {len(json_str)}")
                                advice_json = json.loads(json_str)
                        elif action_reason_str.strip().startswith("{"):
                            logger.info(f"📊 [格式化报告] 直接解析JSON格式")
                            advice_json = json.loads(action_reason_str)
                        
                        if advice_json:
                            logger.info(f"📊 [格式化报告] JSON解析成功，字段: {list(advice_json.keys())}")
                            action_reason_formatted = self._convert_action_advice_json_to_markdown(advice_json)
                            logger.info(f"📊 [格式化报告] JSON转换为Markdown成功，长度: {len(action_reason_formatted)}")
                        else:
                            logger.warning(f"⚠️ [格式化报告] JSON解析后为空，使用原始值")
                    except Exception as e:
                        logger.warning(f"⚠️ [格式化报告] JSON解析失败，使用原始值: {e}", exc_info=True)
            elif isinstance(action_reason_raw, dict):
                # 如果已经是字典格式，直接转换
                logger.info(f"📊 [格式化报告] action_reason是字典格式，字段: {list(action_reason_raw.keys())}")
                action_reason_formatted = self._convert_action_advice_json_to_markdown(action_reason_raw)
                logger.info(f"📊 [格式化报告] 字典转换为Markdown成功，长度: {len(action_reason_formatted)}")
        
        return {
            "analysis_id": report.get("analysis_id"),
            "position_id": report.get("position_id"),
            "code": snapshot.get("code") or report.get("position_id", "").split("_")[0],
            "name": snapshot.get("name"),
            "market": snapshot.get("market") or report.get("position_id", "").split("_")[-1],
            "status": report.get("status"),
            "action": ai_analysis.get("action"),
            "action_reason": action_reason_formatted,
            "confidence": ai_analysis.get("confidence"),
            "price_targets": price_targets,
            "risk_assessment": ai_analysis.get("risk_assessment"),
            "opportunity_assessment": ai_analysis.get("opportunity_assessment"),
            "detailed_analysis": ai_analysis.get("detailed_analysis"),
            "execution_time": report.get("execution_time"),
            "error_message": report.get("error_message"),
            "created_at": report.get("created_at").isoformat() if report.get("created_at") else None,
            "summary": {
                "quantity": snapshot.get("quantity"),
                "cost_price": snapshot.get("cost_price"),
                "current_price": snapshot.get("current_price"),
                "market_value": snapshot.get("market_value"),
                "unrealized_pnl": snapshot.get("unrealized_pnl"),
                "unrealized_pnl_pct": snapshot.get("unrealized_pnl_pct"),
                "holding_days": snapshot.get("holding_days"),
                "position_pct": snapshot.get("position_pct"),
            }
        }

    async def analyze_position_by_code(
        self,
        user_id: str,
        code: str,
        market: str,
        params: "PositionAnalysisByCodeRequest"
    ) -> PositionAnalysisReport:
        """按股票代码分析持仓 - 汇总同一股票的所有建仓记录

        这个方法会：
        1. 查询该股票的所有持仓记录
        2. 汇总计算总数量、平均成本价、总市值等
        3. 作为一个整体进行分析
        """
        from app.models.portfolio import PositionAnalysisByCodeRequest

        analysis_id = str(uuid.uuid4())

        # 🔥 根据 position_type 选择查询用户持仓还是模拟持仓
        position_type = params.position_type if hasattr(params, 'position_type') else "real"
        if position_type == "simulated":
            # 查询模拟持仓
            collection = self.paper_positions_collection
            logger.info(f"📊 [持仓分析] 查询模拟持仓: {code} ({market})")
        else:
            # 查询用户持仓
            collection = self.positions_collection
            logger.info(f"📊 [持仓分析] 查询用户持仓: {code} ({market})")

        # 查询该股票的所有持仓记录
        storage_market = "CN" if market == "ETF" else market
        positions = await self.db[collection].find({
            "user_id": user_id,
            "code": code,
            "market": storage_market
        }).to_list(None)

        if not positions:
            report = PositionAnalysisReport(
                analysis_id=analysis_id,
                user_id=user_id,
                position_id=f"{code}_{storage_market}",  # 使用股票代码作为标识
                status=PortfolioAnalysisStatus.FAILED,
                error_message=f"未找到 {code} 的持仓记录"
            )
            return report

        # 汇总计算
        total_quantity = sum(p.get("quantity", 0) for p in positions)
        # 🔥 根据持仓类型选择成本价字段：模拟持仓使用 avg_cost，用户持仓使用 cost_price
        if position_type == "simulated":
            total_cost = sum(p.get("quantity", 0) * p.get("avg_cost", 0) for p in positions)
        else:
            total_cost = sum(p.get("quantity", 0) * p.get("cost_price", 0) for p in positions)
        avg_cost_price = total_cost / total_quantity if total_quantity > 0 else 0

        # 获取当前价格
        current_price = await self._get_stock_price(code, storage_market)
        # 🔥 修复：确保 market_value、unrealized_pnl 和 unrealized_pnl_pct 不为 None（模拟持仓分析时可能为 None）
        if current_price and current_price > 0:
            market_value = current_price * total_quantity
            unrealized_pnl = market_value - total_cost
            unrealized_pnl_pct = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0
        else:
            market_value = 0.0
            unrealized_pnl = 0.0
            unrealized_pnl_pct = 0.0

        # 获取股票名称和行业
        name = positions[0].get("name") or await self._get_stock_name(code, storage_market)
        industry = positions[0].get("industry") or await self._get_stock_industry(code, storage_market)

        # 计算持仓天数（使用最早的建仓日期）
        holding_days = None
        earliest_date = None
        for p in positions:
            buy_date = p.get("buy_date")
            if buy_date:
                if isinstance(buy_date, str):
                    buy_date = datetime.fromisoformat(buy_date.replace('Z', '+00:00').split('+')[0])
                if earliest_date is None or buy_date < earliest_date:
                    earliest_date = buy_date
        if earliest_date:
            holding_days = (datetime.now() - earliest_date).days

        # 计算仓位占比
        position_pct = None
        if params.total_capital and params.total_capital > 0 and market_value:
            position_pct = round((market_value / params.total_capital) * 100, 2)

        # 构建持仓快照（汇总后的）
        snapshot = PositionSnapshot(
            code=code,
            name=name,
            market=storage_market,
            quantity=total_quantity,
            cost_price=round(avg_cost_price, 4),
            current_price=current_price if current_price else 0.0,
            market_value=round(market_value, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            unrealized_pnl_pct=round(unrealized_pnl_pct, 2),
            industry=industry,
            holding_days=holding_days,
            total_capital=params.total_capital,
            position_pct=position_pct
        )

        # 创建分析参数（转换为 PositionAnalysisRequest）
        analysis_params = PositionAnalysisRequest(
            research_depth=params.research_depth,
            include_add_position=params.include_add_position,
            target_profit_pct=params.target_profit_pct,
            total_capital=params.total_capital,
            max_position_pct=params.max_position_pct,
            max_loss_pct=params.max_loss_pct
        )

        report = PositionAnalysisReport(
            analysis_id=analysis_id,
            user_id=user_id,
            position_id=f"{code}_{storage_market}",  # 使用股票代码作为标识
            position_snapshot=snapshot,
            status=PortfolioAnalysisStatus.PROCESSING
        )

        try:
            start_time = datetime.now()

            # 将市场代码转换为中文名称（单股分析服务需要）
            market_name = convert_market_code_to_name(market, code)

            # 检查是否有缓存的单股分析报告（只读取缓存，不创建新任务）
            stock_analysis_report = None
            if params.use_stock_analysis:
                logger.info(f"📊 [汇总持仓分析] 检查单股分析报告缓存 - {code} (市场: {market} -> {market_name}, 共{len(positions)}条记录)")
                stock_analysis_report = await self._get_cached_stock_analysis_report(
                    stock_code=code,
                    market=market_name,
                    cache_hours=24  # 使用24小时内的缓存
                )

                # 如果没有缓存报告，记录日志
                if not stock_analysis_report:
                    logger.info(f"ℹ️ [汇总持仓分析] 未找到缓存报告，将直接进行持仓分析 - {code}")
                else:
                    logger.info(f"✅ [汇总持仓分析] 找到缓存报告，将作为参考 - {code}, 来源: {stock_analysis_report.get('source', 'unknown')}")
            else:
                logger.info(f"⏭️ [汇总持仓分析] 用户选择不使用单股分析报告 - {code}")

            # 如果没有单股分析报告，创建一个空的报告结构，避免后续代码报错
            if not stock_analysis_report:
                stock_analysis_report = {
                    "task_id": None,
                    "reports": {},
                    "decision": {},
                    "summary": "",
                    "recommendation": "",
                    "source": "none"
                }

            # 第二阶段：结合持仓信息进行持仓分析
            logger.info(f"📊 [汇总持仓分析] 第二阶段：持仓专用分析 - {code}")

            # 根据配置选择分析引擎（USE_STOCK_ENGINE=true 时使用工作流引擎v2.0）
            if _use_stock_engine():
                logger.info(f"🚀 [汇总持仓分析] 使用工作流引擎v2.0进行分析")
                ai_result = await self._call_position_ai_analysis_workflow(
                    snapshot=snapshot,
                    params=analysis_params,
                    stock_analysis_report=stock_analysis_report,
                    user_id=user_id
                )
            else:
                logger.info(f"🤖 [汇总持仓分析] 使用传统LLM分析")
                ai_result = await self._call_position_ai_analysis_v2(
                    snapshot=snapshot,
                    params=analysis_params,
                    stock_analysis_report=stock_analysis_report,
                    user_id=user_id
                )

            execution_time = (datetime.now() - start_time).total_seconds()

            report.ai_analysis = ai_result
            report.execution_time = execution_time
            report.status = PortfolioAnalysisStatus.COMPLETED
            report.stock_analysis_task_id = stock_analysis_report.get("task_id")
            logger.info(f"✅ 汇总持仓分析完成: {code}, 建议: {ai_result.action}, 共{len(positions)}条记录")

        except Exception as e:
            logger.error(f"❌ 汇总持仓分析失败: {e}", exc_info=True)
            report.status = PortfolioAnalysisStatus.FAILED
            report.error_message = str(e)

        # 保存报告
        report_dict = report.model_dump(by_alias=True, exclude={"id"})
        # 🔥 添加 position_type 字段到报告记录中，用于后续查询时区分模拟持仓和用户持仓
        report_dict["position_type"] = position_type
        await self.db[self.position_analysis_collection].insert_one(report_dict)
        return report

    async def analyze_position_by_code_v2(
        self,
        user_id: str,
        code: str,
        market: str,
        params: "PositionAnalysisByCodeRequest"
    ) -> PositionAnalysisReport:
        """按股票代码分析持仓 v2.0 - 使用统一任务引擎

        优势:
        - 支持多引擎切换（workflow/legacy/llm）
        - 统一的任务管理和进度跟踪
        - 自动选择最佳引擎
        - 任务可查询、可取消

        Args:
            user_id: 用户ID
            code: 股票代码
            market: 市场类型（CN/HK/US）
            params: 分析参数

        Returns:
            持仓分析报告
        """
        from app.services.task_analysis_service import get_task_analysis_service
        from app.models.analysis import AnalysisTaskType
        from app.models.portfolio import PositionAnalysisByCodeRequest
        from app.models.user import PyObjectId

        logger.info(f"🚀 [持仓分析v2.0] 使用统一任务引擎分析: {code} (市场: {market})")

        # 查询该股票的所有持仓记录（用于构建快照）
        positions = await self.db[self.positions_collection].find({
            "user_id": user_id,
            "code": code,
            "market": market
        }).to_list(None)

        if not positions:
            # 如果没有持仓记录，返回失败报告
            analysis_id = str(uuid.uuid4())
            report = PositionAnalysisReport(
                analysis_id=analysis_id,
                user_id=user_id,
                position_id=f"{code}_{market}",
                status=PortfolioAnalysisStatus.FAILED,
                error_message=f"未找到 {code} 的持仓记录"
            )
            return report

        # 汇总计算持仓数据
        total_quantity = sum(p.get("quantity", 0) for p in positions)
        total_cost = sum(p.get("quantity", 0) * p.get("cost_price", 0) for p in positions)
        avg_cost_price = total_cost / total_quantity if total_quantity > 0 else 0

        # 获取当前价格
        current_price = await self._get_stock_price(code, market)
        market_value = current_price * total_quantity if current_price else None
        unrealized_pnl = (market_value - total_cost) if market_value else None
        unrealized_pnl_pct = (unrealized_pnl / total_cost * 100) if unrealized_pnl and total_cost > 0 else None

        # 获取股票名称和行业
        name = positions[0].get("name") or await self._get_stock_name(code, market)
        industry = positions[0].get("industry") or await self._get_stock_industry(code, market)

        # 计算持仓天数
        holding_days = None
        earliest_date = None
        for p in positions:
            buy_date = p.get("buy_date")
            if buy_date:
                if isinstance(buy_date, str):
                    buy_date = datetime.fromisoformat(buy_date.replace('Z', '+00:00').split('+')[0])
                if earliest_date is None or buy_date < earliest_date:
                    earliest_date = buy_date
        if earliest_date:
            holding_days = (datetime.now() - earliest_date).days

        # 计算仓位占比
        position_pct = None
        if params.total_capital and params.total_capital > 0 and market_value:
            position_pct = round((market_value / params.total_capital) * 100, 2)

        # 构建持仓快照
        snapshot = PositionSnapshot(
            code=code,
            name=name,
            market=market,
            quantity=total_quantity,
            cost_price=round(avg_cost_price, 4),
            current_price=current_price,
            market_value=round(market_value, 2) if market_value else None,
            unrealized_pnl=round(unrealized_pnl, 2) if unrealized_pnl else None,
            unrealized_pnl_pct=round(unrealized_pnl_pct, 2) if unrealized_pnl_pct else None,
            industry=industry,
            holding_days=holding_days,
            total_capital=params.total_capital,
            position_pct=position_pct
        )

        # 准备任务参数
        task_params = {
            "code": code,
            "market": market,
            "snapshot": snapshot.model_dump(),
            "research_depth": params.research_depth,
            "include_add_position": params.include_add_position,
            "target_profit_pct": params.target_profit_pct,
            "total_capital": params.total_capital,
            "max_position_pct": params.max_position_pct,
            "max_loss_pct": params.max_loss_pct,
            "use_stock_analysis": params.use_stock_analysis
        }

        # 使用统一任务引擎
        task_service = get_task_analysis_service()

        try:
            # 创建并执行任务
            task = await task_service.create_and_execute_task(
                user_id=PyObjectId(user_id),
                task_type=AnalysisTaskType.POSITION_ANALYSIS,
                task_params=task_params,
                engine_type=getattr(params, "engine_type", "auto"),  # 支持引擎选择
                preference_type=getattr(params, "preference_type", "neutral")
            )

            # 将任务结果转换为 PositionAnalysisReport
            report = PositionAnalysisReport(
                analysis_id=task.task_id,
                user_id=user_id,
                position_id=f"{code}_{market}",
                position_snapshot=snapshot,
                status=PortfolioAnalysisStatus.COMPLETED if task.status == "completed" else PortfolioAnalysisStatus.FAILED,
                execution_time=task.execution_time,
                error_message=task.error_message
            )

            # 从任务结果中提取 AI 分析
            if task.result and "ai_analysis" in task.result:
                report.ai_analysis = AIAnalysisResult(**task.result["ai_analysis"])

            logger.info(f"✅ [持仓分析v2.0] 分析完成: {code}, 任务ID: {task.task_id}")

            # 保存报告
            report_dict = report.model_dump(by_alias=True, exclude={"id"})
            await self.db[self.position_analysis_collection].insert_one(report_dict)

            return report

        except Exception as e:
            logger.error(f"❌ [持仓分析v2.0] 分析失败: {code} - {e}", exc_info=True)

            # 返回失败报告
            report = PositionAnalysisReport(
                analysis_id=str(uuid.uuid4()),
                user_id=user_id,
                position_id=f"{code}_{market}",
                position_snapshot=snapshot,
                status=PortfolioAnalysisStatus.FAILED,
                error_message=str(e)
            )

            # 保存报告
            report_dict = report.model_dump(by_alias=True, exclude={"id"})
            await self.db[self.position_analysis_collection].insert_one(report_dict)

            return report

    async def check_stock_analysis_cache(
        self,
        stock_code: str,
        market: str = "A股"
    ) -> Dict[str, Any]:
        """检查单股分析报告缓存状态
        
        Args:
            stock_code: 股票代码
            market: 市场类型（A股/港股/美股）
            
        Returns:
            包含缓存状态的字典：
            {
                "has_cache": bool,  # 是否有缓存
                "cache_age_minutes": int,  # 缓存年龄（分钟）
                "cache_age_hours": float,  # 缓存年龄（小时）
                "source": str  # 缓存来源（"engine" 或 "legacy"）
            }
        """
        from app.core.database import get_mongo_db
        
        db = get_mongo_db()
        
        # 检查24小时内的报告（前端提示用户的标准）
        cache_hours = 24
        # 🔥 使用 now_tz() 确保时区一致
        recent_cutoff = now_tz() - timedelta(hours=cache_hours)
        
        # 检查旧版引擎的缓存（analysis_reports 集合）
        # 这是最常用的缓存位置，无论是否启用新引擎都会使用
        existing_report = await db.analysis_reports.find_one({
            "stock_symbol": stock_code,
            "created_at": {"$gte": recent_cutoff},
            "status": "completed"
        }, sort=[("created_at", -1)])
        
        if existing_report:
            created_at = existing_report.get("created_at", datetime.now())
            if isinstance(created_at, str):
                from dateutil.parser import parse
                created_at = parse(created_at)
            elif created_at is None:
                created_at = now_tz()
            
            # 🔥 确保时区一致：MongoDB 存储的是 UTC 时间
            # 如果从 MongoDB 读取的是 naive datetime，应该假定为 UTC
            # 注意：tz 和 now_tz 已在文件顶部导入
            current_time = now_tz()
            
            if created_at:
                # 如果 created_at 是 naive datetime，假定为 UTC（因为 MongoDB 存储的是 UTC）
                if isinstance(created_at, datetime) and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=tz.utc)
                # 转换为配置时区以便计算时间差
                if created_at.tzinfo:
                    created_at = created_at.astimezone(current_time.tzinfo)
            else:
                created_at = current_time
            
            report_age = current_time - created_at
            age_minutes = int(report_age.total_seconds() / 60)
            age_hours = report_age.total_seconds() / 3600
            
            logger.info(f"✅ [检查缓存] 找到旧版引擎缓存: {stock_code}, 年龄: {age_minutes}分钟 (created_at: {created_at}, current_time: {current_time})")
            return {
                "has_cache": True,
                "cache_age_minutes": age_minutes,
                "cache_age_hours": round(age_hours, 2),
                "source": "legacy"
            }
        
        # 没有找到缓存
        logger.info(f"❌ [检查缓存] 未找到缓存: {stock_code}")
        return {
            "has_cache": False,
            "cache_age_minutes": None,
            "cache_age_hours": None,
            "source": None
        }

    async def _get_cached_stock_analysis_report(
        self,
        stock_code: str,
        market: str = "A股",
        cache_hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        """只从缓存中获取股票分析报告（不创建新任务）
        
        仅用于持仓分析场景，只读取已有缓存，不会创建新的单股分析任务
        
        Args:
            stock_code: 股票代码
            market: 市场类型（A股/港股/美股）
            cache_hours: 缓存有效期（小时）
            
        Returns:
            分析报告字典，如果没有缓存则返回 None
        """
        from app.core.database import get_mongo_db
        
        db = get_mongo_db()
        # 🔥 使用 now_tz() 确保时区一致
        recent_cutoff = now_tz() - timedelta(hours=cache_hours)
        
        # 检查是否有已完成的分析报告（任意用户）
        existing_report = await db.analysis_reports.find_one({
            "stock_symbol": stock_code,
            "created_at": {"$gte": recent_cutoff},
            "status": "completed"
        }, sort=[("created_at", -1)])
        
        if existing_report:
            created_at = existing_report.get("created_at", datetime.now())
            if isinstance(created_at, str):
                from dateutil.parser import parse
                created_at = parse(created_at)
            
            # 🔥 确保时区一致：MongoDB 存储的是 UTC 时间
            # 如果从 MongoDB 读取的是 naive datetime，应该假定为 UTC
            # 注意：tz 和 now_tz 已在文件顶部导入
            current_time = now_tz()
            
            if created_at:
                # 如果 created_at 是 naive datetime，假定为 UTC（因为 MongoDB 存储的是 UTC）
                if isinstance(created_at, datetime) and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=tz.utc)
                # 转换为配置时区以便计算时间差
                if created_at.tzinfo:
                    created_at = created_at.astimezone(current_time.tzinfo)
            else:
                created_at = current_time
            
            report_age = current_time - created_at
            age_minutes = int(report_age.total_seconds() / 60)
            age_hours = report_age.total_seconds() / 3600
            
            logger.info(f"📚 [持仓分析] 从缓存读取单股分析报告: {stock_code}, "
                       f"task_id={existing_report.get('task_id')}, "
                       f"报告时间: {age_minutes}分钟前 (created_at: {created_at}, current_time: {current_time})")
            return {
                "task_id": existing_report.get("task_id"),
                "reports": existing_report.get("reports", {}),
                "decision": existing_report.get("decision", {}),
                "summary": existing_report.get("summary", ""),
                "recommendation": existing_report.get("recommendation", ""),
                "source": "cached",
                "cache_age_minutes": age_minutes,
                "cache_age_hours": round(age_hours, 2)
            }
        
        logger.info(f"❌ [持仓分析] 未找到缓存的单股分析报告: {stock_code}")
        return None

    async def _get_stock_analysis_via_engine(
        self,
        stock_code: str,
        market: str = "A股"
    ) -> Optional[Dict[str, Any]]:
        """使用新版 StockAnalysisEngine 获取分析报告 — 已停用

        v3.0 起旧版 StockAnalysisEngine 已停用（死代码，无调用者）。
        详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md

        Raises:
            RuntimeError: 始终抛出（方法已停用）
        """
        raise RuntimeError(
            "[已停用] PortfolioService._get_stock_analysis_via_engine 已停用，"
            "请使用 analyze_position_by_code_v2 或 _get_cached_stock_analysis_report。"
            "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
        )

    async def _get_stock_analysis_via_legacy(
        self,
        user_id: str,
        stock_code: str,
        market: str = "A股",
        skip_if_not_exists: bool = False
    ) -> Optional[Dict[str, Any]]:
        """使用旧版 simple_analysis_service 获取分析报告（降级方案）— 已停用

        v3.0 起旧版 legacy 引擎已停用（死代码，无调用者）。
        详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md

        Args:
            user_id: 用户ID
            stock_code: 股票代码
            market: 市场类型
            skip_if_not_exists: 如果没有缓存报告，是否跳过（不创建新任务）

        Returns:
            分析报告字典，如果 skip_if_not_exists=True 且没有缓存则返回 None

        Raises:
            RuntimeError: 始终抛出（方法已停用）
        """
        raise RuntimeError(
            "[已停用] PortfolioService._get_stock_analysis_via_legacy 已停用，"
            "请使用 analyze_position_by_code_v2 或 _get_cached_stock_analysis_report。"
            "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
        )
