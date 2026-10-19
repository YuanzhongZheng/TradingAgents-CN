"""PortfolioService.PositionResultMixin。"""
from app.services.portfolio.support import (
    re,
    Dict,
    Any,
    Optional,
    List,
    _format_user_friendly_error,
    PositionAction,
    PositionSnapshot,
    PositionAnalysisRequest,
    PositionAnalysisResult,
    PriceTarget,
    PositionRiskMetrics,
    PositionUserView,
    sanitize_position_analysis_payload,
    logger,
)

class PositionResultMixin:

    def _parse_position_ai_response_v2(
        self,
        content: str,
        snapshot: PositionSnapshot
    ) -> PositionAnalysisResult:
        """解析持仓AI响应 - 方案2 (增强JSON解析)"""
        import re
        import json

        # 尝试提取JSON
        json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))

                # 解析研究结论倾向（兼容旧版操作建议术语）
                action_str = data.get("action", "中性")
                action = PositionAction.HOLD
                if any(k in action_str for k in ["乐观", "加仓", "买入"]):
                    action = PositionAction.ADD
                elif any(k in action_str for k in ["审慎", "减仓", "卖出", "清仓"]):
                    action = PositionAction.REDUCE
                elif "中性" in action_str or "持有" in action_str:
                    action = PositionAction.HOLD

                # 构建价格目标
                price_targets = PriceTarget(
                    stop_loss_price=float(data.get("stop_loss_price", 0)) if data.get("stop_loss_price") else None,
                    take_profit_price=float(data.get("take_profit_price", 0)) if data.get("take_profit_price") else None,
                    breakeven_price=snapshot.cost_price
                )

                # 计算百分比
                if price_targets.stop_loss_price and snapshot.cost_price > 0:
                    price_targets.stop_loss_pct = round((price_targets.stop_loss_price - snapshot.cost_price) / snapshot.cost_price * 100, 2)
                if price_targets.take_profit_price and snapshot.cost_price > 0:
                    price_targets.take_profit_pct = round((price_targets.take_profit_price - snapshot.cost_price) / snapshot.cost_price * 100, 2)

                # 解析建议数量和金额
                suggested_quantity = None
                suggested_amount = None
                if data.get("suggested_quantity"):
                    try:
                        suggested_quantity = int(data["suggested_quantity"])
                    except (ValueError, TypeError):
                        pass
                if data.get("suggested_amount"):
                    try:
                        suggested_amount = float(data["suggested_amount"])
                    except (ValueError, TypeError):
                        pass

                # 构建风险指标（如果提供了资金总量）
                risk_metrics = None
                if snapshot.total_capital and snapshot.total_capital > 0:
                    risk_metrics = self._calculate_risk_metrics(snapshot, price_targets)

                return PositionAnalysisResult(
                    action=action,
                    action_reason=data.get("action_reason", "基于综合分析给出建议"),
                    confidence=float(data.get("confidence", 50)),
                    price_targets=price_targets,
                    risk_assessment=data.get("risk_assessment", "请关注市场风险"),
                    opportunity_assessment=data.get("opportunity_assessment", "请关注市场机会"),
                    detailed_analysis=data.get("detailed_analysis", content[:500]),
                    suggested_quantity=suggested_quantity,
                    suggested_amount=suggested_amount,
                    risk_metrics=risk_metrics
                )
            except json.JSONDecodeError:
                logger.warning("JSON解析失败，使用备用解析方法")

        # 备用解析方法（与原有方法类似）
        return self._parse_position_ai_response(content, snapshot)

    def _parse_position_ai_response(
        self,
        content: str,
        snapshot: PositionSnapshot
    ) -> PositionAnalysisResult:
        """解析单股持仓AI响应"""
        # 确定研究结论倾向（兼容旧版操作建议术语）
        action = PositionAction.HOLD
        if any(k in content for k in ["乐观", "加仓", "买入"]):
            action = PositionAction.ADD
        elif any(k in content for k in ["审慎", "减仓", "卖出", "清仓"]):
            action = PositionAction.REDUCE
        elif "中性" in content or "持有" in content:
            action = PositionAction.HOLD

        # 提取置信度
        confidence = 50.0
        import re
        conf_match = re.search(r'置信度[：:]\s*(\d+)', content)
        if conf_match:
            confidence = float(conf_match.group(1))

        # 提取风险关注/收益关注价格（兼容旧版止损/止盈表述）
        price_targets = PriceTarget()
        stop_loss_match = re.search(r'(风险关注价格|止损价)[：:]\s*(\d+\.?\d*)', content)
        if stop_loss_match:
            price_targets.stop_loss_price = float(stop_loss_match.group(2))
            if snapshot.cost_price > 0:
                price_targets.stop_loss_pct = round((price_targets.stop_loss_price - snapshot.cost_price) / snapshot.cost_price * 100, 2)

        take_profit_match = re.search(r'(收益关注价格|止盈价)[：:]\s*(\d+\.?\d*)', content)
        if take_profit_match:
            price_targets.take_profit_price = float(take_profit_match.group(2))
            if snapshot.cost_price > 0:
                price_targets.take_profit_pct = round((price_targets.take_profit_price - snapshot.cost_price) / snapshot.cost_price * 100, 2)

        price_targets.breakeven_price = snapshot.cost_price

        # 提取各部分内容
        reason = ""
        risk = ""
        opportunity = ""

        lines = content.split('\n')
        current_section = None
        for line in lines:
            line = line.strip()
            if "建议理由" in line or "理由" in line:
                current_section = "reason"
            elif "风险评估" in line:
                current_section = "risk"
            elif "机会评估" in line:
                current_section = "opportunity"
            elif line and current_section:
                if current_section == "reason" and len(reason) < 100:
                    reason += line + " "
                elif current_section == "risk" and len(risk) < 100:
                    risk += line + " "
                elif current_section == "opportunity" and len(opportunity) < 100:
                    opportunity += line + " "

        return PositionAnalysisResult(
            action=action,
            action_reason=reason.strip() or "基于综合分析给出建议",
            confidence=confidence,
            price_targets=price_targets,
            risk_assessment=risk.strip() or "请关注市场风险",
            opportunity_assessment=opportunity.strip() or "请关注市场机会",
            detailed_analysis=content[:500]
        )

    def _calculate_risk_metrics(
        self,
        snapshot: PositionSnapshot,
        price_targets: PriceTarget
    ) -> PositionRiskMetrics:
        """计算持仓风险指标"""
        if not snapshot.total_capital or snapshot.total_capital <= 0:
            return PositionRiskMetrics()

        total_capital = snapshot.total_capital
        market_value = snapshot.market_value or (snapshot.quantity * (snapshot.current_price or snapshot.cost_price))
        position_pct = (market_value / total_capital) * 100 if total_capital > 0 else 0

        # 计算最大可能亏损（使用风险关注价格或默认10%）
        stop_loss_pct = 10.0  # 默认10%
        if price_targets.stop_loss_pct:
            stop_loss_pct = abs(price_targets.stop_loss_pct)
        max_loss_amount = market_value * (stop_loss_pct / 100)
        max_loss_impact_pct = (max_loss_amount / total_capital) * 100

        # 计算可加仓金额（假设最大仓位30%）
        max_position_pct = 30.0
        max_position_value = total_capital * (max_position_pct / 100)
        available_add_amount = max(0, max_position_value - market_value)

        # 判断风险等级
        if position_pct > max_position_pct:
            risk_level = "critical"
            risk_summary = f"仓位{position_pct:.1f}%已超出上限{max_position_pct:.0f}%，建议减仓"
        elif position_pct > max_position_pct * 0.8:
            risk_level = "high"
            risk_summary = f"仓位{position_pct:.1f}%接近上限，谨慎加仓"
        elif position_pct > max_position_pct * 0.5:
            risk_level = "medium"
            risk_summary = f"仓位{position_pct:.1f}%适中，可适度加仓"
        else:
            risk_level = "low"
            risk_summary = f"仓位{position_pct:.1f}%较轻，有较大加仓空间"

        return PositionRiskMetrics(
            position_pct=round(position_pct, 2),
            position_value=round(market_value, 2),
            max_loss_amount=round(max_loss_amount, 2),
            max_loss_impact_pct=round(max_loss_impact_pct, 2),
            available_add_amount=round(available_add_amount, 2),
            risk_level=risk_level,
            risk_summary=risk_summary
        )

    async def get_position_analysis_history(
        self,
        user_id: str,
        position_id: str,
        page: int = 1,
        page_size: int = 10,
        position_type: str = "real"
    ) -> Dict[str, Any]:
        """获取单股分析历史
        
        Args:
            user_id: 用户ID
            position_id: 持仓ID（格式：code_market）
            page: 页码
            page_size: 每页大小
            position_type: 持仓类型 (real/simulated)
        """
        skip = (page - 1) * page_size

        # 🔥 根据 position_type 添加筛选条件
        query = {
            "user_id": user_id,
            "position_id": position_id,
            "position_type": position_type  # 添加 position_type 筛选条件
        }

        cursor = self.db[self.position_analysis_collection].find(query).sort("created_at", -1).skip(skip).limit(page_size)

        items = await cursor.to_list(None)
        total = await self.db[self.position_analysis_collection].count_documents(query)

        # 格式化数据：将 ai_analysis 中的所有字段提取到顶层，方便前端使用
        formatted_items = []
        for item in items:
            formatted_item = dict(item)
            
            # 🔥 提取 position_type 字段（用于区分模拟持仓和用户持仓）
            formatted_item["position_type"] = item.get("position_type", "real")
            
            # 提取 ai_analysis 中的所有字段到顶层
            ai_analysis = item.get("ai_analysis", {})
            if ai_analysis:
                # 提取 action（可能是枚举类型，需要转换为字符串）
                action_value = ai_analysis.get("action")
                if action_value:
                    # 如果是枚举类型，获取其值
                    if hasattr(action_value, "value"):
                        formatted_item["action"] = action_value.value
                    elif isinstance(action_value, str):
                        formatted_item["action"] = action_value
                    else:
                        formatted_item["action"] = str(action_value)
                else:
                    formatted_item["action"] = None
                
                # 提取其他字段
                formatted_item["confidence"] = ai_analysis.get("confidence")
                formatted_item["action_reason"] = ai_analysis.get("action_reason", "")
                formatted_item["risk_assessment"] = ai_analysis.get("risk_assessment", "")
                formatted_item["opportunity_assessment"] = ai_analysis.get("opportunity_assessment", "")
                formatted_item["detailed_analysis"] = ai_analysis.get("detailed_analysis", "")
                
                # 提取 price_targets（如果存在）
                price_targets = ai_analysis.get("price_targets")
                if price_targets:
                    # 如果是字典，直接使用；如果是对象，转换为字典
                    if isinstance(price_targets, dict):
                        formatted_item["price_targets"] = price_targets
                    elif hasattr(price_targets, "model_dump"):
                        formatted_item["price_targets"] = price_targets.model_dump()
                    elif hasattr(price_targets, "dict"):
                        formatted_item["price_targets"] = price_targets.dict()
                    else:
                        formatted_item["price_targets"] = None
                else:
                    formatted_item["price_targets"] = None
                
                # 提取其他可选字段
                formatted_item["suggested_quantity"] = ai_analysis.get("suggested_quantity")
                formatted_item["suggested_amount"] = ai_analysis.get("suggested_amount")
                formatted_item["risk_metrics"] = ai_analysis.get("risk_metrics")
                formatted_item["summary"] = ai_analysis.get("summary", "")
                formatted_item["recommendation"] = ai_analysis.get("recommendation", "")
            else:
                # 如果没有 ai_analysis，设置默认值
                formatted_item["action"] = None
                formatted_item["confidence"] = None
                formatted_item["action_reason"] = ""
                formatted_item["risk_assessment"] = ""
                formatted_item["opportunity_assessment"] = ""
                formatted_item["detailed_analysis"] = ""
                formatted_item["price_targets"] = None
            
            # 保留 position_snapshot 中的持仓信息，方便前端显示
            position_snapshot = item.get("position_snapshot")
            if position_snapshot:
                # 直接保留完整的 position_snapshot 对象
                formatted_item["position_snapshot"] = position_snapshot
                
                # 同时提取常用字段到顶层（用于列表显示）
                formatted_item["quantity"] = position_snapshot.get("quantity", 0)
                formatted_item["cost_price"] = position_snapshot.get("cost_price", 0.0)
                formatted_item["current_price"] = position_snapshot.get("current_price", 0.0)
                
                # 计算浮动盈亏（如果 position_snapshot 中没有）
                if "unrealized_pnl" not in position_snapshot:
                    if formatted_item.get("quantity") and formatted_item.get("cost_price") and formatted_item.get("current_price"):
                        quantity = formatted_item["quantity"]
                        cost_price = formatted_item["cost_price"]
                        current_price = formatted_item["current_price"]
                        floating_pnl = (current_price - cost_price) * quantity
                        floating_pnl_pct = ((current_price - cost_price) / cost_price * 100) if cost_price > 0 else 0.0
                        formatted_item["floating_pnl"] = floating_pnl
                        formatted_item["floating_pnl_pct"] = floating_pnl_pct
                else:
                    formatted_item["floating_pnl"] = position_snapshot.get("unrealized_pnl", 0.0)
                    formatted_item["floating_pnl_pct"] = position_snapshot.get("unrealized_pnl_pct", 0.0)
            
            formatted_items.append(formatted_item)

        return {
            "items": formatted_items,
            "total": total,
            "page": page,
            "page_size": page_size
        }

    # ==================== 工作流引擎集成 ====================

    async def _call_position_ai_analysis_workflow(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest,
        stock_analysis_report: Dict[str, Any],
        user_id: Optional[str] = None
    ) -> PositionAnalysisResult:
        """使用工作流引擎进行持仓分析

        使用持仓分析工作流，包含：
        1. 持仓技术面分析师 - 分析技术指标和走势
        2. 持仓基本面分析师 - 分析财务数据和估值
        3. 持仓风险评估师 - 评估风险并给出风险关注价格参考
        4. 持仓研究观察员 - 综合分析给出持仓研究观察
        """
        try:
            from core.api.workflow_api import WorkflowAPI

            # 准备单股分析报告数据（agents中使用的字段名是 stock_analysis_report）
            # 如果有缓存，需要设置 has_cache 标志，并提取 reports 子报告
            stock_analysis_report_for_workflow = {}
            if stock_analysis_report and stock_analysis_report.get("reports"):
                # 有缓存：设置标志并提取报告
                stock_analysis_report_for_workflow = {
                    "has_cache": True,
                    "reports": stock_analysis_report.get("reports", {}),
                    "task_id": stock_analysis_report.get("task_id"),
                    "source": stock_analysis_report.get("source", "cached")
                }
                logger.info(f"📊 [工作流持仓分析] 传递缓存报告到工作流: {snapshot.code}, reports字段: {list(stock_analysis_report_for_workflow.get('reports', {}).keys())}")
            else:
                # 无缓存：设置标志为空
                stock_analysis_report_for_workflow = {
                    "has_cache": False,
                    "reports": {},
                    "task_id": None,
                    "source": "none"
                }
                logger.info(f"📊 [工作流持仓分析] 无缓存报告，传递空结构到工作流: {snapshot.code}")
            
            # 准备工作流输入数据
            workflow_inputs = {
                # 持仓基础信息
                "position_info": {
                    "code": snapshot.code,
                    "name": snapshot.name,
                    "market": snapshot.market,
                    "industry": snapshot.industry,
                    "quantity": snapshot.quantity,
                    "cost_price": snapshot.cost_price,
                    "current_price": snapshot.current_price,
                    "market_value": snapshot.market_value,
                    "unrealized_pnl": snapshot.unrealized_pnl,
                    "unrealized_pnl_pct": snapshot.unrealized_pnl_pct,
                    "holding_days": snapshot.holding_days,
                    "position_pct": snapshot.position_pct,
                    "total_capital": snapshot.total_capital
                },

                # 分析目标股票：顶层 ticker 字段（工作流 agents 依赖 ticker 定位股票，缺失会报
                # "Missing required parameters: ticker"）；同时补齐 symbol/company_of_interest 兼容别名
                "ticker": snapshot.code,
                "symbol": snapshot.code,
                "company_of_interest": snapshot.code,

                # 分析参数
                "analysis_params": {
                    "research_depth": params.research_depth,
                    "include_add_position": params.include_add_position,
                    "target_profit_pct": params.target_profit_pct,
                    "total_capital": params.total_capital,
                    "max_position_pct": params.max_position_pct,
                    "max_loss_pct": params.max_loss_pct,
                    "risk_tolerance": params.risk_tolerance,
                    "investment_horizon": params.investment_horizon,
                    "analysis_focus": params.analysis_focus,
                    "position_type": params.position_type
                },

                # 单股分析报告（agents中使用的字段名是 stock_analysis_report）
                "stock_analysis_report": stock_analysis_report_for_workflow,

                # 用户偏好（用于选择提示词模板风格）
                "user_preference": self._get_user_analysis_preference(user_id, params.risk_tolerance),

                # 持仓类型标识
                "position_type": params.position_type
            }

            logger.info(f"🚀 [工作流持仓分析] 开始执行持仓分析工作流 - {snapshot.code}")

            # 🔥 从数据库获取模型配置并构建 legacy_config（与单股分析流程保持一致）
            legacy_config = {}
            try:
                from app.services.config_provider import provider as config_provider
                from app.services.unified_analysis_engine import UnifiedAnalysisEngine
                
                # 使用 config_provider 获取系统设置（包含 quick_analysis_model 和 deep_analysis_model）
                effective_settings = await config_provider.get_effective_system_settings()
                
                # 从 system_settings 中读取 quick_analysis_model 和 deep_analysis_model
                quick_analysis_model = effective_settings.get("quick_analysis_model")
                deep_analysis_model = effective_settings.get("deep_analysis_model")
                
                if quick_analysis_model and deep_analysis_model:
                    # 🔥 使用 UnifiedAnalysisEngine 的 _build_llm_config 方法获取完整的LLM配置
                    # 这样可以获取 temperature, timeout, max_tokens 等参数
                    engine = UnifiedAnalysisEngine()
                    full_config = await engine._build_llm_config(quick_analysis_model, deep_analysis_model)
                    
                    if full_config:
                        legacy_config.update(full_config)
                        logger.info(f"📊 [工作流持仓分析] 从数据库获取完整模型配置: quick={quick_analysis_model}, deep={deep_analysis_model}")
                        logger.info(f"  quick: temperature={legacy_config.get('quick_temperature')}, max_tokens={legacy_config.get('quick_max_tokens')}, timeout={legacy_config.get('quick_timeout')}")
                        logger.info(f"  deep: temperature={legacy_config.get('deep_temperature')}, max_tokens={legacy_config.get('deep_max_tokens')}, timeout={legacy_config.get('deep_timeout')}")
                    else:
                        # 如果获取失败，至少设置模型名称
                        legacy_config["quick_think_llm"] = quick_analysis_model
                        legacy_config["deep_think_llm"] = deep_analysis_model
                        legacy_config["llm_provider"] = "dashscope"
                        logger.warning(f"⚠️ [工作流持仓分析] 无法获取完整模型配置，仅设置模型名称")
                else:
                    logger.warning(f"⚠️ [工作流持仓分析] system_settings中未找到quick_analysis_model或deep_analysis_model，使用默认配置")
                    # 如果没有配置，使用默认值
                    legacy_config["quick_think_llm"] = quick_analysis_model or "qwen-turbo"
                    legacy_config["deep_think_llm"] = deep_analysis_model or "qwen-plus"
                    legacy_config["llm_provider"] = "dashscope"
            except Exception as e:
                logger.error(f"❌ [工作流持仓分析] 从数据库获取模型配置失败: {e}", exc_info=True)
                # 失败时使用默认配置
                legacy_config["quick_think_llm"] = "qwen-turbo"
                legacy_config["deep_think_llm"] = "qwen-plus"
                legacy_config["llm_provider"] = "dashscope"

            # 执行工作流（注意：WorkflowAPI.execute() 是同步方法，不需要 await）
            from core.workflow.default_workflow_provider import get_default_workflow_provider
            workflow_id = get_default_workflow_provider().get_active_workflow_id("position_analysis")
            workflow_api = WorkflowAPI()
            result = workflow_api.execute(
                workflow_id=workflow_id,
                inputs=workflow_inputs,
                legacy_config=legacy_config if legacy_config else None  # 🔥 传递模型配置
            )

            if not result.get("success"):
                error_msg = result.get("error", "工作流执行失败")
                logger.error(f"❌ [工作流持仓分析] 执行失败: {error_msg}")
                return PositionAnalysisResult(
                    action=PositionAction.HOLD,
                    action_reason=f"分析失败: {error_msg}"
                )

            # 解析工作流结果
            workflow_result = result.get("result", {})

            # 提取各个分析师的结果（处理可能是字典的情况）
            def extract_content(value):
                """安全提取内容，处理可能是字典的情况"""
                if isinstance(value, dict):
                    return value.get("content", value.get("error", str(value)))
                elif isinstance(value, str):
                    return value
                else:
                    return str(value) if value else ""

            technical_analysis = extract_content(workflow_result.get("technical_analysis", ""))
            fundamental_analysis = extract_content(workflow_result.get("fundamental_analysis", ""))
            risk_analysis = extract_content(workflow_result.get("risk_analysis", ""))
            
            # action_advice可能是字符串（JSON）或字典（包含content字段）
            action_advice_raw = workflow_result.get("action_advice", "")
            logger.info(f"📊 [工作流持仓分析] action_advice_raw类型: {type(action_advice_raw)}")
            
            # 使用extract_content提取content字段（无论原始格式是什么）
            action_advice = extract_content(action_advice_raw)
            logger.info(f"📊 [工作流持仓分析] action_advice提取后类型: {type(action_advice)}, 长度: {len(str(action_advice))}")

            logger.info(f"✅ [工作流持仓分析] 分析完成 - {snapshot.code}")

            # 初始化默认值
            from app.models.portfolio import PriceTarget
            action = PositionAction.HOLD
            action_reason = ""
            confidence = 0.0
            price_targets = PriceTarget()
            risk_assessment_text = risk_analysis  # 默认使用工作流的风险评估
            opportunity_assessment_text = ""
            
            # 从研究观察中提取研究结论和理由
            # 如果action_advice是JSON格式，先转换为Markdown
            import json
            advice_json = None
            logger.info(f"📊 [工作流持仓分析] 开始解析action_advice，类型: {type(action_advice)}, 长度: {len(action_advice) if action_advice else 0}")
            
            # 尝试解析JSON（action_advice应该是字符串）
            if action_advice and isinstance(action_advice, str) and ("```json" in action_advice or action_advice.strip().startswith("{")):
                try:
                    # 提取JSON
                    if "```json" in action_advice:
                        json_start = action_advice.find("```json") + 7
                        json_end = action_advice.find("```", json_start)
                        if json_end > json_start:
                            json_str = action_advice[json_start:json_end].strip()
                            logger.info(f"📊 [工作流持仓分析] 从代码块中提取JSON，长度: {len(json_str)}")
                            advice_json = json.loads(json_str)
                    elif action_advice.strip().startswith("{"):
                        logger.info(f"📊 [工作流持仓分析] 直接解析JSON格式")
                        advice_json = json.loads(action_advice)
                except Exception as e:
                    logger.warning(f"⚠️ [工作流持仓分析] JSON解析失败: {e}")
                    advice_json = None
            
            # 转换为Markdown格式并提取其他字段（无论action_advice是字典还是解析后的JSON字符串）
            if advice_json:
                logger.info(f"📊 [工作流持仓分析] JSON解析成功，字段: {list(advice_json.keys())}")
                action_reason = self._convert_action_advice_json_to_markdown(advice_json)
                logger.info(f"📊 [工作流持仓分析] JSON转换为Markdown成功，长度: {len(action_reason)}")
                # 从JSON中提取action
                action = self._extract_action_from_json(advice_json)
                logger.info(f"📊 [工作流持仓分析] 提取的action: {action}")
                # 提取其他字段
                # 置信度可能在多个位置
                confidence = None
                if "confidence" in advice_json:
                    confidence = advice_json.get("confidence")
                elif "neutral_operation_advice" in advice_json:
                    neutral = advice_json["neutral_operation_advice"]
                    if isinstance(neutral, dict):
                        confidence = neutral.get("confidence")
                elif "neutral_operation_suggestion" in advice_json:
                    neutral = advice_json["neutral_operation_suggestion"]
                    if isinstance(neutral, dict):
                        confidence = neutral.get("confidence")
                
                if confidence is not None:
                    try:
                        confidence = float(confidence)
                        # 确保置信度在0-100范围内
                        confidence = max(0.0, min(100.0, confidence))
                    except (ValueError, TypeError):
                        confidence = 50.0  # 默认值
                        logger.warning(f"⚠️ [工作流持仓分析] confidence转换失败，使用默认值50")
                else:
                    # 如果AI没有生成置信度，根据分析质量设置一个合理的默认值
                    confidence = 50.0  # 默认中等置信度
                    logger.info(f"📊 [工作流持仓分析] JSON中没有confidence字段，使用默认值50")
                
                logger.info(f"📊 [工作流持仓分析] 提取的confidence: {confidence}")
                price_targets = self._extract_price_targets_from_json(advice_json, snapshot, params)
                logger.info(f"📊 [工作流持仓分析] 提取的price_targets: stop_loss={price_targets.stop_loss_price}, take_profit={price_targets.take_profit_price}, breakeven={price_targets.breakeven_price}")
                risk_assessment_text = advice_json.get("risk_assessment", "") or risk_analysis
                logger.info(f"📊 [工作流持仓分析] 提取的risk_assessment长度: {len(risk_assessment_text)}")
                opportunity_assessment_text = advice_json.get("opportunity_assessment", "")
                logger.info(f"📊 [工作流持仓分析] 提取的opportunity_assessment长度: {len(opportunity_assessment_text)}")
            else:
                logger.warning(f"⚠️ [工作流持仓分析] advice_json为空，使用文本解析")
                action, action_reason = self._parse_workflow_action_advice(action_advice if isinstance(action_advice, str) else str(action_advice))

            # 构建完整的分析结果
            # 注意：使用转换后的 action_reason（Markdown格式）而不是原始的 action_advice（可能是JSON）
            action_advice_for_summary = action_reason if action_reason else action_advice
            logger.info(f"📊 [工作流持仓分析] 构建analysis_summary: 使用转换后的action_reason（长度: {len(action_advice_for_summary)}）")
            
            # analysis_summary 包含所有内容（用于summary字段）
            analysis_summary = f"""
【技术面分析】
{technical_analysis}

【基本面分析】
{fundamental_analysis}

【风险评估】
{risk_analysis}

【研究观察】
{action_advice_for_summary}
            """.strip()
            
            # detailed_analysis 只包含技术面、基本面和风险评估，不包含研究观察（避免与action_reason重复）
            detailed_analysis = f"""
【技术面分析】
{technical_analysis}

【基本面分析】
{fundamental_analysis}

【风险评估】
{risk_analysis}
            """.strip()
            
            logger.info(f"📊 [工作流持仓分析] analysis_summary长度: {len(analysis_summary)}, detailed_analysis长度: {len(detailed_analysis)}")
            
            # 确保 detailed_analysis 不为空
            if not detailed_analysis:
                logger.warning(f"⚠️ [工作流持仓分析] detailed_analysis为空，使用默认内容")
                detailed_analysis = f"【技术面分析】\n{technical_analysis}\n\n【基本面分析】\n{fundamental_analysis}\n\n【风险评估】\n{risk_analysis}".strip()

            logger.info(f"📊 [工作流持仓分析] 构建分析结果: action={action}, risk_assessment长度={len(risk_assessment_text)}, detailed_analysis长度={len(detailed_analysis)}")
            
            # recommendation 字段也应该使用转换后的 Markdown 格式（与 action_reason 保持一致）
            # 如果 action_reason 已经转换过，直接使用它；否则使用原始的 action_advice（但必须是字符串）
            if action_reason and isinstance(action_reason, str) and action_reason:
                recommendation_markdown = action_reason
            elif isinstance(action_advice, str):
                recommendation_markdown = action_advice
            else:
                # 如果 action_advice 是字典，使用转换后的 action_reason，或者空字符串
                recommendation_markdown = action_reason if isinstance(action_reason, str) else ""
            
            # 确保 recommendation_markdown 是字符串
            if not isinstance(recommendation_markdown, str):
                logger.warning(f"⚠️ [工作流持仓分析] recommendation不是字符串，类型: {type(recommendation_markdown)}，转换为字符串")
                recommendation_markdown = str(recommendation_markdown) if recommendation_markdown else ""
            
            logger.info(f"📊 [工作流持仓分析] recommendation字段: 使用转换后的Markdown，长度: {len(recommendation_markdown)}")

            # 构建用户版展示内容（user_view）：从 LLM 返回的 JSON 中提取，供前端主界面直接展示
            user_view = PositionUserView()
            raw_user_view = advice_json.get("user_view") if advice_json else None
            if isinstance(raw_user_view, dict):
                user_view.conclusion = str(raw_user_view.get("conclusion") or "").strip()
                user_view.position_context = str(raw_user_view.get("position_context") or "").strip()
                user_view.uncertainty_note = str(raw_user_view.get("uncertainty_note") or "").strip()
                for field, key in (
                    ("key_points", "key_points"),
                    ("risk_focus", "risk_focus"),
                    ("watch_items", "watch_items"),
                ):
                    raw_items = raw_user_view.get(key) or []
                    if isinstance(raw_items, str):
                        raw_items = [raw_items]
                    cleaned_items: List[str] = []
                    if isinstance(raw_items, list):
                        for item in raw_items:
                            text = str(item or "").strip()
                            if text and text not in cleaned_items:
                                cleaned_items.append(text)
                    setattr(user_view, field, cleaned_items[:3])

            # ===== 兜底：若 LLM 未生成合规的 user_view 列表字段，从 neutral_operation_advice / specific_plan 等字段降级提取 =====
            # 确保「核心依据 / 风险提示 / 后续观察点」三个卡片至少有内容可展示
            def _to_clean_list(value, limit=3):
                if not value:
                    return []
                if isinstance(value, str):
                    value = [s.strip() for s in re.split(r'[；;。\n]', value) if s.strip()]
                if isinstance(value, list):
                    result = []
                    for item in value:
                        text = str(item or "").strip()
                        if text and text not in result:
                            result.append(text)
                    return result[:limit]
                return []

            neutral_advice = advice_json.get("neutral_operation_advice") if advice_json else None
            specific_plan = advice_json.get("specific_plan") if advice_json else None
            if isinstance(neutral_advice, dict):
                # 核心依据：优先用 reasoning，其次 core_judgment
                if not user_view.key_points:
                    reasoning = neutral_advice.get("reasoning") or []
                    core_judgment = neutral_advice.get("core_judgment") or ""
                    merged = _to_clean_list(reasoning, 3)
                    if not merged and core_judgment:
                        merged = [core_judgment]
                    user_view.key_points = merged

                # 风险提示：优先用 risk_reminder（字符串），拆分展示
                if not user_view.risk_focus:
                    risk_reminder = neutral_advice.get("risk_reminder") or ""
                    if risk_reminder:
                        user_view.risk_focus = _to_clean_list(risk_reminder, 3)

                # 后续观察点：优先用 analysis_points，其次 specific_plan.observation_factors / immediate_step
                if not user_view.watch_items:
                    analysis_points = neutral_advice.get("analysis_points") or []
                    obs_factors = (specific_plan.get("observation_factors") or []) if isinstance(specific_plan, dict) else []
                    immediate_step = (specific_plan.get("immediate_step") or "") if isinstance(specific_plan, dict) else ""
                    merged = _to_clean_list(analysis_points, 3)
                    if not merged:
                        merged = _to_clean_list(obs_factors, 3)
                    if not merged and immediate_step:
                        merged = [immediate_step]
                    user_view.watch_items = merged

            # 结论兜底：若 conclusion 为空，用 recommendation_markdown / action_reason 替代
            if not user_view.conclusion:
                if recommendation_markdown:
                    user_view.conclusion = recommendation_markdown
                elif action_reason:
                    user_view.conclusion = action_reason

            logger.info(
                f"📊 [工作流持仓分析] 构建user_view: conclusion={user_view.conclusion[:40]}, "
                f"key_points={len(user_view.key_points)}条, risk_focus={len(user_view.risk_focus)}条, watch_items={len(user_view.watch_items)}条"
            )

            return PositionAnalysisResult(
                action=action,
                action_reason=action_reason,
                confidence=confidence,
                price_targets=price_targets,
                risk_assessment=risk_assessment_text,
                opportunity_assessment=opportunity_assessment_text,
                detailed_analysis=detailed_analysis,
                summary=analysis_summary,
                recommendation=recommendation_markdown,
                user_view=user_view,
                source="workflow_engine_v2"
            )

        except Exception as e:
            logger.error(f"❌ [工作流持仓分析] 执行异常: {e}", exc_info=True)
            # 将技术性/英文错误消息转换为用户友好的中文提示
            error_msg = _format_user_friendly_error(str(e))
            return PositionAnalysisResult(
                action=PositionAction.HOLD,
                action_reason=error_msg,
                error_message=error_msg
            )

    def _get_user_analysis_preference(self, user_id: Optional[str], risk_tolerance: str) -> str:
        """根据用户ID和风险偏好确定分析风格

        Returns:
            str: 'aggressive', 'neutral', 'conservative'
        """
        # 根据风险偏好映射到分析风格
        risk_to_preference = {
            "low": "conservative",      # 低风险 -> 保守型
            "medium": "neutral",        # 中等风险 -> 中性型
            "high": "aggressive"        # 高风险 -> 激进型
        }

        return risk_to_preference.get(risk_tolerance, "neutral")

    def _convert_action_advice_json_to_markdown(self, advice_json: Dict[str, Any]) -> str:
        """将JSON格式的研究观察转换为Markdown格式的文本
        
        Args:
            advice_json: 研究观察的JSON对象
            
        Returns:
            Markdown格式的研究观察文本
        """
        lines = []
        
        # 1. 分析摘要（如果有）
        if "analysis_summary" in advice_json:
            summary = advice_json["analysis_summary"]
            if isinstance(summary, str) and summary:
                lines.append(f"## 📊 分析摘要\n\n{summary}\n")
            elif isinstance(summary, dict):
                overall_view = summary.get("overall_view", summary.get("overall_assessment", ""))
                if overall_view:
                    lines.append(f"## 📊 分析摘要\n\n{overall_view}\n")
                key_points = summary.get("key_points", [])
                if isinstance(key_points, list) and key_points:
                    lines.append("\n**关键点**：\n\n")
                    for point in key_points:
                        if point:
                            lines.append(f"- {point}\n")
        
        # 2. 综合评估（如果有）
        if "comprehensive_assessment" in advice_json:
            assessment = advice_json["comprehensive_assessment"]
            if isinstance(assessment, dict):
                lines.append("\n## 🔍 综合评估\n\n")
                
                # 持仓状态
                if "position_status" in assessment:
                    pos_status = assessment["position_status"]
                    if isinstance(pos_status, dict):
                        lines.append("### 📈 持仓状态\n\n")
                        current_price = pos_status.get("current_price", "")
                        cost_price = pos_status.get("cost_price", "")
                        floating_loss_ratio = pos_status.get("floating_loss_ratio", "")
                        assessment_text = pos_status.get("assessment", "")
                        if current_price or cost_price or floating_loss_ratio:
                            lines.append(f"- **当前价格**: ¥{current_price}\n")
                            lines.append(f"- **成本价格**: ¥{cost_price}\n")
                            lines.append(f"- **浮动盈亏**: {floating_loss_ratio}\n")
                        if assessment_text:
                            lines.append(f"\n{assessment_text}\n")
                
                # 技术面视角
                if "technical_perspective" in assessment:
                    tech = assessment["technical_perspective"]
                    if isinstance(tech, dict):
                        lines.append("\n### 📊 技术面视角\n\n")
                        trend = tech.get("trend", "")
                        momentum = tech.get("momentum", "")
                        neutral_view = tech.get("neutral_view", "")
                        if trend:
                            lines.append(f"**趋势判断**: {trend}\n\n")
                        if momentum:
                            lines.append(f"**动能分析**: {momentum}\n\n")
                        if neutral_view:
                            lines.append(f"**中性观点**: {neutral_view}\n\n")
                
                # 基本面视角
                if "fundamental_perspective" in assessment:
                    fund = assessment["fundamental_perspective"]
                    if isinstance(fund, dict):
                        lines.append("\n### 💼 基本面视角\n\n")
                        strengths = fund.get("strengths", "")
                        challenges = fund.get("challenges", "")
                        neutral_view = fund.get("neutral_view", "")
                        if strengths:
                            lines.append(f"**优势**: {strengths}\n\n")
                        if challenges:
                            lines.append(f"**挑战**: {challenges}\n\n")
                        if neutral_view:
                            lines.append(f"**中性观点**: {neutral_view}\n\n")
                
                # 风险视角
                if "risk_perspective" in assessment:
                    risk = assessment["risk_perspective"]
                    if isinstance(risk, dict):
                        lines.append("\n### ⚠️ 风险视角\n\n")
                        volatility = risk.get("volatility", "")
                        key_risk_factors = risk.get("key_risk_factors", "")
                        neutral_view = risk.get("neutral_view", "")
                        if volatility:
                            lines.append(f"**波动性**: {volatility}\n\n")
                        if key_risk_factors:
                            lines.append(f"**关键风险因素**: {key_risk_factors}\n\n")
                        if neutral_view:
                            lines.append(f"**中性观点**: {neutral_view}\n\n")
        
        # 3. 中性研究观察（最核心的内容）
        # 支持两种格式：neutral_operation_advice 和 neutral_operation_suggestion
        neutral_advice = None
        if "neutral_operation_advice" in advice_json:
            neutral_advice = advice_json["neutral_operation_advice"]
        elif "neutral_operation_suggestion" in advice_json:
            neutral_advice = advice_json["neutral_operation_suggestion"]
        
        if neutral_advice and isinstance(neutral_advice, dict):
            lines.append("\n## 💡 研究观察\n\n")
            
            # 核心判断
            core_judgment = neutral_advice.get("core_judgment", "")
            if core_judgment:
                lines.append(f"### 核心判断\n\n{core_judgment}\n\n")
            
            # 研究结论倾向/建议（兼容不同字段名）
            recommended_action = neutral_advice.get("analysis_view", neutral_advice.get("recommended_action", neutral_advice.get("suggested_action", "")))
            if recommended_action:
                lines.append(f"**研究结论倾向**: {recommended_action}\n\n")
            
            # 理由说明（reasoning字段，可能是列表或字符串）
            reasoning = neutral_advice.get("reasoning", [])
            if isinstance(reasoning, list) and reasoning:
                lines.append("### 理由说明\n\n")
                for idx, reason_item in enumerate(reasoning, 1):
                    if isinstance(reason_item, str) and reason_item:
                        lines.append(f"{idx}. {reason_item}\n\n")
                    elif isinstance(reason_item, dict):
                        # 如果是字典，提取所有键值对
                        for key, value in reason_item.items():
                            if value:
                                lines.append(f"**{key}**: {value}\n\n")
            elif isinstance(reasoning, str) and reasoning:
                lines.append(f"### 理由说明\n\n{reasoning}\n\n")
            
            # 具体建议（新格式）
            specific_suggestions = neutral_advice.get("specific_suggestions", [])
            if isinstance(specific_suggestions, list) and specific_suggestions:
                lines.append("### 具体建议\n\n")
                for idx, suggestion in enumerate(specific_suggestions, 1):
                    if isinstance(suggestion, dict):
                        action = suggestion.get("action", "")
                        reason = suggestion.get("reason", "")
                        if action or reason:
                            lines.append(f"#### {idx}. {action}\n\n")
                            if reason:
                                # 处理reason中的Markdown格式（如**加粗**）
                                lines.append(f"{reason}\n\n")
            
            # 详细建议步骤（兼容旧格式）
            detailed_suggestions = neutral_advice.get("detailed_suggestions", [])
            if isinstance(detailed_suggestions, list) and detailed_suggestions:
                for suggestion in detailed_suggestions:
                    if isinstance(suggestion, dict):
                        step = suggestion.get("step", "")
                        content = suggestion.get("content", "")
                        if step or content:
                            if step:
                                lines.append(f"**{step}**\n\n")
                            if content:
                                lines.append(f"{content}\n\n")
            
            # 理由说明（兼容旧格式）
            action_reasoning = neutral_advice.get("action_reasoning", "")
            if action_reasoning:
                lines.append(f"{action_reasoning}\n\n")
            
            # 风险提醒
            risk_reminder = neutral_advice.get("risk_reminder", "")
            if risk_reminder:
                lines.append(f"\n---\n\n> ⚠️ **风险提醒**: {risk_reminder}\n")
        
        # 处理 specific_plan 字段（新格式）
        if "specific_plan" in advice_json:
            specific_plan = advice_json["specific_plan"]
            if isinstance(specific_plan, dict):
                lines.append("\n## 📋 具体计划\n\n")
                
                # 立即步骤
                immediate_step = specific_plan.get("immediate_step", "")
                if immediate_step:
                    lines.append(f"### 立即行动\n\n{immediate_step}\n\n")
                
                # 价格监控
                price_monitoring = specific_plan.get("price_monitoring", {})
                if isinstance(price_monitoring, dict) and price_monitoring:
                    lines.append("### 价格监控\n\n")
                    stop_loss_ref = price_monitoring.get("风险关注参考价", price_monitoring.get("止损参考价", ""))
                    if stop_loss_ref:
                        lines.append(f"**风险关注参考价**: {stop_loss_ref}\n\n")
                    first_target = price_monitoring.get("第一关键验证价位（减亏/解套）", price_monitoring.get("第一目标价（减亏/解套）", ""))
                    if first_target:
                        lines.append(f"**第一关键验证价位（减亏/解套）**: {first_target}\n\n")
                    second_target = price_monitoring.get("第二关键验证价位（达成收益）", price_monitoring.get("第二目标价（达成收益）", ""))
                    if second_target:
                        lines.append(f"**第二关键验证价位（达成收益）**: {second_target}\n\n")
                
                # 观察因素
                observation_factors = specific_plan.get("observation_factors", [])
                if isinstance(observation_factors, list) and observation_factors:
                    lines.append("### 观察要点\n\n")
                    for factor in observation_factors:
                        if factor:
                            lines.append(f"- {factor}\n")
                    lines.append("\n")
        
        # 4. 基于目标的具体建议（兼容旧格式）
        if "specific_suggestions_based_on_your_goals" in advice_json:
            suggestions = advice_json["specific_suggestions_based_on_your_goals"]
            if isinstance(suggestions, dict):
                lines.append("\n## 🎯 基于您的目标的具体建议\n\n")
                
                if_price_rises = suggestions.get("if_price_rises", {})
                if isinstance(if_price_rises, dict):
                    scenario = if_price_rises.get("scenario", "")
                    suggestion = if_price_rises.get("suggestion", "")
                    if scenario or suggestion:
                        lines.append(f"### 📈 如果股价上涨\n\n")
                        if scenario:
                            lines.append(f"**情景**: {scenario}\n\n")
                        if suggestion:
                            lines.append(f"{suggestion}\n\n")
                
                if_price_falls = suggestions.get("if_price_falls", {})
                if isinstance(if_price_falls, dict):
                    scenario = if_price_falls.get("scenario", "")
                    suggestion = if_price_falls.get("suggestion", "")
                    if scenario or suggestion:
                        lines.append(f"### 📉 如果股价下跌\n\n")
                        if scenario:
                            lines.append(f"**情景**: {scenario}\n\n")
                        if suggestion:
                            lines.append(f"{suggestion}\n\n")
                
                if_price_consolidates = suggestions.get("if_price_consolidates", {})
                if isinstance(if_price_consolidates, dict):
                    scenario = if_price_consolidates.get("scenario", "")
                    suggestion = if_price_consolidates.get("suggestion", "")
                    if scenario or suggestion:
                        lines.append(f"### ➡️ 如果股价盘整\n\n")
                        if scenario:
                            lines.append(f"**情景**: {scenario}\n\n")
                        if suggestion:
                            lines.append(f"{suggestion}\n\n")
        
        # 5. 风险提醒（顶层，兼容旧格式）
        if "risk_reminder" in advice_json and "neutral_operation_advice" not in advice_json:
            risk_reminder = advice_json["risk_reminder"]
            if risk_reminder:
                lines.append(f"\n---\n\n> ⚠️ **风险提醒**: {risk_reminder}\n")
        
        # 6. 数据差异说明（兼容旧格式）
        if "data_discrepancy_note" in advice_json:
            note = advice_json["data_discrepancy_note"]
            if isinstance(note, dict):
                important_note = note.get("**重要提示**", note.get("important_note", ""))
                if important_note:
                    lines.append(f"\n> 📌 **重要提示**: {important_note}\n")
            elif isinstance(note, str) and note:
                lines.append(f"\n> 📌 **数据差异说明**: {note}\n")
        
        # 如果没有提取到任何内容，尝试其他字段
        if len(lines) == 0:
            if "conclusion" in advice_json:
                lines.append(advice_json["conclusion"])
            elif "operation_suggestion" in advice_json:
                lines.append(advice_json["operation_suggestion"])
        
        return "\n".join(lines).strip() if lines else "基于综合分析给出建议"
    
    def _extract_price_targets_from_json(self, advice_json: Dict[str, Any], snapshot: PositionSnapshot, params: Optional[PositionAnalysisRequest] = None) -> PriceTarget:
        """从JSON中提取风险关注/收益关注价格
        
        Args:
            advice_json: 研究观察的JSON对象
            snapshot: 持仓快照
            
        Returns:
            PriceTarget对象
        """
        from app.models.portfolio import PriceTarget
        import re
        price_targets = PriceTarget()
        
        logger.info(f"📊 [提取价格目标] JSON字段: {list(advice_json.keys())}")
        
        # 提取风险关注价格（可能在多个位置，兼容旧版止损价字段）
        stop_loss_price = None
        
        # 1. 从顶层提取
        stop_loss_price = advice_json.get("stop_loss_price")
        
        # 2. 从specific_plan.price_monitoring中提取
        if not stop_loss_price and "specific_plan" in advice_json:
            specific_plan = advice_json["specific_plan"]
            if isinstance(specific_plan, dict):
                price_monitoring = specific_plan.get("price_monitoring", {})
                if isinstance(price_monitoring, dict):
                    # 提取"风险关注参考价"字段（兼容旧版"止损参考价"，可能是字符串，包含价格）
                    stop_loss_ref = price_monitoring.get("风险关注参考价", price_monitoring.get("止损参考价", ""))
                    if stop_loss_ref:
                        # 尝试从字符串中提取价格数字（如"¥4.73"或"4.73"）
                        price_match = re.search(r'[\d.]+', str(stop_loss_ref))
                        if price_match:
                            try:
                                stop_loss_price = float(price_match.group())
                                logger.info(f"📊 [提取价格目标] 从specific_plan.price_monitoring提取风险关注价格: {stop_loss_price}")
                            except (ValueError, TypeError):
                                pass
        
        # 3. 从neutral_operation_advice中提取（如果有）
        if not stop_loss_price and "neutral_operation_advice" in advice_json:
            neutral = advice_json["neutral_operation_advice"]
            if isinstance(neutral, dict):
                stop_loss_price = neutral.get("stop_loss_price")
        
        # 处理风险关注价格
        if stop_loss_price:
            try:
                price_targets.stop_loss_price = float(stop_loss_price)
                if snapshot.cost_price and snapshot.cost_price > 0:
                    price_targets.stop_loss_pct = round(
                        (price_targets.stop_loss_price - snapshot.cost_price) / snapshot.cost_price * 100, 2
                    )
                logger.info(f"📊 [提取价格目标] 风险关注价格: {price_targets.stop_loss_price}, 百分比: {price_targets.stop_loss_pct}")
            except (ValueError, TypeError) as e:
                logger.warning(f"⚠️ [提取价格目标] 风险关注价格转换失败: {e}")
        else:
            logger.info(f"📊 [提取价格目标] JSON中没有找到stop_loss_price字段")
        
        # 提取收益关注价格（可能在多个位置，兼容旧版止盈价字段）
        take_profit_price = None
        
        # 1. 从顶层提取
        take_profit_price = advice_json.get("take_profit_price")
        
        # 2. 从specific_plan.price_monitoring中提取（第二关键验证价位通常是收益关注价格）
        if not take_profit_price and "specific_plan" in advice_json:
            specific_plan = advice_json["specific_plan"]
            if isinstance(specific_plan, dict):
                price_monitoring = specific_plan.get("price_monitoring", {})
                if isinstance(price_monitoring, dict):
                    # 优先使用"第二关键验证价位（达成收益）"
                    second_target = price_monitoring.get("第二关键验证价位（达成收益）", price_monitoring.get("第二目标价（达成收益）", ""))
                    if second_target:
                        price_match = re.search(r'[\d.]+', str(second_target))
                        if price_match:
                            try:
                                take_profit_price = float(price_match.group())
                                logger.info(f"📊 [提取价格目标] 从specific_plan.price_monitoring提取收益关注价格: {take_profit_price}")
                            except (ValueError, TypeError):
                                pass
                    # 如果没有第二关键验证价位，尝试使用第一关键验证价位
                    if not take_profit_price:
                        first_target = price_monitoring.get("第一关键验证价位（减亏/解套）", price_monitoring.get("第一目标价（减亏/解套）", ""))
                        if first_target:
                            price_match = re.search(r'[\d.]+', str(first_target))
                            if price_match:
                                try:
                                    take_profit_price = float(price_match.group())
                                    logger.info(f"📊 [提取价格目标] 从specific_plan.price_monitoring提取收益关注价格（使用第一关键验证价位）: {take_profit_price}")
                                except (ValueError, TypeError):
                                    pass
        
        # 3. 从neutral_operation_advice中提取（如果有）
        if not take_profit_price and "neutral_operation_advice" in advice_json:
            neutral = advice_json["neutral_operation_advice"]
            if isinstance(neutral, dict):
                take_profit_price = neutral.get("take_profit_price")
        
        # 处理收益关注价格
        if take_profit_price:
            try:
                price_targets.take_profit_price = float(take_profit_price)
                if snapshot.cost_price and snapshot.cost_price > 0:
                    price_targets.take_profit_pct = round(
                        (price_targets.take_profit_price - snapshot.cost_price) / snapshot.cost_price * 100, 2
                    )
                logger.info(f"📊 [提取价格目标] 收益关注价格: {price_targets.take_profit_price}, 百分比: {price_targets.take_profit_pct}")
            except (ValueError, TypeError) as e:
                logger.warning(f"⚠️ [提取价格目标] 收益关注价格转换失败: {e}")
        else:
            logger.info(f"📊 [提取价格目标] JSON中没有找到take_profit_price字段")
        
        # 回本价就是成本价
        if snapshot.cost_price and snapshot.cost_price > 0:
            price_targets.breakeven_price = snapshot.cost_price
            logger.info(f"📊 [提取价格目标] 回本价: {price_targets.breakeven_price}")
        
        # 如果AI没有生成价格目标，根据用户设定的百分比计算（fallback逻辑）
        if not price_targets.stop_loss_price and params and snapshot.cost_price and snapshot.cost_price > 0:
            # 使用用户设定的最大亏损百分比计算风险关注价格
            max_loss_pct = params.max_loss_pct if params.max_loss_pct else 10.0  # 默认10%
            price_targets.stop_loss_price = round(snapshot.cost_price * (1 - max_loss_pct / 100), 2)
            price_targets.stop_loss_pct = round(-max_loss_pct, 2)
            logger.info(f"📊 [提取价格目标] AI未生成风险关注价格，根据用户设定计算: stop_loss={price_targets.stop_loss_price} (基于{max_loss_pct}%亏损)")
        
        if not price_targets.take_profit_price and params and snapshot.cost_price and snapshot.cost_price > 0:
            # 使用用户设定的目标收益百分比计算收益关注价格
            target_profit_pct = params.target_profit_pct if params.target_profit_pct else 10.0  # 默认10%
            price_targets.take_profit_price = round(snapshot.cost_price * (1 + target_profit_pct / 100), 2)
            price_targets.take_profit_pct = round(target_profit_pct, 2)
            logger.info(f"📊 [提取价格目标] AI未生成收益关注价格，根据用户设定计算: take_profit={price_targets.take_profit_price} (基于{target_profit_pct}%收益)")
        
        return price_targets
    
    def _extract_action_from_json(self, advice_json: Dict[str, Any]) -> PositionAction:
        """从JSON中提取研究结论倾向对应的PositionAction
        
        Args:
            advice_json: 研究观察的JSON对象
            
        Returns:
            PositionAction枚举值（仅供学习研究参考，非交易建议）
        """
        # 优先从neutral_operation_advice.specific_suggestions中提取（新格式）
        if "neutral_operation_advice" in advice_json:
            neutral = advice_json["neutral_operation_advice"]
            if isinstance(neutral, dict):
                # 新格式：从specific_suggestions数组中提取第一个action
                specific_suggestions = neutral.get("specific_suggestions", [])
                if isinstance(specific_suggestions, list) and specific_suggestions:
                    first_suggestion = specific_suggestions[0]
                    if isinstance(first_suggestion, dict):
                        action_text = first_suggestion.get("action", "")
                        if action_text:
                            action_lower = action_text.lower()
                            if any(k in action_lower for k in ["乐观", "加仓", "买入", "补仓"]):
                                return PositionAction.ADD
                            elif any(k in action_lower for k in ["审慎", "减仓", "减持", "卖出", "清仓", "全部卖出"]):
                                return PositionAction.REDUCE
                            elif any(k in action_lower for k in ["中性", "持有", "继续持有", "观察"]):
                                return PositionAction.HOLD
                
                # 兼容旧格式：从recommended_action/analysis_view中提取
                recommended = neutral.get("analysis_view", neutral.get("recommended_action", ""))
                if recommended:
                    recommended_lower = recommended.lower()
                    if any(k in recommended_lower for k in ["乐观", "加仓", "买入"]):
                        return PositionAction.ADD
                    elif any(k in recommended_lower for k in ["审慎", "减仓", "卖出", "清仓"]):
                        return PositionAction.REDUCE
                    elif any(k in recommended_lower for k in ["中性", "持有"]):
                        return PositionAction.HOLD
        
        # 默认持有
        return PositionAction.HOLD
    
    def _parse_workflow_action_advice(self, action_advice: str) -> tuple[PositionAction, str]:
        """从工作流的研究观察中解析出研究结论倾向和理由

        Args:
            action_advice: 研究观察员的输出文本

        Returns:
            tuple: (PositionAction, 研究观察理由)
        """
        if not action_advice:
            return PositionAction.HOLD, "未获取到研究观察"

        # 转换为小写便于匹配
        advice_lower = action_advice.lower()

        # 根据关键词判断研究结论倾向（兼容合规术语与旧版操作术语）
        if any(keyword in advice_lower for keyword in ["乐观", "买入", "加仓", "增持", "建议买", "推荐买", "add"]):
            action = PositionAction.ADD
        elif any(keyword in advice_lower for keyword in ["审慎", "清仓", "建议卖", "推荐卖", "clear", "卖出", "减仓", "reduce"]):
            action = PositionAction.REDUCE
        elif any(keyword in advice_lower for keyword in ["中性", "持有", "观望", "hold"]):
            action = PositionAction.HOLD
        else:
            action = PositionAction.HOLD

        # 提取研究观察理由（取前200个字符作为简要理由）
        action_reason = action_advice[:200] + "..." if len(action_advice) > 200 else action_advice

        return action, action_reason

    def _sanitize_position_analysis_dict(
        self,
        data: Dict[str, Any],
        position_snapshot: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """统一清洗持仓研究详情和历史记录中的结果字段（合规化：移除执行性交易指令）。"""
        return sanitize_position_analysis_payload(data, position_snapshot=position_snapshot)
