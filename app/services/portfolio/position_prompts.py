"""PortfolioService.PositionPromptMixin。"""
from app.services.portfolio.support import (
    datetime,
    Dict,
    Any,
    Optional,
    truncate_api_key,
    _use_template_prompts,
    PositionAction,
    PositionSnapshot,
    PositionAnalysisRequest,
    PositionAnalysisResult,
    logger,
)

class PositionPromptMixin:

    async def _call_position_ai_analysis_v2(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest,
        stock_analysis_report: Dict[str, Any],
        user_id: Optional[str] = None
    ) -> PositionAnalysisResult:
        """调用AI进行单股持仓分析 - 方案2

        基于完整的单股分析报告 + 持仓信息，生成个性化持仓研究观察
        使用系统配置的深度分析模型
        """
        import httpx

        try:
            # 构建持仓分析专用提示词
            prompt = self._build_position_analysis_prompt_v2(
                snapshot=snapshot,
                params=params,
                stock_analysis_report=stock_analysis_report,
                user_id=user_id
            )

            from app.services.llm_model_resolution import get_provider_and_url_by_model_sync
            from app.services.model_capability_service import get_model_capability_service
            from tradingagents.graph.trading_graph import create_llm_by_provider

            # 使用系统配置的深度分析模型
            capability_service = get_model_capability_service()
            _, deep_model = capability_service._get_default_models()
            model_name = deep_model

            logger.info(f"🤖 [持仓AI分析] 从系统配置获取深度分析模型: {model_name}")

            # 从数据库获取模型的完整配置（包括 API URL 和 API Key）
            provider_info = get_provider_and_url_by_model_sync(model_name)

            base_url = provider_info.get("backend_url")
            api_key = provider_info.get("api_key")
            provider = provider_info.get("provider")

            logger.info(f"🤖 [持仓AI分析] 使用模型: {model_name}")
            logger.info(f"🏭 [持仓AI分析] 模型供应商: {provider}")
            logger.info(f"🔗 [持仓AI分析] API地址: {base_url}")
            logger.info(f"🔑 [持仓AI分析] API Key: {'已配置' if api_key else '未配置'}")

            if not api_key:
                logger.warning("⚠️ [持仓AI分析] API Key 未配置，将尝试从环境变量获取")

            if not base_url:
                logger.error("❌ [持仓AI分析] API地址未配置")
                return PositionAnalysisResult(
                    action=PositionAction.HOLD,
                    action_reason="AI模型未正确配置，请在设置中配置深度分析模型的API地址"
                )

            # 使用统一的 LLM 适配器
            logger.info(f"🔧 [持仓AI分析v2] 准备创建 LLM: provider={provider}, model={model_name}, temperature=0.3")
            if api_key:
                logger.info(f"🔑 [持仓AI分析v2] API Key: {truncate_api_key(api_key)}")
            llm = create_llm_by_provider(
                provider=provider,
                model=model_name,
                backend_url=base_url,
                temperature=0.3,
                max_tokens=4000,
                timeout=120,
                api_key=api_key
            )

            logger.info(f"📤 [持仓AI分析] 开始调用 LLM...")
            response = await llm.ainvoke(prompt)

            # 记录 token 使用
            try:
                from app.services.usage_statistics_service import usage_statistics_service
                await usage_statistics_service.record_llm_usage(
                    response=response,
                    session_id=user_id or "portfolio",
                    analysis_type="portfolio",
                    stock_code=snapshot.code
                )
            except Exception as e:
                logger.warning(f"记录 token 使用失败: {e}")

            content = response.content if hasattr(response, 'content') else str(response)
            logger.info(f"📥 [持仓AI分析] LLM 响应成功，内容长度: {len(content)}")

            return self._parse_position_ai_response_v2(content, snapshot)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ [持仓AI分析] 分析失败: {error_msg}", exc_info=True)
            from app.utils.error_formatter import ErrorFormatter

            action_reason = f"AI分析暂时不可用：{ErrorFormatter.format_user_message(error_msg, include_suggestion=False)}"

            return PositionAnalysisResult(
                action=PositionAction.HOLD,
                action_reason=action_reason
            )

    async def _call_position_ai_analysis(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest
    ) -> PositionAnalysisResult:
        """调用AI进行单股持仓分析 (旧版本，保留兼容)"""
        try:
            prompt = self._build_position_analysis_prompt(snapshot, params)

            from app.services.llm_model_resolution import get_provider_and_url_by_model_sync
            from tradingagents.graph.trading_graph import create_llm_by_provider

            model_name = "qwen-turbo"
            provider_info = get_provider_and_url_by_model_sync(model_name)

            # 使用统一的 LLM 适配器
            llm = create_llm_by_provider(
                provider=provider_info.get("provider", "dashscope"),
                model=model_name,
                backend_url=provider_info.get("backend_url"),
                temperature=0.3,
                max_tokens=4000,
                timeout=120,
                api_key=provider_info.get("api_key")
            )

            response = await llm.ainvoke(prompt)

            # 记录 token 使用
            try:
                from app.services.usage_statistics_service import usage_statistics_service
                await usage_statistics_service.record_llm_usage(
                    response=response,
                    session_id="portfolio",
                    analysis_type="portfolio",
                    stock_code=snapshot.code
                )
            except Exception as e:
                logger.warning(f"记录 token 使用失败: {e}")

            content = response.content if hasattr(response, 'content') else str(response)
            return self._parse_position_ai_response(content, snapshot)

        except Exception as e:
            logger.error(f"单股AI分析失败: {e}", exc_info=True)
            from app.utils.error_formatter import ErrorFormatter

            return PositionAnalysisResult(
                action=PositionAction.HOLD,
                action_reason=f"AI分析暂时不可用：{ErrorFormatter.format_user_message(str(e), include_suggestion=False)}"
            )

    def _build_position_analysis_prompt(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest
    ) -> str:
        """构建单股持仓分析提示词"""
        pnl_status = "盈利" if (snapshot.unrealized_pnl or 0) >= 0 else "亏损"

        prompt = f"""# 单股持仓分析任务

## 持仓信息
- 股票代码: {snapshot.code}
- 股票名称: {snapshot.name or '未知'}
- 持仓数量: {snapshot.quantity} 股
- 成本价: {snapshot.cost_price:.2f} 元
- 当前价: {snapshot.current_price or '未知'} 元
- 持仓市值: {snapshot.market_value or '未知'} 元
- 浮动盈亏: {snapshot.unrealized_pnl or 0:.2f} 元 ({snapshot.unrealized_pnl_pct or 0:.2f}%)
- 持仓天数: {snapshot.holding_days or '未知'} 天
- 当前状态: {pnl_status}

## 用户设置
- 目标收益率: {params.target_profit_pct}%
- 是否考虑加仓: {'是' if params.include_add_position else '否'}

## 分析要求
请从以下维度分析该持仓，用中文回答：

1. **研究结论倾向** (必须是以下之一: 乐观/审慎/中性，不得输出加仓/减仓/持有/清仓等交易指令)
2. **研究观察理由** (100字以内)
3. **置信度** (0-100分)
4. **风险关注价格** (具体价格，用于用户自行跟踪下行风险)
5. **收益关注价格** (具体价格，用于用户自行跟踪上行观察价位)
6. **风险审阅** (300-500字，需详细说明主要风险点、风险等级、风险影响等)
7. **机会评估** (300-500字，需详细说明潜在机会、催化剂、收益空间等，但不得给出买入/卖出建议)
8. **详细分析** (200字以内的完整分析)

请直接给出分析结果。整体输出仅供学习研究参考，不构成投资建议。"""

        return prompt

    def _build_position_analysis_prompt_v2(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest,
        stock_analysis_report: Dict[str, Any],
        user_id: Optional[str] = None
    ) -> str:
        """构建持仓分析专用提示词 - 方案2

        基于完整的单股分析报告 + 持仓信息，生成个性化持仓研究观察
        支持模板系统（通过 USE_TEMPLATE_PROMPTS 特性开关控制）
        """
        # 先构建硬编码版本作为降级兜底
        legacy_prompt = self._build_legacy_position_prompt(snapshot, params, stock_analysis_report)

        # 如果启用模板系统，尝试使用模板生成提示词
        if _use_template_prompts():
            try:
                from tradingagents.utils.template_client import get_agent_prompt

                variables = self._build_position_vars(snapshot, params, stock_analysis_report)

                prompt = get_agent_prompt(
                    agent_type="trader",
                    agent_name="position_advisor",
                    variables=variables,
                    user_id=user_id,
                    preference_id=params.invest_style or "neutral",
                    fallback_prompt=legacy_prompt
                )

                if prompt and prompt != legacy_prompt:
                    logger.info(f"✅ [持仓分析] 使用模板系统生成提示词 (长度: {len(prompt)})")
                    return prompt

            except Exception as e:
                logger.warning(f"⚠️ [持仓分析] 模板系统调用失败，使用硬编码提示词: {e}")

        return legacy_prompt

    def _build_legacy_position_prompt(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest,
        stock_analysis_report: Dict[str, Any]
    ) -> str:
        """构建硬编码持仓分析提示词（旧版，作为降级兜底）"""
        pnl_status = "盈利" if (snapshot.unrealized_pnl or 0) >= 0 else "亏损"
        pnl_pct = snapshot.unrealized_pnl_pct or 0

        # 提取各分析师报告
        reports = stock_analysis_report.get("reports", {})
        decision = stock_analysis_report.get("decision", {})
        summary = stock_analysis_report.get("summary", "")
        recommendation = stock_analysis_report.get("recommendation", "")

        # 构建分析报告摘要
        market_report = reports.get("market_report", "")[:1500] if reports.get("market_report") else "暂无"
        fundamentals_report = reports.get("fundamentals_report", "")[:1500] if reports.get("fundamentals_report") else "暂无"
        news_report = reports.get("news_report", "")[:1000] if reports.get("news_report") else "暂无"

        # 提取研究结论倾向
        trade_action = decision.get("action", "未知") if decision else "未知"
        target_price = decision.get("target_price") if decision else None

        prompt = f"""# 持仓分析专家任务

你是一位专业的持仓研究助理，需要基于完整的股票分析报告和用户的持仓情况，给出个性化的持仓研究观察（非交易建议）。

## 📊 股票分析报告摘要

### 技术面分析
{market_report}

### 基本面分析
{fundamentals_report}

### 新闻面分析
{news_report}

### 综合研究
- 摘要: {summary or '暂无'}
- 研究结论倾向: {trade_action}
- 关键验证价位: {target_price or '未知'}
- 详细研究观察: {recommendation or '暂无'}

---

## 💼 用户持仓信息

| 项目 | 数值 |
|------|------|
| 股票代码 | {snapshot.code} |
| 股票名称 | {snapshot.name or '未知'} |
| 持仓数量 | {snapshot.quantity} 股 |
| 成本价 | {snapshot.cost_price:.2f} 元 |
| 当前价 | {snapshot.current_price or '未知'} 元 |
| 持仓市值 | {snapshot.market_value or '未知':.2f} 元 |
| 浮动盈亏 | {snapshot.unrealized_pnl or 0:.2f} 元 ({pnl_pct:.2f}%) |
| 持仓天数 | {snapshot.holding_days or '未知'} 天 |
| 当前状态 | {pnl_status} |
| 行业 | {snapshot.industry or '未知'} |

## 🎯 用户投资目标
- 目标收益率: {params.target_profit_pct}%
- 是否考虑加仓: {'是' if params.include_add_position else '否'}
{self._build_capital_info_section(snapshot, params)}

---

## 📝 分析要求

请综合以上股票分析报告和持仓信息，给出个性化的持仓研究观察（非交易建议）。

**重要考量因素**：
1. 当前盈亏状态：{pnl_status} {pnl_pct:.2f}%
2. 与目标收益率的差距：目标 {params.target_profit_pct}%，当前 {pnl_pct:.2f}%
3. 股票分析报告中的研究结论倾向：{trade_action}
4. 技术面、基本面、新闻面的综合判断

**请以JSON格式输出**：
```json
{{
    "action": "乐观|审慎|中性",
    "action_reason": "研究观察的核心理由，需要结合分析报告和持仓情况说明（100字以内）",
    "confidence": 0-100的整数,
    "stop_loss_price": 风险关注价格（数字，保留2位小数）,
    "take_profit_price": 收益关注价格（数字，保留2位小数）,
    "risk_assessment": "基于分析报告的详细风险审阅（300-500字，需包含主要风险点、风险等级、风险影响等）",
    "opportunity_assessment": "基于分析报告的详细机会评估（300-500字，需包含潜在机会、催化剂、收益空间等，但不得给出买入/卖出建议）",
    "detailed_analysis": "结合分析报告和持仓情况的详细分析（200字以内）",
    "suggested_quantity": 0,
    "suggested_amount": 0,
    "position_risk_analysis": "仓位风险分析（如提供了资金总量，分析仓位占比是否合理，50字以内，可选，但不得给出加仓/减仓建议）"
}}
```

**注意事项**：
- 风险关注价格应低于当前价，用于用户自行跟踪下行风险
- 收益关注价格应高于当前价，用于用户自行跟踪上行观察价位
- 所有价格保留2位小数
- 必须基于提供的分析报告进行判断，不要凭空编造信息
- 如果用户提供了资金总量，可在分析中说明仓位占比观察，但不得给出加仓/减仓/清仓等交易指令
- 整体输出仅供学习研究参考，不构成投资建议
"""

        return prompt

    def _build_capital_info_section(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest
    ) -> str:
        """构建资金信息部分的提示词"""
        if not params.total_capital or params.total_capital <= 0:
            return ""

        # 计算各项风险指标
        market_value = snapshot.market_value or (snapshot.quantity * (snapshot.current_price or snapshot.cost_price))
        position_pct = (market_value / params.total_capital) * 100 if params.total_capital > 0 else 0

        # 计算最大可能亏损（假设跌到止损线）
        stop_loss_pct = params.max_loss_pct  # 默认10%
        max_loss_amount = market_value * (stop_loss_pct / 100)
        max_loss_impact_pct = (max_loss_amount / params.total_capital) * 100

        # 计算可加仓金额（基于最大仓位限制）
        max_position_value = params.total_capital * (params.max_position_pct / 100)
        available_add_amount = max(0, max_position_value - market_value)

        # 判断风险等级
        risk_level = "low"
        if position_pct > params.max_position_pct:
            risk_level = "critical"
        elif position_pct > params.max_position_pct * 0.8:
            risk_level = "high"
        elif position_pct > params.max_position_pct * 0.5:
            risk_level = "medium"

        section = f"""
## 💰 资金与仓位分析

| 项目 | 数值 | 说明 |
|------|------|------|
| 投资资金总量 | {params.total_capital:,.0f} 元 | 用户设置的总投资资金 |
| 当前持仓市值 | {market_value:,.2f} 元 | 该股票的持仓市值 |
| 当前仓位占比 | {position_pct:.2f}% | 占总资金比例 |
| 最大仓位限制 | {params.max_position_pct:.0f}% | 单只股票最大仓位 |
| 仓位风险等级 | {risk_level} | low/medium/high/critical |

### 风险敞口分析
- 如果该股票下跌 {stop_loss_pct:.0f}%（止损线），最大亏损金额: {max_loss_amount:,.2f} 元
- 最大亏损对总资金影响: {max_loss_impact_pct:.2f}%
- 用户可接受最大亏损比例: {params.max_loss_pct:.0f}%

### 加仓空间分析
- 最大允许持仓市值: {max_position_value:,.0f} 元
- 当前已占用: {market_value:,.2f} 元
- 剩余可加仓金额: {available_add_amount:,.0f} 元
- 剩余可加仓数量（按当前价）: {int(available_add_amount / (snapshot.current_price or snapshot.cost_price))} 股

**⚠️ 重要提醒**：
- 仓位风险等级为 **{risk_level}**
- {'仓位已超限！建议减仓' if risk_level == 'critical' else '仓位接近上限，谨慎加仓' if risk_level == 'high' else '仓位合理' if risk_level == 'medium' else '仓位较轻，有加仓空间'}
"""
        return section

    def _build_position_vars(
        self,
        snapshot: PositionSnapshot,
        params: PositionAnalysisRequest,
        stock_analysis_report: Dict[str, Any]
    ) -> Dict[str, Any]:
        """构建模板变量（用于模板系统）

        根据设计文档 portfolio-trade-review-engine-integration.md 中定义的变量规范
        """
        pnl_pct = snapshot.unrealized_pnl_pct or 0

        # 提取分析报告
        reports = stock_analysis_report.get("reports", {})
        decision = stock_analysis_report.get("decision", {})

        # 计算仓位信息
        market_value = snapshot.market_value or (snapshot.quantity * (snapshot.current_price or snapshot.cost_price))
        position_pct = 0.0
        position_risk_level = "unknown"

        if params.total_capital and params.total_capital > 0:
            position_pct = (market_value / params.total_capital) * 100
            if position_pct > params.max_position_pct:
                position_risk_level = "critical"
            elif position_pct > params.max_position_pct * 0.8:
                position_risk_level = "high"
            elif position_pct > params.max_position_pct * 0.5:
                position_risk_level = "medium"
            else:
                position_risk_level = "low"

        return {
            # 持仓信息
            "position": {
                "code": snapshot.code,
                "name": snapshot.name or "未知",
                "quantity": snapshot.quantity,
                "cost_price": f"{snapshot.cost_price:.2f}",
                "current_price": f"{snapshot.current_price:.2f}" if snapshot.current_price else "未知",
                "market_value": f"{market_value:.2f}",
                "holding_days": snapshot.holding_days or "未知",
                "unrealized_pnl": f"{snapshot.unrealized_pnl or 0:.2f}",
                "unrealized_pnl_pct": f"{pnl_pct:.2f}",
                "industry": snapshot.industry or "未知"
            },

            # 账户与仓位
            "capital": {
                "total_assets": f"{params.total_capital:,.0f}" if params.total_capital else "未提供",
                "position_pct": f"{position_pct:.2f}",
                "position_risk_level": position_risk_level,
                "max_position_pct": f"{params.max_position_pct:.0f}"
            },

            # 用户设置
            "goal": {
                "target_profit_pct": f"{params.target_profit_pct}",
                "max_loss_pct": f"{params.max_loss_pct}",
                "include_add_position": "是" if params.include_add_position else "否"
            },

            # 单股分析报告
            "engine": {
                "market_report": (reports.get("market_report", "") or "暂无")[:1500],
                "fundamentals_report": (reports.get("fundamentals_report", "") or "暂无")[:1500],
                "news_report": (reports.get("news_report", "") or "暂无")[:1000],
                "sentiment_report": (reports.get("sentiment_report", "") or "暂无")[:500],
                "sector_report": (reports.get("sector_report", "") or "暂无")[:500],
                "final_decision": {
                    "action": decision.get("action", "未知") if decision else "未知",
                    "target_price": str(decision.get("target_price", "未知")) if decision else "未知",
                    "reason": decision.get("reason", stock_analysis_report.get("recommendation", "暂无"))[:500]
                }
            },

            # 上下文
            "context": {
                "analysis_date": datetime.now().strftime("%Y-%m-%d"),
                "user_style": params.invest_style or "neutral"
            }
        }
