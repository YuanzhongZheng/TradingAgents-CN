"""PortfolioService.PortfolioReportMixin。"""
from app.services.portfolio.support import (
    datetime,
    Dict,
    Any,
    Optional,
    List,
    truncate_api_key,
    PortfolioSnapshot,
    IndustryDistribution,
    AIAnalysisResult,
    PortfolioAnalysisReport,
    logger,
)

class PortfolioReportMixin:

    async def _call_ai_analysis(
        self,
        snapshot: PortfolioSnapshot,
        industry_dist: List[IndustryDistribution],
        user_id: str = None
    ) -> AIAnalysisResult:
        """调用AI进行持仓分析"""
        try:
            # 查询分红数据
            dividend_info = {}
            if user_id:
                dividend_info = await self._get_dividend_summary(user_id)

            # 构建分析提示词
            prompt = self._build_analysis_prompt(snapshot, industry_dist, dividend_info)

            # 从系统配置获取深度分析模型（与个股分析保持一致）
            from app.services.model_capability_service import get_model_capability_service
            from app.services.llm_model_resolution import get_provider_and_url_by_model_sync
            from tradingagents.graph.trading_graph import create_llm_by_provider

            capability_service = get_model_capability_service()
            _, deep_model = capability_service._get_default_models()
            model_name = deep_model

            logger.info(f"🤖 [整体持仓AI分析] 从系统配置获取深度分析模型: {model_name}")

            provider_info = get_provider_and_url_by_model_sync(model_name)
            backend_url = provider_info.get("backend_url")
            api_key = provider_info.get("api_key")

            logger.info(f"🏭 [整体持仓AI分析] 模型供应商: {provider_info.get('provider', '未知')}")
            logger.info(f"🔗 [整体持仓AI分析] API地址: {backend_url or '未配置'}")
            logger.info(f"🔑 [整体持仓AI分析] API Key: {'已配置' if api_key else '未配置'}")

            if not backend_url:
                raise ValueError(f"模型 {model_name} 的API地址未配置，请在设置页面配置")

            # 使用统一的 LLM 适配器
            provider_name = provider_info.get("provider", "dashscope")
            logger.info(f"🔧 [持仓AI分析] 准备创建 LLM: provider={provider_name}, model={model_name}, temperature=0.3")
            if api_key:
                logger.info(f"🔑 [持仓AI分析] API Key: {truncate_api_key(api_key)}")
            llm = create_llm_by_provider(
                provider=provider_name,
                model=model_name,
                backend_url=backend_url,
                temperature=0.3,
                max_tokens=4000,
                timeout=120,
                api_key=api_key
            )

            response = await llm.ainvoke(prompt)

            # 记录 token 使用
            try:
                from app.services.usage_statistics_service import usage_statistics_service
                await usage_statistics_service.record_llm_usage(
                    response=response,
                    session_id="portfolio",
                    analysis_type="portfolio"
                )
            except Exception as e:
                logger.warning(f"记录 token 使用失败: {e}")

            content = response.content if hasattr(response, 'content') else str(response)

            # 解析响应
            return self._parse_ai_response(content)

        except Exception as e:
            logger.error(f"AI分析失败: {e}", exc_info=True)
            from app.utils.error_formatter import ErrorFormatter

            user_message = ErrorFormatter.format_user_message(str(e))
            return AIAnalysisResult(
                summary=f"AI分析暂时不可用：{user_message}",
                suggestions=["请检查大模型账号、API Key、余额或套餐配置后重试"]
            )

    async def _get_real_market_rules(self, market: str) -> Optional[Dict[str, Any]]:
        """获取真实持仓的市场规则配置（复用 paper_market_rules 集合）"""
        try:
            rules_doc = await self.db["paper_market_rules"].find_one({"market": market})
            if rules_doc:
                return rules_doc.get("rules", {})
        except Exception as e:
            logger.warning(f"获取市场规则失败: {e}")
        return None

    def _calculate_real_commission(
        self, market: str, side: str, amount: float, rules: Dict[str, Any]
    ) -> Dict[str, Any]:
        """计算真实持仓交易的手续费

        Args:
            market: 市场（CN/HK/US）
            side: 买/卖（buy/sell）
            amount: 成交金额
            rules: 市场规则配置

        Returns:
            {
                "commission": float,        # 总手续费
                "broker_fee": float,        # 佣金
                "stamp_duty": float,        # 印花税
                "transfer_fee": float,      # 过户费
                "other_fee": float,         # 其他费用（港股/美股）
            }
        """
        result = {
            "commission": 0.0,
            "broker_fee": 0.0,
            "stamp_duty": 0.0,
            "transfer_fee": 0.0,
            "other_fee": 0.0,
        }
        if not rules or "commission" not in rules:
            return result

        config = rules["commission"]

        # 佣金
        comm_rate = config.get("rate", 0.0)
        comm_min = config.get("min", 0.0)
        broker_fee = max(amount * comm_rate, comm_min) if comm_rate > 0 else 0.0
        result["broker_fee"] = round(broker_fee, 2)

        # 印花税（A股仅卖出，港股买卖都收）
        stamp_duty = 0.0
        if "stamp_duty_rate" in config:
            if market == "CN" and side == "sell":
                stamp_duty = amount * config["stamp_duty_rate"]
            elif market == "HK":
                stamp_duty = amount * config["stamp_duty_rate"]
        result["stamp_duty"] = round(stamp_duty, 2)

        # 过户费（A股）
        transfer_fee = 0.0
        if "transfer_fee_rate" in config and market == "CN":
            transfer_fee = amount * config["transfer_fee_rate"]
        result["transfer_fee"] = round(transfer_fee, 2)

        # 其他费用（港股）
        other_fee = 0.0
        if market == "HK":
            if "transaction_levy_rate" in config:
                other_fee += amount * config["transaction_levy_rate"]
            if "trading_fee_rate" in config:
                other_fee += amount * config["trading_fee_rate"]
            if "settlement_fee_rate" in config:
                other_fee += amount * config["settlement_fee_rate"]

        # SEC费用（美股，仅卖出）
        if market == "US" and side == "sell" and "sec_fee_rate" in config:
            other_fee += amount * config["sec_fee_rate"]
        result["other_fee"] = round(other_fee, 2)

        result["commission"] = round(
            broker_fee + stamp_duty + transfer_fee + other_fee, 2
        )
        return result

    async def _get_dividend_summary(self, user_id: str) -> Dict[str, Any]:
        """查询用户的分红收益汇总

        Returns:
            {
                "total_dividend": float,      # 累计分红总额
                "dividend_count": int,         # 分红股票数
                "by_stock": {                  # 按股票汇总
                    "000001": {"name": "平安银行", "amount": 120.0, "count": 2},
                    ...
                }
            }
        """
        result = {"total_dividend": 0.0, "dividend_count": 0, "by_stock": {}}
        try:
            # 查询资金账户中的分红记录
            cursor = self.db[self.transactions_collection].find({
                "user_id": user_id,
                "type": "dividend"
            })
            total = 0.0
            by_stock = {}
            async for txn in cursor:
                amount = float(txn.get("amount", 0))
                code = txn.get("code", "")
                name = txn.get("name", "")
                if amount <= 0:
                    continue
                total += amount
                if code:
                    if code not in by_stock:
                        by_stock[code] = {"name": name, "amount": 0.0, "count": 0}
                    by_stock[code]["amount"] += amount
                    by_stock[code]["count"] += 1

            result["total_dividend"] = total
            result["dividend_count"] = len(by_stock)
            result["by_stock"] = by_stock

            if total > 0:
                logger.info(f"💵 查询到用户 {user_id} 累计分红 ¥{total:,.2f}，{len(by_stock)} 只股票")
        except Exception as e:
            logger.warning(f"查询分红数据失败: {e}")
        return result

    def _build_analysis_prompt(
        self,
        snapshot: PortfolioSnapshot,
        industry_dist: List[IndustryDistribution],
        dividend_info: Dict[str, Any] = None
    ) -> str:
        """构建分析提示词（包含资金账户、个股收益和分红信息）"""

        # 资金账户信息
        account_section = ""
        if snapshot.account:
            acc = snapshot.account
            account_section = f"""## 💰 资金账户概况
| 项目 | 金额 | 说明 |
|------|------|------|
| 总资产 | ¥{acc.total_assets:,.2f} | 现金+持仓市值 |
| 可用现金 | ¥{acc.cash:,.2f} | 占比 {acc.cash_ratio:.1f}% |
| 持仓市值 | ¥{acc.positions_value:,.2f} | 占比 {acc.position_ratio:.1f}% |
| 净投入资金 | ¥{acc.net_capital:,.2f} | 初始资金+入金-出金 |
| 总盈亏 | ¥{acc.total_profit:,.2f} | 收益率 {acc.total_profit_pct:.2f}% |

### 仓位水平评估
- 当前仓位: **{acc.position_ratio:.1f}%**
- {"⚠️ 重仓状态（>80%），风险敞口较大" if acc.position_ratio > 80 else "⚠️ 偏重仓位（60-80%），建议保留部分现金" if acc.position_ratio > 60 else "✅ 仓位适中（40-60%），攻守兼备" if acc.position_ratio > 40 else "💡 轻仓状态（<40%），可择机加仓"}

"""

        # 持仓明细表（增强版：包含资金占用和收益详情）
        position_table = "| 股票代码 | 股票名称 | 行业 | 持仓天数 | 成本金额 | 市值 | 盈亏金额 | 盈亏% | 资金占比 |\n"
        position_table += "|---------|---------|-----|--------|---------|-----|---------|------|--------|\n"

        # 按盈亏金额排序，便于分析
        sorted_positions = sorted(
            snapshot.positions,
            key=lambda x: x.unrealized_pnl or 0,
            reverse=True
        )

        for pos in sorted_positions:
            pnl_emoji = "🟢" if (pos.unrealized_pnl or 0) >= 0 else "🔴"
            holding_days_str = f"{pos.holding_days}天" if pos.holding_days else "-"
            cost_value = pos.cost_value or (pos.cost_price * pos.quantity)
            position_table += f"| {pos.code} | {pos.name or '-'} | {pos.industry or '-'} | "
            position_table += f"{holding_days_str} | ¥{cost_value:,.0f} | ¥{pos.market_value or cost_value:,.0f} | "
            position_table += f"{pnl_emoji} ¥{pos.unrealized_pnl or 0:,.0f} | {pos.unrealized_pnl_pct or 0:.2f}% | "
            position_table += f"{pos.position_pct or '-'}% |\n"

        # 个股收益分析
        profitable_positions = [p for p in snapshot.positions if (p.unrealized_pnl or 0) > 0]
        losing_positions = [p for p in snapshot.positions if (p.unrealized_pnl or 0) < 0]
        total_profit = sum(p.unrealized_pnl or 0 for p in profitable_positions)
        total_loss = sum(p.unrealized_pnl or 0 for p in losing_positions)

        performance_section = f"""## 📊 个股收益分析
- 盈利股票: {len(profitable_positions)}只，合计盈利 ¥{total_profit:,.2f}
- 亏损股票: {len(losing_positions)}只，合计亏损 ¥{total_loss:,.2f}
- 盈亏比: {abs(total_profit/total_loss) if total_loss != 0 else '∞'}:1

### 盈利最多的股票
"""
        for pos in sorted_positions[:3]:
            if (pos.unrealized_pnl or 0) > 0:
                performance_section += f"- {pos.name}({pos.code}): +¥{pos.unrealized_pnl:,.0f} (+{pos.unrealized_pnl_pct:.2f}%)\n"

        performance_section += "\n### 亏损最多的股票\n"
        for pos in reversed(sorted_positions[-3:]):
            if (pos.unrealized_pnl or 0) < 0:
                performance_section += f"- {pos.name}({pos.code}): ¥{pos.unrealized_pnl:,.0f} ({pos.unrealized_pnl_pct:.2f}%)\n"

        # 资金占用分析
        capital_section = ""
        if snapshot.account and snapshot.account.total_assets > 0:
            high_concentration = [p for p in snapshot.positions if (p.position_pct or 0) > 20]
            if high_concentration:
                capital_section = "\n### ⚠️ 高集中度持仓（单只>20%）\n"
                for pos in high_concentration:
                    capital_section += f"- {pos.name}({pos.code}): 占比 {pos.position_pct:.1f}%，资金占用 ¥{pos.cost_value:,.0f}\n"

        # 行业分布
        industry_text = "\n".join([
            f"- {d.industry}: {d.percentage:.1f}% ({d.count}只)"
            for d in industry_dist[:5]
        ])

        # 分红收益信息
        dividend_section = ""
        if dividend_info and dividend_info.get("total_dividend", 0) > 0:
            dividend_section = f"""
## 💵 分红收益
- 累计分红总额: ¥{dividend_info['total_dividend']:,.2f}
- 分红股票数: {dividend_info['dividend_count']} 只
"""
            stock_dividends = dividend_info.get("by_stock", {})
            if stock_dividends:
                dividend_section += "\n| 股票代码 | 股票名称 | 累计分红 | 分红次数 |\n"
                dividend_section += "|---------|---------|---------|--------|\n"
                for code, info in sorted(stock_dividends.items(), key=lambda x: x[1]["amount"], reverse=True):
                    dividend_section += f"| {code} | {info.get('name', '-')} | ¥{info['amount']:,.2f} | {info.get('count', 0)}次 |\n"
            dividend_section += "\n**注意**：分红收益未计入浮动盈亏，实际总收益 = 浮动盈亏 + 累计分红。\n"

        prompt = f"""# 持仓组合分析任务

{account_section}## 📈 持仓概况
- 持仓总市值: ¥{snapshot.total_value:,.2f}
- 持仓成本: ¥{snapshot.total_cost:,.2f}
- 浮动盈亏: ¥{snapshot.unrealized_pnl:,.2f} ({snapshot.unrealized_pnl_pct:.2f}%)
- 持仓股票数: {snapshot.total_positions} 只

## 📋 持仓明细
{position_table}

{performance_section}
{capital_section}

## 🏭 行业分布
{industry_text}
{dividend_section}
## 分析要求
请基于以上数据，从以下维度进行专业分析（用中文回答）：

1. **持仓健康度评估** (给出0-100分，并说明主要扣分项)
2. **风险评估** (低/中/高，结合仓位水平和集中度分析)
3. **资金配置评估** (评估现金/持仓比例是否合理，资金利用效率)
4. **个股表现分析** (哪些股票表现好/差，是否需要止盈/止损；如有分红数据，需结合分红计算真实总收益)
5. **分红收益分析** (如有分红数据，评估分红对组合收益的贡献，哪些股票分红稳定)
6. **组合优势** (列出2-3点)
7. **组合劣势** (列出2-3点)
8. **调仓建议** (列出2-3条具体建议，包括：
   - 是否需要调整仓位水平
   - 是否需要止盈/止损某些股票
   - 是否需要调整行业配置)
9. **综合评价** (150字以内的总结，含分红的总收益)

请直接给出分析结果，不需要JSON格式。"""

        return prompt

    def _parse_ai_response(self, content: str) -> AIAnalysisResult:
        """解析AI响应"""
        # 简单解析，提取关键信息
        lines = content.split('\n')

        summary = ""
        strengths = []
        weaknesses = []
        suggestions = []

        current_section = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if "综合评价" in line or "总结" in line:
                current_section = "summary"
            elif "优势" in line:
                current_section = "strengths"
            elif "劣势" in line or "不足" in line:
                current_section = "weaknesses"
            elif "建议" in line:
                current_section = "suggestions"
            elif line.startswith("-") or line.startswith("•") or line.startswith("*"):
                item = line.lstrip("-•* ").strip()
                if current_section == "strengths" and item:
                    strengths.append(item)
                elif current_section == "weaknesses" and item:
                    weaknesses.append(item)
                elif current_section == "suggestions" and item:
                    suggestions.append(item)
            elif current_section == "summary":
                summary += line + " "

        return AIAnalysisResult(
            summary=summary.strip() or content[:200],
            strengths=strengths[:3],
            weaknesses=weaknesses[:3],
            suggestions=suggestions[:5],
            detailed_report=content
        )

    def _calculate_health_score(self, report: PortfolioAnalysisReport) -> float:
        """计算健康度评分

        改进逻辑：
        1. 单股持仓时不因集中度扣分（单股起步是正常的）
        2. 主要根据盈亏情况评分
        3. 多股时才考虑集中度和行业分散度
        """
        score = 70.0  # 基础分提高到70

        concentration = report.concentration_analysis
        stock_count = concentration.stock_count or 1
        pnl_pct = report.portfolio_snapshot.total_unrealized_pnl_pct or 0

        # === 单股持仓的评分逻辑 ===
        if stock_count == 1:
            # 单股时不扣集中度分，主要看盈亏
            if pnl_pct > 20:
                score += 20
            elif pnl_pct > 10:
                score += 15
            elif pnl_pct > 5:
                score += 10
            elif pnl_pct > 0:
                score += 5
            elif pnl_pct > -5:
                score += 0  # 小幅亏损，不扣分
            elif pnl_pct > -10:
                score -= 5
            elif pnl_pct > -20:
                score -= 10
            else:
                score -= 20  # 亏损超20%

            return max(0, min(100, score))

        # === 多股持仓的评分逻辑 ===
        # 集中度评分 (最多扣15分)
        if concentration.top1_pct > 60:
            score -= 12
        elif concentration.top1_pct > 40:
            score -= 6

        if concentration.hhi_index > 4000:
            score -= 8
        elif concentration.hhi_index > 2500:
            score -= 4

        # 行业分散度加分 (最多加15分)
        if concentration.industry_count >= 5:
            score += 12
        elif concentration.industry_count >= 3:
            score += 8
        elif concentration.industry_count >= 2:
            score += 4

        # 盈利情况加分 (最多加/扣15分)
        if pnl_pct > 20:
            score += 12
        elif pnl_pct > 10:
            score += 8
        elif pnl_pct > 0:
            score += 4
        elif pnl_pct < -20:
            score -= 12
        elif pnl_pct < -10:
            score -= 6

        return max(0, min(100, score))

    def _calculate_risk_level(self, report: PortfolioAnalysisReport) -> str:
        """计算风险等级

        改进逻辑：
        1. 单股持仓时，不单纯按集中度判高风险
        2. 结合持仓市值、亏损比例等绝对风险指标
        3. 小资金轻仓位应判为低风险
        """
        concentration = report.concentration_analysis
        snapshot = report.portfolio_snapshot

        # 获取基本指标
        stock_count = concentration.stock_count or 1
        total_value = snapshot.total_market_value or 0
        total_pnl_pct = snapshot.total_unrealized_pnl_pct or 0

        # === 单股持仓的特殊处理 ===
        if stock_count == 1:
            # 单股持仓时，主要看亏损幅度和绝对金额
            # 小资金单股起步是正常的投资行为，不应判高风险

            # 亏损超过20%为高风险
            if total_pnl_pct < -20:
                return "高"
            # 亏损超过10%为中风险
            elif total_pnl_pct < -10:
                return "中"
            # 盈利或小幅亏损为低风险
            else:
                return "低"

        # === 多股持仓的集中度风险评估 ===
        # 高风险条件：集中度过高且有较大亏损
        if concentration.top1_pct > 50 and total_pnl_pct < -10:
            return "高"

        if concentration.hhi_index > 3000 and total_pnl_pct < -5:
            return "高"

        # 低风险条件：分散度好
        if concentration.top1_pct < 30 and concentration.industry_count >= 3:
            return "低"

        if concentration.top1_pct < 20 and concentration.industry_count >= 5:
            return "低"

        return "中"

    async def _save_analysis_report(self, report: PortfolioAnalysisReport):
        """保存分析报告"""
        report_dict = report.model_dump(by_alias=True, exclude={"id"})
        await self.db[self.analysis_collection].insert_one(report_dict)
        logger.info(f"✅ 保存分析报告: {report.analysis_id}")

    # ==================== 数据获取工具 ====================

    async def _get_stock_price(self, code: str, market: str) -> Optional[float]:
        """获取股票最新价格"""
        code6 = str(code).strip().zfill(6)
        if market in {"CN", "ETF"}:
            # A股从数据库获取：1) market_quotes 2) stock_basic_info 3) stock_daily_quotes 历史数据
            q = await self.db["market_quotes"].find_one(
                {"$or": [{"code": code}, {"code": code6}, {"symbol": code}, {"symbol": code6}]},
                {"_id": 0, "close": 1}
            )
            if q and q.get("close"):
                try:
                    return float(q["close"])
                except (ValueError, TypeError):
                    pass

            # 回退到 basic_info
            basic = await self.db["stock_basic_info"].find_one(
                {"$or": [{"code": code}, {"code": code6}, {"symbol": code}, {"symbol": code6}]},
                {"_id": 0, "current_price": 1}
            )
            if basic and basic.get("current_price"):
                try:
                    return float(basic["current_price"])
                except (ValueError, TypeError):
                    pass

            # 回退到 stock_daily_quotes 历史数据（取最新收盘价）
            daily = await self.db["stock_daily_quotes"].find_one(
                {"$or": [{"symbol": code}, {"symbol": code6}, {"code": code}, {"code": code6}]},
                sort=[("trade_date", -1)],
                projection={"_id": 0, "close": 1}
            )
            if daily and daily.get("close"):
                try:
                    return float(daily["close"])
                except (ValueError, TypeError):
                    pass

            # 回退到 etf_daily_quotes（ETF 行情）
            etf_daily = await self.db["etf_daily_quotes"].find_one(
                {"code": code6},
                sort=[("trade_date", -1)],
                projection={"_id": 0, "close": 1}
            )
            if etf_daily and etf_daily.get("close"):
                try:
                    return float(etf_daily["close"])
                except (ValueError, TypeError):
                    pass
        else:
            # 港股/美股
            try:
                from app.services.foreign_stock_service import ForeignStockService
                service = ForeignStockService(db=self.db)
                quote = await service.get_quote(market, code)
                if quote:
                    for field in ['regularMarketPrice', 'price', 'last']:
                        if quote.get(field):
                            return float(quote[field])
            except Exception as e:
                logger.warning(f"获取{market}股票价格失败: {code}, {e}")

        return None

    async def _validate_cost_price_against_historical(
        self, code: str, market: str, buy_date, cost_price: float
    ) -> Optional[Dict[str, Any]]:
        """
        校验成本价是否在买入日历史数据的最高价和最低价范围内。

        校验策略：
        1. 优先用数据库中的前复权数据校验（大多数情况成本价就是前复权价格）
        2. 若前复权校验失败，实时调用 API 获取不复权数据兜底校验
           （用户输入的实际成交价是不复权的，与前复权K线可能不一致）
        3. 不复权校验通过时，计算复权因子用于将用户价格转为前复权保存

        Args:
            code: 股票代码
            market: 市场类型
            buy_date: 买入日期
            cost_price: 用户输入的成本价

        Returns:
            None — 成本价与前复权数据一致，无需转换
            Dict — 成本价是不复权价格，需要转换：
                {"adjust_type": "none", "qfq_price": float, "adj_factor": float}

        Raises:
            ValueError — 成本价明显错误，拒绝添加
        """
        if not buy_date:
            return None
        # 格式化买入日期：支持 datetime、date、ISO 字符串
        # ⚠️ 时区修正：前端通过 toISOString() 发送 UTC 字符串（如 "2026-05-11T16:00:00.000Z"），
        # Pydantic 解析为 aware datetime。直接 strftime 会用 UTC 字段值，导致东八区用户
        # 选的 5月12日 被格式化为 "2026-05-11"。必须先转成本地时区再格式化。
        if hasattr(buy_date, "strftime"):
            if getattr(buy_date, "tzinfo", None) is not None:
                # aware datetime：转到本地时区（服务器 TZ，通常为 Asia/Shanghai）
                buy_date = buy_date.astimezone()
            buy_date_str = buy_date.strftime("%Y-%m-%d")
            buy_date_compact = buy_date.strftime("%Y%m%d")
        elif isinstance(buy_date, str):
            buy_date_str = buy_date[:10] if len(buy_date) >= 10 else buy_date
            buy_date_compact = buy_date_str.replace("-", "") if "-" in buy_date_str else buy_date_str
        else:
            return None  # 无法解析则跳过校验
        code6 = str(code).strip().zfill(6)
        symbol_conds = [{"symbol": code}, {"symbol": code6}, {"code": code}, {"code": code6}]
        date_conds = [{"trade_date": buy_date_str}, {"trade_date": buy_date_compact}]
        normalized_market = "CN" if market == "ETF" else market

        # ---- 第一层：使用数据库中的前复权数据校验 ----
        collection_map = {"CN": "stock_daily_quotes", "HK": "stock_daily_quotes_hk", "US": "stock_daily_quotes_us"}
        coll_name = collection_map.get(normalized_market, "stock_daily_quotes")
        coll = self.db[coll_name]
        doc = None
        for date_cond in date_conds:
            doc = await coll.find_one(
                {"$or": symbol_conds, **date_cond},
                {"_id": 0, "high": 1, "low": 1, "open": 1, "close": 1}
            )
            if doc:
                break
        # 回退到 etf_daily_quotes（ETF 行情）
        if not doc and normalized_market == "CN":
            for date_cond in date_conds:
                doc = await self.db["etf_daily_quotes"].find_one(
                    {"code": code6, **date_cond},
                    {"_id": 0, "high": 1, "low": 1, "open": 1, "close": 1}
                )
                if doc:
                    break

        if not doc:
            # 数据库无历史数据，尝试 API 不复权兜底
            return await self._validate_with_unadjusted_api(
                code6, normalized_market, buy_date_str, cost_price
            )

        # 提取前复权 high/low
        high_val = doc.get("high")
        low_val = doc.get("low")
        if high_val is None or low_val is None:
            open_val = doc.get("open")
            close_val = doc.get("close")
            if open_val is not None and close_val is not None:
                high_val = max(open_val, close_val)
                low_val = min(open_val, close_val)
            else:
                return None  # 无法校验则放行
        try:
            high_f = float(high_val)
            low_f = float(low_val)
        except (ValueError, TypeError):
            return None

        # 前复权校验通过 — 用户输入的价格与前复权数据一致
        # ⚠️ 容差策略：用户从券商 app 复制的"成本价"通常包含佣金/过户费等交易成本，
        # 不是纯成交价。按 2% 容差放宽上下限，覆盖手续费和滑点影响。
        #   买入成本价 = 成交价 + 佣金 + 过户费（约 0.05%~0.5%）
        #   2% 容差远大于实际手续费比例，既能容错又不会放过明显错误。
        tolerance = 0.02
        high_with_tol = high_f * (1 + tolerance)
        low_with_tol = low_f * (1 - tolerance)
        if low_with_tol <= cost_price <= high_with_tol:
            logger.info(
                f"✅ 成本价 {cost_price:.2f} 通过前复权校验（{buy_date_str}，"
                f"原始范围 {low_f:.2f}~{high_f:.2f}，含 2% 容差 {low_with_tol:.2f}~{high_with_tol:.2f}）"
            )
            return None

        # ---- 第二层：前复权校验失败，尝试不复权 API 兜底 ----
        logger.info(
            f"⚠️ 成本价 {cost_price:.2f} 未通过前复权校验（{buy_date_str}，范围 {low_f:.2f}~{high_f:.2f}），"
            f"尝试不复权数据兜底校验..."
        )
        return await self._validate_with_unadjusted_api(
            code6, normalized_market, buy_date_str, cost_price
        )

    async def _validate_with_unadjusted_api(
        self, code6: str, market: str, buy_date_str: str, cost_price: float
    ) -> Optional[Dict[str, Any]]:
        """
        通过 API 获取不复权数据进行兜底校验，并计算复权因子用于价格转换。

        场景：用户输入的是不复权（实际成交价），而数据库存的是前复权数据。
        例如：不复权成交价 14.11，前复权最高价 13.72（因分红/送股导致差异）。

        Returns:
            None — 不复权校验也失败，拒绝（由调用方抛出 ValueError）
            Dict — 不复权校验通过，返回转换信息：
                {"adjust_type": "none", "qfq_price": float, "adj_factor": float}
        """
        if market != "CN":
            # 目前仅支持 A 股的不复权兜底校验
            raise ValueError(
                f"成本价 {cost_price:.2f} 无法通过校验，非 A 股市场暂不支持不复权兜底校验。"
            )

        try:
            from app.services.data_sources.manager import DataSourceManager
            ds_manager = DataSourceManager()
        except Exception as e:
            logger.warning(f"无法初始化数据源管理器: {e}")
            raise ValueError(
                f"成本价 {cost_price:.2f} 无法通过校验，数据源服务不可用。"
            )

        # 获取不复权日线数据
        unadj_ohlc, unadj_source = ds_manager.get_daily_ohlc_with_fallback(
            code=code6, trade_date=buy_date_str, adj=None
        )
        if not unadj_ohlc:
            raise ValueError(
                f"成本价 {cost_price:.2f} 无法校验：数据库为前复权数据且 API 无法获取不复权数据。"
                f"请检查买入日期是否为交易日，或暂不填写买入日期。"
            )

        unadj_high = unadj_ohlc.get("high")
        unadj_low = unadj_ohlc.get("low")
        if unadj_high is None or unadj_low is None:
            raise ValueError(
                f"成本价 {cost_price:.2f} 无法校验：API 返回的不复权数据缺少最高/最低价。"
            )

        try:
            unadj_high_f = float(unadj_high)
            unadj_low_f = float(unadj_low)
        except (ValueError, TypeError):
            raise ValueError(
                f"成本价 {cost_price:.2f} 无法校验：API 返回的不复权数据格式异常。"
            )

        # 不复权校验
        # ⚠️ 同前复权校验，按 2% 容差放宽上下限，覆盖券商 app 成本价含手续费的情况
        tolerance = 0.02
        unadj_high_with_tol = unadj_high_f * (1 + tolerance)
        unadj_low_with_tol = unadj_low_f * (1 - tolerance)
        if cost_price < unadj_low_with_tol or cost_price > unadj_high_with_tol:
            raise ValueError(
                f"成本价 {cost_price:.2f} 超出 {buy_date_str} 的波动范围"
                f"（不复权最低 {unadj_low_f:.2f}～最高 {unadj_high_f:.2f}，"
                f"含 2% 手续费容差 {unadj_low_with_tol:.2f}~{unadj_high_with_tol:.2f}）。"
                f"券商 app 显示的成本价通常包含佣金/过户费（约 0.05%~0.5%），"
                f"建议改用纯成交价录入。"
            )

        # ---- 不复权校验通过，计算复权因子 ----
        # 获取同日的前复权数据，计算转换比例：前复权 / 不复权
        qfq_ohlc, qfq_source = ds_manager.get_daily_ohlc_with_fallback(
            code=code6, trade_date=buy_date_str, adj="qfq"
        )
        if not qfq_ohlc or qfq_ohlc.get("close") is None or unadj_ohlc.get("close") is None:
            # 无法获取前复权数据，保留不复权价格，标注类型
            logger.warning(
                f"无法获取前复权数据进行转换，成本价 {cost_price:.2f} 将以不复权价格保存"
            )
            return {
                "adjust_type": "none",
                "qfq_price": None,  # 无法转换
                "adj_factor": None,
            }

        # 复权因子 = 前复权收盘价 / 不复权收盘价
        qfq_close = float(qfq_ohlc["close"])
        unadj_close = float(unadj_ohlc["close"])

        if unadj_close <= 0:
            logger.warning(f"不复权收盘价异常（{unadj_close}），无法计算复权因子")
            return {
                "adjust_type": "none",
                "qfq_price": None,
                "adj_factor": None,
            }

        adj_factor = qfq_close / unadj_close
        qfq_price = cost_price * adj_factor

        logger.info(
            f"✅ 成本价 {cost_price:.2f} 通过不复权校验（{buy_date_str}，范围 {unadj_low_f:.2f}~{unadj_high_f:.2f}），"
            f"复权因子={adj_factor:.6f}，转为前复权价格={qfq_price:.2f}"
        )

        return {
            "adjust_type": "none",  # 原始输入为不复权
            "qfq_price": round(qfq_price, 4),
            "adj_factor": round(adj_factor, 6),
        }

    async def _get_stock_name(self, code: str, market: str) -> Optional[str]:
        """获取股票名称（含 ETF 回退）"""
        if market in {"CN", "ETF"}:
            basic = await self.db["stock_basic_info"].find_one(
                {"$or": [{"code": code}, {"symbol": code}]},
                {"_id": 0, "name": 1}
            )
            if basic and basic.get("name"):
                return basic["name"]
            # 回退到 etf_basic_info
            etf = await self.db["etf_basic_info"].find_one(
                {"code": code}, {"_id": 0, "name": 1}
            )
            if etf and etf.get("name"):
                return etf["name"]
        return None

    async def _get_stock_industry(self, code: str, market: str) -> Optional[str]:
        """获取股票所属行业
        
        优先从官网获取（避免 AKShare 轮询封号），其次按数据库数据源优先级查询
        """
        try:
            if market == "CN":
                logger.info(f"🔍 [获取行业] 查询股票代码: {code}, 市场: {market}")

                # 🔥 优先从官网获取行业数据（避免 AKShare 轮询封号）
                from app.services.official_industry_service import get_industry
                official_industry = await get_industry(code)
                if official_industry:
                    logger.info(f"✅ [获取行业] 从官网获取: {code} -> {official_industry}")
                    return official_industry
                
                # 🔥 获取启用的数据源列表（按优先级从高到低排序）
                from app.core.data_source_priority import get_enabled_data_sources_async
                enabled_sources = await get_enabled_data_sources_async(market_category="a_shares")
                logger.info(f"📊 [获取行业] 数据源优先级: {enabled_sources}")
                
                # 尝试多种查询方式
                query_conditions = [
                    {"code": code},
                    {"symbol": code},
                    {"code": code.strip().zfill(6)},  # 补齐前导零
                    {"symbol": code.strip().zfill(6)}
                ]
                
                # 🔥 按数据源优先级顺序查询
                for source in enabled_sources:
                    logger.info(f"🔍 [获取行业] 尝试数据源: {source}")
                    
                    for condition in query_conditions:
                        # 🔥 添加数据源筛选条件
                        condition_with_source = {**condition, "source": source}
                        basic = await self.db["stock_basic_info"].find_one(
                            condition_with_source,
                            {"_id": 0, "industry": 1, "code": 1, "symbol": 1, "name": 1, "source": 1}
                        )
                        
                        if basic:
                            industry = basic.get("industry")
                            code_found = basic.get("code") or basic.get("symbol")
                            name_found = basic.get("name")
                            source_found = basic.get("source")
                            logger.info(f"✅ [获取行业] 使用数据源 {source_found} 和查询条件 {condition} 找到记录")
                            logger.info(f"✅ [获取行业] 股票 {code} 查询结果 - 代码: {code_found}, 名称: {name_found}, 行业: {industry}, 数据源: {source_found}")
                            
                            if industry:
                                logger.info(f"✅ [获取行业] 成功获取行业信息: {industry} (数据源: {source_found})")
                                return industry
                            else:
                                logger.warning(f"⚠️ [获取行业] 股票 {code} 在数据源 {source_found} 中找到记录，但 industry 字段为空，继续尝试下一个数据源")
                                break  # 跳出查询条件循环，尝试下一个数据源
                
                # 🔥 如果所有数据源都没有找到行业信息，记录警告
                logger.warning(f"⚠️ [获取行业] 股票 {code} 在所有数据源中均未找到行业信息")
                # 打印一些调试信息
                count = await self.db["stock_basic_info"].count_documents({})
                logger.info(f"📊 [获取行业] stock_basic_info 集合总记录数: {count}")
                # 查找类似的代码（不限制数据源）
                similar = await self.db["stock_basic_info"].find_one(
                    {"code": {"$regex": code[-4:]}},  # 查找后4位相同的
                    {"_id": 0, "code": 1, "symbol": 1, "name": 1, "source": 1}
                )
                if similar:
                    logger.info(f"🔍 [获取行业] 找到类似的代码: {similar}")
            else:
                logger.info(f"🔍 [获取行业] 市场 {market} 暂不支持行业查询")
        except Exception as e:
            logger.error(f"❌ [获取行业] 获取股票 {code} 行业信息失败: {e}", exc_info=True)
        return None
