"""PortfolioService.PortfolioStatsMixin。"""
from app.services.portfolio.support import (
    uuid,
    datetime,
    Dict,
    Any,
    Optional,
    List,
    ObjectId,
    PortfolioAnalysisStatus,
    PositionSnapshot,
    PortfolioSnapshot,
    IndustryDistribution,
    AccountSnapshot,
    ConcentrationAnalysis,
    PortfolioAnalysisReport,
    PositionResponse,
    PortfolioStatsResponse,
    AccountSummary,
    now_tz,
    logger,
)

class PortfolioStatsMixin:

    # ==================== 持仓统计 ====================

    async def get_portfolio_statistics(self, user_id: str) -> PortfolioStatsResponse:
        """获取持仓统计"""
        positions = await self.get_positions(user_id, source="all", include_market_data=True)

        total_value = 0.0
        total_cost = 0.0
        industry_map: Dict[str, Dict] = {}
        market_map: Dict[str, float] = {}

        for pos in positions:
            cost = pos.cost_price * pos.quantity
            total_cost += cost

            if pos.market_value:
                total_value += pos.market_value
            else:
                total_value += cost  # 无法获取市值时使用成本

            # 🔥 修复：如果行业为空，尝试实时获取并更新到数据库
            industry = pos.industry
            logger.info(f"📊 [统计] 持仓 {pos.code} ({pos.name}) 当前行业: {industry}")
            
            if not industry or industry == "未知":
                logger.info(f"🔄 [统计] 持仓 {pos.code} 行业为空或'未知'，尝试实时获取...")
                # 尝试从数据库获取行业信息
                industry = await self._get_stock_industry(pos.code, pos.market)
                logger.info(f"📊 [统计] 持仓 {pos.code} 获取到的行业: {industry}")
                
                # 如果获取到了行业信息，更新到数据库
                if industry:
                    try:
                        update_result = await self.db[self.positions_collection].update_one(
                            {"_id": ObjectId(pos.id)},
                            {"$set": {"industry": industry, "updated_at": now_tz()}}
                        )
                        if update_result.modified_count > 0:
                            logger.info(f"✅ [统计] 已更新持仓 {pos.code} 的行业信息: {industry}")
                        else:
                            logger.warning(f"⚠️ [统计] 更新持仓 {pos.code} 行业信息失败: 未找到记录或未修改")
                    except Exception as e:
                        logger.error(f"❌ [统计] 更新持仓 {pos.code} 行业信息失败: {e}", exc_info=True)
                else:
                    # 如果无法获取，使用"未知"
                    logger.warning(f"⚠️ [统计] 持仓 {pos.code} 无法获取行业信息，使用'未知'")
                    industry = "未知"
            
            # 行业统计
            if industry not in industry_map:
                industry_map[industry] = {"value": 0.0, "count": 0}
            industry_map[industry]["value"] += pos.market_value or cost
            industry_map[industry]["count"] += 1

            # 市场统计
            market = pos.market
            if market not in market_map:
                market_map[market] = 0.0
            market_map[market] += pos.market_value or cost

        # 计算盈亏
        unrealized_pnl = total_value - total_cost
        unrealized_pnl_pct = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0

        # 转换行业分布
        industry_dist = []
        for industry, data in industry_map.items():
            pct = (data["value"] / total_value * 100) if total_value > 0 else 0.0
            industry_dist.append(IndustryDistribution(
                industry=industry,
                value=round(data["value"], 2),
                percentage=round(pct, 2),
                count=data["count"]
            ))
        industry_dist.sort(key=lambda x: x.value, reverse=True)

        return PortfolioStatsResponse(
            total_positions=len(positions),
            total_value=round(total_value, 2),
            total_cost=round(total_cost, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            unrealized_pnl_pct=round(unrealized_pnl_pct, 2),
            industry_distribution=industry_dist,
            market_distribution={k: round(v, 2) for k, v in market_map.items()}
        )

    # ==================== 持仓分析 ====================

    async def analyze_portfolio(
        self,
        user_id: str,
        include_paper: bool = True,
        research_depth: str = "标准"
    ) -> PortfolioAnalysisReport:
        """
        执行持仓分析

        Args:
            user_id: 用户ID
            include_paper: 是否包含模拟交易持仓
            research_depth: 分析深度
        """
        analysis_id = str(uuid.uuid4())
        analysis_date = datetime.now().strftime("%Y-%m-%d")

        # 创建分析报告
        report = PortfolioAnalysisReport(
            analysis_id=analysis_id,
            user_id=user_id,
            analysis_date=analysis_date,
            status=PortfolioAnalysisStatus.PROCESSING
        )

        try:
            # 1. 获取持仓数据
            source = "all" if include_paper else "real"
            positions = await self.get_positions(user_id, source=source)

            if not positions:
                report.status = PortfolioAnalysisStatus.FAILED
                report.error_message = "没有持仓数据"
                await self._save_analysis_report(report)
                return report

            # 2. 获取资金账户信息
            account_summary = await self.get_account_summary(user_id)

            # 3. 构建持仓快照（包含资金账户信息）
            portfolio_snapshot = await self._build_portfolio_snapshot(positions, account_summary)
            report.portfolio_snapshot = portfolio_snapshot

            # 4. 计算行业分布
            report.industry_distribution = await self._calculate_industry_distribution(positions)

            # 5. 计算集中度
            report.concentration_analysis = self._calculate_concentration(positions)

            # 6. 调用AI分析（传入资金账户信息）
            start_time = datetime.now()
            ai_result = await self._call_ai_analysis(portfolio_snapshot, report.industry_distribution, user_id)
            execution_time = (datetime.now() - start_time).total_seconds()

            report.ai_analysis = ai_result
            report.health_score = self._calculate_health_score(report)
            report.risk_level = self._calculate_risk_level(report)
            report.execution_time = execution_time
            report.status = PortfolioAnalysisStatus.COMPLETED

            logger.info(f"✅ 持仓分析完成: {analysis_id}, 健康度: {report.health_score}")

        except Exception as e:
            logger.error(f"❌ 持仓分析失败: {e}")
            report.status = PortfolioAnalysisStatus.FAILED
            report.error_message = str(e)

        # 保存报告
        await self._save_analysis_report(report)
        return report

    async def get_analysis_history(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 10
    ) -> Dict[str, Any]:
        """获取分析历史"""
        skip = (page - 1) * page_size

        cursor = self.db[self.analysis_collection].find(
            {"user_id": user_id}
        ).sort("created_at", -1).skip(skip).limit(page_size)

        items = await cursor.to_list(None)
        total = await self.db[self.analysis_collection].count_documents({"user_id": user_id})

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size
        }

    async def get_analysis_detail(self, analysis_id: str) -> Optional[Dict]:
        """获取分析报告详情"""
        return await self.db[self.analysis_collection].find_one({"analysis_id": analysis_id})

    # ==================== 辅助方法 ====================

    async def _build_portfolio_snapshot(
        self,
        positions: List[PositionResponse],
        account_summary: AccountSummary = None
    ) -> PortfolioSnapshot:
        """构建持仓快照（包含资金账户信息）"""
        total_value = 0.0
        total_cost = 0.0
        snapshots = []

        # 获取总资产用于计算占比（默认使用CNY）
        total_assets = 0.0
        if account_summary:
            total_assets = account_summary.total_assets.get("CNY", 0.0)

        for pos in positions:
            cost = pos.cost_price * pos.quantity
            value = pos.market_value or cost
            total_cost += cost
            total_value += value

            # 计算持仓天数
            holding_days = None
            if pos.buy_date:
                holding_days = (datetime.now() - pos.buy_date).days

            # 计算资金占用比例（相对于总资产）
            cost_pct = None
            position_pct = None
            if total_assets > 0:
                cost_pct = round((cost / total_assets) * 100, 2)
                position_pct = round((value / total_assets) * 100, 2)

            snapshots.append(PositionSnapshot(
                code=pos.code,
                name=pos.name,
                market=pos.market,
                quantity=pos.quantity,
                cost_price=pos.cost_price,
                current_price=pos.current_price,
                market_value=pos.market_value,
                unrealized_pnl=pos.unrealized_pnl,
                unrealized_pnl_pct=pos.unrealized_pnl_pct,
                industry=pos.industry,
                holding_days=holding_days,
                cost_value=round(cost, 2),
                cost_pct=cost_pct,
                position_pct=position_pct
            ))

        unrealized_pnl = total_value - total_cost
        unrealized_pnl_pct = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0

        # 构建资金账户快照
        account_snapshot = None
        if account_summary:
            cash_cny = account_summary.cash.get("CNY", 0.0)
            positions_value_cny = account_summary.positions_value.get("CNY", 0.0)
            total_assets_cny = account_summary.total_assets.get("CNY", 0.0)
            initial_capital_cny = account_summary.initial_capital.get("CNY", 0.0)
            net_capital_cny = account_summary.net_capital.get("CNY", 0.0)
            profit_cny = account_summary.profit.get("CNY", 0.0)
            profit_pct_cny = account_summary.profit_pct.get("CNY", 0.0)

            position_ratio = (positions_value_cny / total_assets_cny * 100) if total_assets_cny > 0 else 0.0
            cash_ratio = (cash_cny / total_assets_cny * 100) if total_assets_cny > 0 else 0.0

            account_snapshot = AccountSnapshot(
                total_assets=round(total_assets_cny, 2),
                cash=round(cash_cny, 2),
                positions_value=round(positions_value_cny, 2),
                position_ratio=round(position_ratio, 2),
                cash_ratio=round(cash_ratio, 2),
                initial_capital=round(initial_capital_cny, 2),
                net_capital=round(net_capital_cny, 2),
                total_profit=round(profit_cny, 2),
                total_profit_pct=round(profit_pct_cny, 2),
                currency="CNY"
            )

        return PortfolioSnapshot(
            total_positions=len(positions),
            total_value=round(total_value, 2),
            total_cost=round(total_cost, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            unrealized_pnl_pct=round(unrealized_pnl_pct, 2),
            positions=snapshots,
            account=account_snapshot
        )

    async def _calculate_industry_distribution(
        self,
        positions: List[PositionResponse]
    ) -> List[IndustryDistribution]:
        """计算行业分布"""
        industry_map: Dict[str, Dict] = {}
        total_value = 0.0

        for pos in positions:
            value = pos.market_value or (pos.cost_price * pos.quantity)
            total_value += value

            industry = pos.industry or "未知"
            if industry not in industry_map:
                industry_map[industry] = {"value": 0.0, "count": 0}
            industry_map[industry]["value"] += value
            industry_map[industry]["count"] += 1

        result = []
        for industry, data in industry_map.items():
            pct = (data["value"] / total_value * 100) if total_value > 0 else 0.0
            result.append(IndustryDistribution(
                industry=industry,
                value=round(data["value"], 2),
                percentage=round(pct, 2),
                count=data["count"]
            ))

        result.sort(key=lambda x: x.value, reverse=True)
        return result

    def _calculate_concentration(self, positions: List[PositionResponse]) -> ConcentrationAnalysis:
        """计算集中度分析"""
        if not positions:
            return ConcentrationAnalysis()

        # 计算每只股票的市值
        values = []
        industries = set()
        total_value = 0.0

        for pos in positions:
            value = pos.market_value or (pos.cost_price * pos.quantity)
            values.append(value)
            total_value += value
            if pos.industry:
                industries.add(pos.industry)

        if total_value == 0:
            return ConcentrationAnalysis(stock_count=len(positions))

        # 排序获取Top N
        values.sort(reverse=True)

        top1_pct = (values[0] / total_value * 100) if len(values) >= 1 else 0
        top3_pct = (sum(values[:3]) / total_value * 100) if len(values) >= 3 else (sum(values) / total_value * 100)
        top5_pct = (sum(values[:5]) / total_value * 100) if len(values) >= 5 else (sum(values) / total_value * 100)

        # 计算HHI指数
        hhi = sum((v / total_value * 100) ** 2 for v in values)

        return ConcentrationAnalysis(
            stock_count=len(positions),
            top1_pct=round(top1_pct, 2),
            top3_pct=round(top3_pct, 2),
            top5_pct=round(top5_pct, 2),
            hhi_index=round(hhi, 2),
            industry_count=len(industries)
        )
