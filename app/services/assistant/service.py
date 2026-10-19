"""智能助手：service。"""
import asyncio
import inspect
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from bson import ObjectId

# 智能助手整体超时（LLM + 多轮工具调用）。
# 设计对齐主流 Agent（ChatGPT/Manus 等）：agent 循环不设短的全局超时，
# 依靠「单步超时（工具 tier 15/180s）+ 轮次上限（max_tool_rounds=8）+
# 流式进度反馈 + 用户手动停止」控制时长；全局超时只是防挂死的安全网。
# 实测：推理模型（长思考）+ 277 工具 schema + 长会话历史，首轮 LLM 思考
# 可达 4 分钟，300s 会在工具刚执行完就被掐断（2026-09-15 营收预测导出案例）。
ASSISTANT_TIMEOUT_SECONDS = int(os.environ.get("ASSISTANT_TIMEOUT_SECONDS", "900"))
# 当对话中使用了重型工具（Agent 调用等）时，额外追加的超时余量
ASSISTANT_HEAVY_TOOL_EXTRA_SECONDS = 120

# 会话历史最大条数（每用户）
ASSISTANT_CONVERSATION_LIMIT = 100
ASSISTANT_DEFAULT_THREAD_ID = "default"
ASSISTANT_MAX_CONTEXT_MESSAGES = 10
ASSISTANT_MAX_REPORT_REFS = 12
ASSISTANT_REPORT_INBOX_TITLE = "报告收纳"

from core.tools import get_tool_registry, tool_metadata_list_to_openai
from core.tools.context import set_current_assistant_thread_id, set_current_user_id
from core.llm import UnifiedLLMClient
from core.llm.models import LLMConfig, LLMProvider, Message, MessageRole, ToolCall, ToolResult

# A11 智能助手质量架构：回复质量防线（编造检测/数字溯源）与契约型唯一出口。
# 防线实现已收编至 core.assistant.defense（单一事实源），此处统一 import 复用；
# 所有路径回复经 ReplyGate 出口，证据经 Evidence/ReplyCandidate 携带，data_refs 真实现。
from core.assistant import (
    EVIDENCE_MEMORY,
    EVIDENCE_SEARCH,
    EVIDENCE_TOOL,
    AssistantRequest,
    Evidence,
    PathRegistry,
    PRIORITY_DATA_PRECHECK,
    PRIORITY_FAST_SEARCH,
    PRIORITY_HITL,
    PRIORITY_LLM_UNAVAILABLE,
    PRIORITY_MAIN_LLM,
    PRIORITY_UNSUPPORTED_ASSET,
    ReplyCandidate,
    ReplyGate,
    ROUTE_DATA_PRECHECK,
    ROUTE_ERROR,
    ROUTE_FAST_SEARCH,
    ROUTE_HITL,
    ROUTE_LLM_UNAVAILABLE,
    ROUTE_MAIN_LLM,
    ROUTE_TIMEOUT,
    ROUTE_TYPE_PASSTHROUGH,
    ROUTE_TYPE_ANSWER,
    ROUTE_UNSUPPORTED_ASSET,
    detect_unsupported_asset,
    extract_a_share_code,
    extract_data_refs,
    quality_metrics,
    stock_directory,
)
from core.assistant.reply_gate import build_fallback_candidate
from core.assistant.defense import (
    _SPECIFIC_NUMBER_RE,
    _UNSOURCED_RETRY_PROMPT,
    _UNTRACED_RETRY_PROMPT_TEMPLATE,
    _build_data_transparency_note,
    _get_untraced_risky_numbers,
    _has_fabricated_tool_json,
    _has_fabricated_tool_refs,
    _has_unsourced_numbers,
    _has_untraced_risky_numbers,
)

logger = logging.getLogger("app.services.intelligent_assistant_service")

_MEMORY_FACT_KEYWORDS = [
    "结论", "营收", "收入", "净利润", "扣非", "毛利率", "现金流", "分红", "股息率", "估值",
    "预期", "超预期", "低于预期", "价格战", "成本", "销量", "增长", "下滑", "风险", "压力",
    "支撑", "阻力", "走势", "震荡", "偏弱", "偏强", "回调", "反弹", "逻辑", "原因",
]

_NUMBER_ONLY_REPLY_RE = re.compile(r"^\s*([1-9]\d?)\s*[\.、\)）]?\s*$")
_NUMBERED_OPTION_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[\.、\)）]\s*(.+?)\s*$")



class IntelligentAssistantService:
    """智能助手服务"""

    def __init__(
        self,
        db,
        preferred_models: Optional[Dict[str, Any]] = None,
        prefer_reasoning_model: bool = False,
        user_id: str = "",
        assistant_role: str = "general",
        user_context: Optional[dict] = None,
    ):
        self._db = db
        self._llm_client: Optional[UnifiedLLMClient] = None
        self._openai_tools: Optional[List[Dict[str, Any]]] = None
        self._tool_functions: Optional[Dict[str, Any]] = None
        self._preferred_model = _get_preferred_assistant_model(preferred_models)
        self._preferred_model_config_id = _get_preferred_assistant_config_id(preferred_models)
        self._prefer_reasoning_model = prefer_reasoning_model
        self._user_id = user_id  # 用于构造带 user_id 闭包的元工具（如 remember_this）
        # v3.0 新增：使用问答机器人角色相关字段
        self._assistant_role = assistant_role or "general"
        self._user_context = user_context or {}

    async def _ensure_client(self) -> bool:
        """确保 LLM 客户端和工具已加载"""
        if self._llm_client is not None:
            return True

        llm_config = await _get_assistant_llm_config(
            self._db,
            use_reasoning_model=self._prefer_reasoning_model,
            preferred_models=_build_assistant_model_preferences(
                model=self._preferred_model,
                model_config_id=self._preferred_model_config_id,
            ) if (self._preferred_model or self._preferred_model_config_id) else None,
        )
        if not llm_config:
            return False

        try:
            self._llm_client = UnifiedLLMClient.from_config(llm_config)
        except Exception as e:
            logger.error(f"[智能助手] 创建 LLM 客户端失败: {e}", exc_info=True)
            return False

        registry = get_tool_registry()
        all_metadata = registry.list_all()

        # v3.0 新增：使用问答机器人只注册 9 个系统感知工具
        # 不加载金融市场数据工具、不加载分析师 Agent 工具
        if self._assistant_role == "usage_helper":
            allowed_tool_ids = {
                "get_user_current_state",
                "get_system_status",
                "get_user_recent_activity",
                "read_user_manual_section",
                "search_user_manual",  # 向量语义检索（v3.0.1 新增，解决自然语言问题匹配）
                "get_data_sync_status",
                "get_paper_trading_status",
                "get_token_usage",
                "get_running_tasks",
            }
            all_metadata = [t for t in all_metadata if t.id in allowed_tool_ids]
            logger.info(f"[智能助手·使用问答] 仅加载 {len(all_metadata)} 个系统感知工具（含向量检索/数据同步/模拟交易/Token用量/任务进度）")

        self._openai_tools = tool_metadata_list_to_openai(all_metadata)

        self._tool_functions = {}
        for tool in all_metadata:
            # 无参数工具同样需要注入，否则会出现"工具已加载但无法执行"
            if tool.fc_enabled:
                func = registry.get_function(tool.id)
                if func:
                    self._tool_functions[tool.id] = func

        # 注入 `remember_this` 元工具：允许用户主动让助手记住偏好
        # 参考：docs/05-design/v3.0/unified-memory-layer-mem0.md §15.5 P0-1
        # 使用问答机器人不注入 remember_this（避免污染用户长期记忆）
        if self._user_id and self._assistant_role != "usage_helper":
            self._openai_tools.append(_build_remember_this_tool_def())
            self._tool_functions[REMEMBER_THIS_TOOL_NAME] = _make_remember_this_callable(
                self._db, self._user_id,
            )

        self._llm_client.inject_tools(self._tool_functions)
        logger.info(f"[智能助手] 已加载 {len(self._openai_tools)} 个工具")
        return True

    async def _summarize_search_result(
        self,
        tool_text: str,
        user_message: str,
        history_messages: Optional[List[Message]] = None,
        conversation_id: str = "",
    ) -> str:
        """近期搜索快路径：将搜索结果交由 LLM 做针对性总结（chat/chat_stream 共用）。

        v3.x 修复：快路径原实现把搜索工具返回值直接透传给用户，导致
        "提问收到一堆搜索链接列表"。现在搜索结果注入 LLM 总结回答，并过
        数字溯源防线；返回空串表示降级（调用方回退为返回原始搜索结果）。
        """
        summarize_messages = [
            Message(
                role=MessageRole.SYSTEM,
                content=(
                    "你是 TradingAgents-CN 智能助手。系统已根据用户问题执行联网搜索，"
                    "搜索结果见下方【联网搜索返回】。请严格依据该材料回答用户问题：\n"
                    "1. 直接回答用户问的内容并提炼要点，不要罗列链接、不要照搬搜索结果的列表格式；\n"
                    "2. 只使用搜索结果中出现的信息，材料里没有的不要编造；材料不足以回答的部分要明确说明；\n"
                    "3. 涉及数据/时间的信息注明来源与日期，注意时效（较早的信息要提示发布时间）；\n"
                    "4. 区分官方确证与媒体报道口径，不要把媒体报道说成官方确认；\n"
                    "5. 涉及投资判断时提醒不构成投资建议即可，无需长篇免责声明。"
                ),
            ),
            Message(
                role=MessageRole.SYSTEM,
                content=f"【联网搜索返回】\n{tool_text}",
            ),
        ]
        if history_messages:
            summarize_messages.extend(history_messages)
        summarize_messages.append(
            Message(role=MessageRole.USER, content=user_message.strip())
        )
        try:
            resp = await self._llm_client.achat(summarize_messages)
            await _record_assistant_token_usage(resp, conversation_id or "assistant_chat")
            summarized = (getattr(resp, "content", "") or "").strip()
        except Exception as sum_err:
            logger.warning(f"[智能助手] 搜索结果 LLM 总结失败，降级返回原始结果: {sum_err}")
            return ""
        if not summarized:
            return ""
        # 数字溯源防线：总结中的数字必须能在搜索结果原文中溯源
        fast_tool_results = [{"tool": "search_recent_information", "content": tool_text}]
        if _has_fabricated_tool_json(summarized) or _has_untraced_risky_numbers(
            summarized,
            fast_tool_results,
            ["search_recent_information"],
            user_message,
            [],
        ):
            logger.warning("[智能助手] 快路径总结未过溯源校验，降级返回原始搜索结果")
            return ""
        return summarized

    def _build_tool_interceptor(self, conversation_id: str):
        """构建 HITL 工具拦截器（v3.6.0：提取为类方法，供 chat/chat_stream 共用）。

        对关键操作（触发分析/删除配置等）先保存 pending 到 MongoDB，
        让用户确认后再执行，避免 LLM 在流式/非流式路径下绕过确认直接执行。
        """
        async def _intercept(tool_call) -> Optional[Any]:
            from core.hitl.assistant_hitl import (
                is_critical_tool,
                save_pending_critical_action,
                build_pending_confirmation_reply,
            )

            tool_id = tool_call.name
            if not is_critical_tool(tool_id):
                return None

            pending_id = await save_pending_critical_action(
                self._db,
                user_id=self._user_id or "default",
                conversation_id=conversation_id or "default",
                tool_id=tool_id,
                tool_arguments=tool_call.arguments,
            )
            if not pending_id:
                # 保存失败时降级为直接执行
                return None

            confirmation_msg = build_pending_confirmation_reply(
                tool_id=tool_id,
                tool_arguments=tool_call.arguments,
                pending_id=pending_id,
            )
            # 返回 ToolResult（兼容统一 LLM 客户端）
            from core.llm.unified_client import ToolResult
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_id,
                content=confirmation_msg,
                is_error=False,
            )
        return _intercept

    # ─────────────────────────────────────────────────────────────────────
    # A11 阶段3：统一路由管道。chat()（JSON）与 chat_stream()（SSE）只是
    # 同一套「路径注册表 → ReplyGate → 结果」管道的两个输出适配器，
    # 不再各自维护 HITL/快路径/预检查/工具循环/质量防线的平行实现（S2）。
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _effective_timeout(tools_used: List[str]) -> int:
        """根据已调用工具的 tier 动态计算总超时（两种模式共用）。"""
        base = ASSISTANT_TIMEOUT_SECONDS
        if not tools_used:
            return base
        try:
            from core.tools import get_tool_registry
            from core.tools.config import ToolTimeoutTier
            registry = get_tool_registry()
            for name in tools_used:
                meta = registry.get(name)
                if meta and getattr(meta, "timeout_tier", "") == ToolTimeoutTier.HEAVY:
                    return base + ASSISTANT_HEAVY_TOOL_EXTRA_SECONDS
        except Exception:
            pass
        return base

    async def _route_candidate(self, req: AssistantRequest) -> ReplyCandidate:
        """按显式优先级装配并执行路径注册表；首个命中的路径胜出。"""
        registry = (
            PathRegistry()
            .register(
                ROUTE_HITL, PRIORITY_HITL, self._path_hitl,
                route_type=ROUTE_TYPE_PASSTHROUGH, description="HITL 确认/拒绝待执行操作",
            )
            .register(
                ROUTE_UNSUPPORTED_ASSET, PRIORITY_UNSUPPORTED_ASSET, self._path_unsupported_asset,
                route_type=ROUTE_TYPE_PASSTHROUGH, description="不支持品种拦截",
            )
            .register(
                ROUTE_LLM_UNAVAILABLE, PRIORITY_LLM_UNAVAILABLE, self._path_llm_unavailable,
                route_type=ROUTE_TYPE_PASSTHROUGH, description="LLM 客户端不可用",
            )
            .register(
                ROUTE_FAST_SEARCH, PRIORITY_FAST_SEARCH, self._path_fast_search,
                route_type=ROUTE_TYPE_ANSWER, description="近期搜索快路径（LLM 总结）",
            )
            .register(
                ROUTE_DATA_PRECHECK, PRIORITY_DATA_PRECHECK, self._path_data_precheck,
                route_type=ROUTE_TYPE_PASSTHROUGH, description="数据同步预检查",
            )
            .register(
                ROUTE_MAIN_LLM, PRIORITY_MAIN_LLM, self._path_main_llm,
                route_type=ROUTE_TYPE_ANSWER, description="LLM + 工具循环主路径",
            )
        )
        return await registry.resolve(req)

    async def _path_hitl(
        self, req: AssistantRequest
    ) -> Optional[ReplyCandidate]:
        """路径1：HITL 待确认操作的确认/拒绝解析（passthrough 登记路径）。"""
        try:
            from core.hitl.assistant_hitl import try_resolve_pending_action
            resolution = await try_resolve_pending_action(
                self._db,
                user_id=self._user_id or "default",
                conversation_id=req.conversation_id or "default",
                user_message=req.user_message,
            )
        except Exception as e:
            logger.warning("[智能助手] HITL 检查失败（降级为正常对话）: %s", e)
            return None
        if not resolution:
            return None
        if resolution.get("type") == "confirmed":
            result_text = resolution.get("result", "") or "操作已执行完成"
            return ReplyCandidate.passthrough(
                reply=f"✅ 已确认执行：{result_text}",
                route_id=ROUTE_HITL,
                tools_used=[resolution.get("tool_id", "")],
            )
        if resolution.get("type") == "rejected":
            return ReplyCandidate.passthrough(
                reply=resolution.get("message", "已取消操作"),
                route_id=ROUTE_HITL,
            )
        return None

    async def _path_unsupported_asset(
        self, req: AssistantRequest
    ) -> Optional[ReplyCandidate]:
        """路径2：不支持品种类型（港股/美股等）系统提示（词表事实源：lexicon）。"""
        message = detect_unsupported_asset(req.user_message)
        if not message:
            return None
        return ReplyCandidate.passthrough(
            reply=message, route_id=ROUTE_UNSUPPORTED_ASSET
        )

    async def _path_llm_unavailable(
        self, req: AssistantRequest
    ) -> Optional[ReplyCandidate]:
        """路径3：LLM 客户端无法初始化（配置缺失等）。"""
        if await self._ensure_client():
            return None
        logger.warning("[智能助手] LLM 客户端创建失败")
        return ReplyCandidate.passthrough(
            reply="智能助手暂时不可用，请检查 LLM 配置是否已在「设置」中正确配置。",
            route_id=ROUTE_LLM_UNAVAILABLE,
        )

    async def _path_fast_search(
        self, req: AssistantRequest
    ) -> Optional[ReplyCandidate]:
        """路径4："最新/近期"类事实问题 → 近期搜索 + LLM 针对性总结（answer 型）。

        LLM 总结成功才允许标 llm_processed；失败时由 gate 契约拦截并降级为
        "局限说明+证据摘录"，两种模式都不会再裸透传搜索链接列表。
        """
        if not _should_use_recent_search(req.user_message):
            return None
        # 专用外部 Skill 能结构化承接的问题（如车型销量库），快路径必须让路——
        # 否则「最近+变化」关键词组合会在 LLM 工具选择之前把问题劫持去网页搜索，
        # 专用工具连出场机会都没有。匹配基于注册表元数据，不写死具体 Skill。
        if _matches_generated_skill(req.user_message):
            logger.info("[智能助手] 命中近期搜索关键词但存在专用 Skill，转常规工具选择路径")
            return None
        try:
            from core.tools.implementations.assistant_ops.recent_search import (
                run_recent_information_search,
            )

            tool_result = run_recent_information_search(req.user_message.strip())
            if inspect.isawaitable(tool_result):
                tool_result = await tool_result
            tool_text = str(tool_result or "").strip()
            if not tool_text:
                return None
            logger.info("[智能助手] 命中近期搜索快路径，搜索结果交由 LLM 总结回答")
            summarized = await self._summarize_search_result(
                tool_text, req.user_message, req.history_messages, req.conversation_id,
            )
            return ReplyCandidate.answer(
                reply=summarized or tool_text,
                route_id=ROUTE_FAST_SEARCH,
                llm_processed=bool(summarized),
                tools_used=["search_recent_information"],
                evidence=[
                    Evidence(
                        source_type=EVIDENCE_SEARCH,
                        source_id="search_recent_information",
                        content=tool_text,
                    )
                ],
            )
        except Exception as e:
            logger.warning("[智能助手] 近期搜索快路径失败，回退常规对话: %s", e)
            return None

    async def _build_data_precheck_message(self, user_message: str) -> Optional[str]:
        """数据同步预检查：数据不充分时返回引导文案，充分/不相关返回 None。"""
        stock_code = _extract_stock_code(user_message)
        if not stock_code:
            text_lower = user_message.lower()
            if any(k in text_lower for k in _STOCK_DATA_KEYWORDS):
                stock_code = await _resolve_stock_name_to_code(user_message, db=self._db)
                logger.info("[智能助手] 预检查·名称解析结果: %s", stock_code)
        if not stock_code:
            return None

        text_lower = user_message.lower()
        if not any(k in text_lower for k in _STOCK_DATA_KEYWORDS):
            return None

        sync_status = await _check_stock_data_sync(stock_code, db=self._db)
        logger.info("[智能助手] 预检查·股票 %s 数据状态: %s", stock_code, sync_status)
        if not sync_status or sync_status.get("error"):
            return None

        has_hist = sync_status.get("has_historical", False)
        has_fin = sync_status.get("has_financial", False)
        has_basic = sync_status.get("has_basic", False)
        hist_records = sync_status.get("hist_records", 0)
        fin_records = sync_status.get("fin_records", 0)

        financial_keywords = [
            "财报", "利润", "营收", "收入", "净利润", "毛利率", "现金流",
            "资产", "负债", "估值", "市盈率", "市净率", "pe", "pb", "ps",
            "盈利", "财务", "roe", "roa", "资产负债率",
        ]
        technical_keywords = [
            "技术面", "技术指标", "macd", "rsi", "kdj", "boll", "布林",
            "均线", "ma", "ema", "超买", "超卖", "背离", "支撑", "阻力",
            "压力位", "趋势", "k线",
        ]
        investment_decision_keywords = [
            "入手", "买入", "卖出", "加仓", "减仓", "持有", "清仓",
            "值得", "能不能买", "该不该", "怎么样", "如何", "分析",
        ]
        asks_financial = any(k in text_lower for k in financial_keywords)
        asks_technical = any(k in text_lower for k in technical_keywords)
        asks_investment_decision = any(k in text_lower for k in investment_decision_keywords)
        # 投资决策类问题需要财务+技术面综合判断
        if asks_investment_decision:
            asks_financial = True
            asks_technical = True

        missing_parts = []
        if asks_financial and not has_fin:
            missing_parts.append("财务数据（利润表/资产负债表/现金流量表，当前 0 条）")
        if asks_technical and (not has_hist or hist_records < 30):
            missing_parts.append(f"历史行情数据（当前 {hist_records} 条，技术分析需要至少 30 条）")
        if not missing_parts:
            return None

        missing_text = "\n".join(f"• ❌ {p}" for p in missing_parts)
        available_text = (
            f"• ✅ 历史行情: {hist_records} 条"
            if has_hist else "• ❌ 历史行情: 未同步"
        )
        available_text += "\n"
        available_text += (
            f"• ✅ 财务数据: {fin_records} 条"
            if has_fin else "• ❌ 财务数据: 未同步"
        )
        available_text += "\n"
        available_text += "✅ 基础数据: 已同步" if has_basic else "❌ 基础数据: 未同步"

        logger.info(
            "[智能助手] 股票 %s 数据不充分，拦截（缺失: %s）",
            stock_code, ", ".join(missing_parts),
        )
        return (
            f"📊 股票 {stock_code} 的数据不充分，无法进行分析\n\n"
            f"当前数据同步状态：\n{available_text}\n\n"
            f"缺失的数据：\n{missing_text}\n\n"
            f"请先同步数据后再提问：\n"
            f"• 前往「数据管理」页面，搜索 {stock_code} 并触发数据同步\n"
            f"• 建议同步完整的历史行情（至少 250 个交易日）和财务数据\n"
            f"• 同步完成后再次提问，我会调用工具获取真实数据为您分析\n\n"
            f"或者，如果您只是想了解互联网公开信息，我也可以通过 web_search 搜索。"
            f"是否需要我搜索？"
        )

    async def _path_data_precheck(
        self, req: AssistantRequest
    ) -> Optional[ReplyCandidate]:
        """路径5：股票数据未同步时的引导提示（passthrough 登记路径）。"""
        message = await self._build_data_precheck_message(req.user_message)
        if not message:
            return None
        return ReplyCandidate.passthrough(
            reply=message,
            route_id=ROUTE_DATA_PRECHECK,
            tools_used=["get_data_sync_status"],
        )

    async def _path_main_llm(self, req: AssistantRequest) -> ReplyCandidate:
        """路径6（兜底）：RAG/记忆注入 + LLM 工具循环 + 四道防线（answer 型）。

        质量处理顺序（两模式统一）：
        ① _do_chat 内部对 unsourced / untraced 各自动纠错重答最多 1 次；
        ② 构造 answer 候选后由适配器统一过 ReplyGate 出口兜底；
        ③ 仍不过则降级为 fallback_evidence（局限说明+证据摘录）。
        数据透明化附注经 meta 透传，由适配器在 gate 之后追加，避免附注数字干扰检查。
        """
        user_message = req.user_message
        conversation_id = req.conversation_id

        # RAG 动态知识检索（优雅降级：不可用时返回空字符串）
        knowledge_snippets = ""
        try:
            from core.knowledge import FinancialKnowledgeManager
            km = FinancialKnowledgeManager(db=self._db)
            knowledge_snippets = km.retrieve_for_prompt(user_message)
            if knowledge_snippets:
                logger.info("[智能助手] ✅ RAG 知识检索成功 (%d 字符)", len(knowledge_snippets))
            else:
                logger.info("[智能助手] RAG 知识检索返回空（无匹配知识）")
        except Exception as e:
            logger.warning("[智能助手] ⚠️ 知识检索失败（降级为静态知识）: %s", e)

        # 事实记忆检索：跨会话复用之前工具获取的真实数据（带时间标签，未过期才注入）
        recalled_memory_docs: List[Dict[str, Any]] = []
        memory_injection = ""
        try:
            recalled_memory_docs = await _recall_memory_facts(
                self._db, user_message, conversation_id or ""
            )
            memory_injection = _build_memory_injection(recalled_memory_docs)
        except Exception as e:
            logger.warning("[智能助手] 事实记忆检索异常（忽略）: %s", e)

        messages = [
            Message(
                role=MessageRole.SYSTEM,
                content=_get_system_prompt(
                    knowledge_snippets,
                    req.assistant_settings,
                    is_im=req.is_im,
                    user_profile=req.user_profile,
                    current_topic_title=req.current_topic_title,
                    memory_block=req.memory_block,
                    assistant_role=self._assistant_role,
                    user_context=self._user_context,
                ),
            ),
        ]
        if memory_injection:
            messages.append(Message(role=MessageRole.SYSTEM, content=memory_injection))
        if req.history_messages:
            messages.extend(req.history_messages)
            logger.info("[智能助手] 已注入 %d 条历史消息", len(req.history_messages))
        messages.append(Message(role=MessageRole.USER, content=user_message.strip()))

        # 溯源可信文本：会话历史工具返回 + 注入的事实记忆（引用记忆数字不算编造）
        memory_trusted_contents = [
            str(d.get("content", "")) for d in recalled_memory_docs if d.get("content")
        ]

        tools_used: List[str] = req.tools_used
        tool_results: List[Dict[str, Any]] = []
        # 质量信号（S9 埋点）：防线触发/重试记录，最终随 candidate.meta 落库
        quality_signals: List[str] = []

        async def _emit_progress(payload: Dict[str, Any]) -> None:
            if req.on_progress is None:
                return
            try:
                await req.on_progress(payload)
            except Exception:
                pass

        async def _do_chat(_retry_attempt: int = 0) -> str:
            tool_interceptor = self._build_tool_interceptor(conversation_id or "default")
            achat_kwargs: Dict[str, Any] = dict(
                tools=self._openai_tools,
                auto_execute_tools=True,
                max_tool_rounds=8,
                tools_used=tools_used,
                tool_results_collector=tool_results,
                dry_run=False,
                tool_interceptor=tool_interceptor,
            )
            # 仅流式模式注入进度回调（tool_started / tool_completed 等事件）
            if req.on_progress is not None:
                achat_kwargs["progress_callback"] = req.on_progress
                # 首轮 LLM 思考可能持续数分钟（推理模型+大工具表+长历史），
                # 先发一条反馈避免用户面对无任何提示的静默等待
                await _emit_progress({
                    "event": "llm_thinking",
                    "round": 1,
                    "message": "正在分析问题并规划处理步骤…",
                })

            resp = await self._llm_client.achat(messages, **achat_kwargs)
            await _record_assistant_token_usage(resp, conversation_id or "assistant_chat")

            # 兜底："应走工具"的个人数据查询首轮未调工具时，二次强制引导
            if not tools_used and _should_force_tool_retry(user_message):
                quality_signals.append("forced_tool_retry")
                logger.warning("[智能助手] 首轮未调用工具，触发工具重试兜底")
                retry_messages = list(messages)
                retry_messages.append(Message(
                    role=MessageRole.USER,
                    content=(
                        "请不要直接根据上下文猜测或编造结果。"
                        "这类问题必须先调用相关工具获取真实数据，再基于工具返回回答。"
                        "尤其不能直接编造指数点位、涨跌幅、MACD、RSI、均线、成交额等实时数字。"
                        "如果工具返回为空或日期不匹配，再明确说明数据为空或数据日期不匹配。"
                    ),
                ))
                # 优先 tool_choice=required；模型侧不支持则降级普通重试
                retry_kwargs = dict(achat_kwargs)
                retry_kwargs["tool_choice"] = "required"
                try:
                    resp = await self._llm_client.achat(retry_messages, **retry_kwargs)
                    await _record_assistant_token_usage(resp, conversation_id or "assistant_chat")
                except Exception as retry_err:
                    logger.warning("[智能助手] tool_choice=required 重试失败，降级普通重试: %s", retry_err)
                    resp = await self._llm_client.achat(retry_messages, **achat_kwargs)
                    await _record_assistant_token_usage(resp, conversation_id or "assistant_chat")

            # 事实记忆保存：本轮提取的"事实"落库（带时间标签，跨会话复用）
            if tool_results:
                await _persist_memory_facts(
                    self._db,
                    tool_results,
                    user_message,
                    conversation_id or "",
                    assistant_reply=resp.content or "",
                    related_context=_recent_user_context(messages),
                )

            # ── 回复后四道防线（第①级降级：反馈重答，最多 1 次）──
            reply_content = resp.content or ""
            trusted_texts = _extract_history_tool_contents(messages) + memory_trusted_contents

            if _has_fabricated_tool_json(reply_content):
                quality_signals.append("fabricated_json")
                logger.warning("[智能助手] 检测到编造的工具JSON结果，硬拦截回复")
                return (
                    "抱歉，我在呈现工具返回结果时出现了格式错误（把工具返回改写成了结构化数据），"
                    "这可能导致数据失真。\n\n"
                    "请重新提问，我会严格按工具返回的原始内容为您呈现分析结果。"
                )
            if _has_fabricated_tool_refs(reply_content, tools_used):
                quality_signals.append("fabricated_refs")
                logger.warning("[智能助手] 检测到伪造工具调用痕迹，拦截回复（tools_used=%s）", tools_used)
                return (
                    "抱歉，我在生成回复时出现了数据来源标注错误。\n\n"
                    "为确保您获取的信息真实可靠，请尝试将问题拆分得更具体一些，"
                    "例如直接指定想查看的数据维度（如\"查一下牧原股份最新财报\"\"看技术指标\"等），"
                    "我会调用对应工具获取真实数据后再为您分析。"
                )
            if _has_unsourced_numbers(reply_content, tools_used):
                logger.warning(
                    "[智能助手] 检测到未调工具但回复含大量数字，拦截回复（数字数=%d）",
                    len(_SPECIFIC_NUMBER_RE.findall(reply_content)),
                )
                if _retry_attempt < 1:
                    quality_signals.append("unsourced_retry")
                    await _emit_progress({
                        "event": "progress",
                        "stage": "retry",
                        "message": "检测到未经验证的数据，正在调用工具重新获取…",
                    })
                    messages.append(_retry_message("assistant", reply_content))
                    messages.append(_retry_message("user", _UNSOURCED_RETRY_PROMPT))
                    logger.info("[智能助手] 已拦截未溯源数字回复，自动纠错重试（第1次）")
                    return await _do_chat(_retry_attempt + 1)
                quality_signals.append("unsourced_blocked")
                return (
                    "抱歉，我刚才两次都未能先调用工具获取真实数据，为避免误导已停止回答。\n\n"
                    "请换一个更具体的问法（例如指明行业或股票，如\"养殖行业最近走势如何\"），"
                    "我会先调用工具获取真实数据再为您分析。"
                )
            if _has_untraced_risky_numbers(
                reply_content, tool_results, tools_used,
                user_message, trusted_texts,
            ):
                logger.warning("[智能助手] 检测到无法溯源的高风险数字（疑似训练记忆填充/单位换算错误），拦截回复")
                if _retry_attempt < 1:
                    quality_signals.append("untraced_retry")
                    await _emit_progress({
                        "event": "progress",
                        "stage": "retry",
                        "message": "检测到无法溯源的数字，正在基于工具数据重新生成…",
                    })
                    untraced_numbers = _get_untraced_risky_numbers(
                        reply_content, tool_results, user_message, trusted_texts
                    )[:6]
                    retry_prompt = _UNTRACED_RETRY_PROMPT_TEMPLATE.format(
                        numbers="、".join(untraced_numbers)
                    )
                    messages.append(_retry_message("assistant", reply_content))
                    messages.append(_retry_message("user", retry_prompt))
                    logger.info("[智能助手] 已拦截无法溯源数字回复，自动纠错重试（第1次）")
                    return await _do_chat(_retry_attempt + 1)
                quality_signals.append("untraced_blocked")
                untraced_detail = "、".join(_get_untraced_risky_numbers(
                    reply_content, tool_results, user_message, trusted_texts
                )[:6])
                return (
                    "抱歉，我在刚才的回复中引用了一些工具和搜索结果里都没有的具体数字"
                    f"（如 {untraced_detail}），这类数字可能来自过时的记忆而非实时数据，"
                    "继续呈现会误导您的判断，因此已整段拦截。\n\n"
                    "常见原因：搜索结果只有标题/链接/摘要，未包含具体数值"
                    "（如行业成本数据多在付费研报正文中，搜索引擎摘要抓不到）。\n"
                    "您可以：\n"
                    "• 换更具体的关键词让我再搜索（如\"牧原股份 7月成本 公告\"）\n"
                    "• 让我引用搜索到的来源链接，由您人工查看原文\n"
                    "• 只让我分析工具已返回的真实数据\n"
                )

            return reply_content

        content = await asyncio.wait_for(
            _do_chat(), timeout=self._effective_timeout(tools_used)
        )
        reply = content or "抱歉，暂时无法生成回答，请稍后再试。"
        if tools_used:
            logger.info("[智能助手] 已调用工具: %s", tools_used)
        else:
            logger.info("[智能助手] 未调用任何工具（纯文本回复）")
        logger.info("[智能助手] 回复内容(前200字): %s", reply[:200])

        # 证据集：本轮工具返回（跳过 is_error）+ 注入的事实记忆
        evidence = [
            Evidence(
                source_type=EVIDENCE_TOOL,
                source_id=str(item.get("tool") or "tool"),
                content=str(item.get("content") or ""),
            )
            for item in tool_results
            if item.get("content") and not item.get("is_error")
        ]
        evidence.extend(
            Evidence(source_type=EVIDENCE_MEMORY, source_id="mem0", content=trusted_text)
            for trusted_text in memory_trusted_contents
            if trusted_text
        )

        # 透明化附注（推导/估算值↔工具原始值）经 meta 透传，gate 通过后由适配器追加
        meta: Dict[str, Any] = {}
        if quality_signals:
            meta["quality_signals"] = quality_signals
        if tools_used:
            note = _build_data_transparency_note(
                reply, tool_results, user_message,
                _extract_history_tool_contents(messages) + memory_trusted_contents,
            )
            if note:
                meta["transparency_note"] = note

        return ReplyCandidate.answer(
            reply=reply,
            route_id=ROUTE_MAIN_LLM,
            llm_processed=True,
            tools_used=tools_used,
            evidence=evidence,
            meta=meta,
        )

    def _through_gate(
        self, candidate: ReplyCandidate, req: AssistantRequest
    ) -> ReplyCandidate:
        """唯一质量出口（两种模式共用）：契约 + 四道防线；answer 失败回退证据。

        关卡结果写入 meta["gate"]（S9 埋点在适配器统一采集）。
        """
        trusted = (
            _extract_history_tool_contents(req.history_messages)
            if req.history_messages else []
        )
        result = ReplyGate().run(candidate, req.user_message, trusted)
        if not result.passed and candidate.route_type == ROUTE_TYPE_ANSWER:
            logger.warning("[智能助手] gate 出口拦截，回退证据: %s", result.failed_checks)
            fallback = build_fallback_candidate(candidate, result.failures)
            fallback.meta["gate"] = {
                "passed": False,
                "failed": list(result.failed_checks),
                "fallback": True,
            }
            candidate = fallback
        elif not result.passed:
            # passthrough 未登记属程序缺陷（正常不应发生），仅记录，不臆造回复
            logger.error("[智能助手] passthrough 路径未登记: %s", result.failed_checks)
            candidate.meta["gate"] = {
                "passed": False,
                "failed": list(result.failed_checks),
                "fallback": False,
            }
        else:
            candidate.meta["gate"] = {"passed": True, "failed": [], "fallback": False}
        return candidate

    def _finalize_result(
        self, candidate: ReplyCandidate, req: AssistantRequest
    ) -> Dict[str, Any]:
        """关卡后的统一返回结构；透明化附注在此追加（不参与 gate 检查）。"""
        reply = candidate.reply
        note = candidate.meta.get("transparency_note")
        if note and not reply.endswith(note):
            reply = reply + note
        return {
            "reply": reply,
            "tools_used": candidate.tools_used,
            "data_refs": extract_data_refs(candidate.evidence),
        }

    def _record_metrics(
        self,
        result: Dict[str, Any],
        candidate: ReplyCandidate,
        req: AssistantRequest,
        t0: float,
        streaming: bool,
        error: Optional[str] = None,
    ) -> None:
        """S9 质量埋点：一次对话回合一条事件（fire-and-forget，绝不影响主链路）。"""
        try:
            meta = candidate.meta or {}
            gate = meta.get("gate") or {}
            quality_metrics.record(
                db=self._db,
                streaming=streaming,
                conversation_id=req.conversation_id,
                user_id=self._user_id,
                message_preview=(req.user_message or "")[:120],
                route_id=candidate.route_id,
                route_priority=meta.get("route_priority"),
                route_type=candidate.route_type,
                llm_processed=candidate.llm_processed,
                gate_passed=gate.get("passed"),
                gate_failed=gate.get("failed") or [],
                fallback_used=bool(gate.get("fallback")),
                quality_signals=meta.get("quality_signals") or [],
                evidence_count=len(candidate.evidence),
                data_refs_count=len(result.get("data_refs") or []),
                reply_chars=len(result.get("reply") or ""),
                tools_used=result.get("tools_used") or [],
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=error,
            )
        except Exception as exc:  # 埋点组装失败也不影响对话
            logger.debug("[智能助手] 质量埋点组装失败（忽略）: %s", exc)

    async def chat(
        self,
        user_message: str,
        conversation_id: Optional[str] = None,
        assistant_settings: dict = None,
        is_im: bool = False,
        history_messages: Optional[List[Message]] = None,
        user_profile: Optional[dict] = None,
        current_topic_title: Optional[str] = None,
        memory_block: str = "",
    ) -> Dict[str, Any]:
        """智能助手对话（非流式 / JSON 输出适配器）。

        与 chat_stream() 共用同一路径注册表与 ReplyGate，仅输出形态不同。

        Returns:
            {"reply": str, "tools_used": List[str], "data_refs": List[dict]}
        """
        t0 = time.monotonic()
        resolved_user_message = _rewrite_number_selection_reply(user_message, history_messages)

        req = AssistantRequest(
            service=self,
            user_message=resolved_user_message,
            conversation_id=conversation_id,
            assistant_settings=assistant_settings,
            is_im=is_im,
            history_messages=history_messages,
            user_profile=user_profile,
            current_topic_title=current_topic_title,
            memory_block=memory_block,
        )

        try:
            candidate = await self._route_candidate(req)
        except asyncio.TimeoutError:
            timeout_used = self._effective_timeout(req.tools_used)
            logger.warning("[智能助手] 请求超时（%ss）", timeout_used)
            candidate = ReplyCandidate.passthrough(
                reply=(
                    f"抱歉，处理您的问题用时过长（已超过 {timeout_used} 秒），已中断。\n\n"
                    "常见原因：模型正在处理较复杂的分析（多轮工具调用+长思考）。\n"
                    "建议您：\n"
                    "• 若刚才对话中已生成过结果表格，可直接要求\"把刚才的表格导出成Excel\"（秒级完成）\n"
                    "• 尝试将问题拆分得更具体一些\n"
                    "• 稍后重试"
                ),
                route_id=ROUTE_TIMEOUT,
                tools_used=req.tools_used,
            )
        except Exception as e:
            logger.exception("[智能助手] 对话失败")
            candidate = ReplyCandidate.passthrough(
                reply=f"处理您的请求时发生错误：{str(e)}。请检查网络和 LLM 配置后重试。",
                route_id=ROUTE_ERROR,
                tools_used=req.tools_used,
            )

        candidate = self._through_gate(candidate, req)
        result = self._finalize_result(candidate, req)
        self._record_metrics(result, candidate, req, t0, streaming=False)
        return result

    async def chat_stream(
        self,
        user_message: str,
        conversation_id: Optional[str] = None,
        assistant_settings: dict = None,
        is_im: bool = False,
        history_messages: Optional[List[Message]] = None,
        user_profile: Optional[dict] = None,
        current_topic_title: Optional[str] = None,
        memory_block: str = "",
    ) -> AsyncGenerator[str, None]:
        """智能助手对话（流式 / SSE 输出适配器）。

        与 chat() 共用同一路径注册表与 ReplyGate：工具执行期间转发 progress
        事件（tool_started/tool_completed/retry/keepalive），最终回复整段生成、
        过 gate 后再按固定块大小切块推送（LLM 调用本身为非流式，故关卡可以
        在任何字节发出前对完整回复执行，不存在"token 已推送"困境）。
        done 事件载荷：{"tools_used": [...], "data_refs": [...]}。
        """

        def _sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        t0 = time.monotonic()
        resolved_user_message = _rewrite_number_selection_reply(user_message, history_messages)
        logger.info("[智能助手·流式] chat_stream 开始，user_message=%r", resolved_user_message[:50])

        req = AssistantRequest(
            service=self,
            user_message=resolved_user_message,
            conversation_id=conversation_id,
            assistant_settings=assistant_settings,
            is_im=is_im,
            history_messages=history_messages,
            user_profile=user_profile,
            current_topic_title=current_topic_title,
            memory_block=memory_block,
        )

        event_queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()

        async def _progress(payload: Dict[str, Any]) -> None:
            await event_queue.put(payload)

        req.on_progress = _progress
        task = asyncio.create_task(self._route_candidate(req))

        try:
            # 管道执行期间持续转发工具进度事件，1s 无事件发心跳
            while not task.done():
                try:
                    payload = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    yield _sse(payload.get("event", "progress"), payload)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

            while not event_queue.empty():
                payload = event_queue.get_nowait()
                yield _sse(payload.get("event", "progress"), payload)

            candidate = task.result()
            candidate = self._through_gate(candidate, req)
            result = self._finalize_result(candidate, req)
            self._record_metrics(result, candidate, req, t0, streaming=True)
            reply = result["reply"]

            # 整段过 gate 后切块，保持前端逐段渲染体验
            chunk_size = 40
            for i in range(0, len(reply), chunk_size):
                yield _sse("token", {"content": reply[i:i + chunk_size]})
                await asyncio.sleep(0.015)

            yield _sse("done", {
                "tools_used": result["tools_used"],
                "data_refs": result["data_refs"],
            })
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            self._record_metrics(
                {"tools_used": req.tools_used, "data_refs": [], "reply": ""},
                ReplyCandidate.passthrough("", route_id="cancelled"),
                req, t0, streaming=True, error="cancelled",
            )
            yield _sse("error", {"message": "请求已取消"})
            yield _sse("done", {"tools_used": req.tools_used, "error": True})
        except asyncio.TimeoutError:
            timeout_used = self._effective_timeout(req.tools_used)
            logger.warning("[智能助手·流式] 请求超时（%ss）", timeout_used)
            self._record_metrics(
                {"tools_used": req.tools_used, "data_refs": [], "reply": ""},
                ReplyCandidate.passthrough("", route_id=ROUTE_TIMEOUT),
                req, t0, streaming=True, error=f"timeout:{timeout_used}s",
            )
            yield _sse("error", {"message": f"处理超时（已超过 {timeout_used} 秒）。若对话中已生成过结果表格，可直接要求导出成 Excel；或稍后重试。"})
            yield _sse("done", {"tools_used": req.tools_used, "error": True})
        except Exception as e:
            logger.exception("[智能助手·流式] 对话失败")
            self._record_metrics(
                {"tools_used": req.tools_used, "data_refs": [], "reply": ""},
                ReplyCandidate.passthrough("", route_id=ROUTE_ERROR),
                req, t0, streaming=True, error=str(e)[:200],
            )
            yield _sse("error", {"message": f"处理请求时发生错误：{str(e)}"})
            yield _sse("done", {"tools_used": req.tools_used, "error": True})


from app.services.assistant.memory import (
    _build_memory_injection,
    _check_stock_data_sync,
    _extract_history_tool_contents,
    _extract_stock_code,
    _persist_memory_facts,
    _recall_memory_facts,
    _recent_user_context,
    _record_assistant_token_usage,
    _resolve_stock_name_to_code,
    _retry_message,
    _should_force_tool_retry,
)
from app.services.assistant.tools import (
    _build_remember_this_tool_def,
    _make_remember_this_callable,
    _matches_generated_skill,
    _rewrite_number_selection_reply,
    _should_use_recent_search,
)
from app.services.assistant.prompts import (
    _get_system_prompt,
)
from app.services.assistant.llm_config import (
    _build_assistant_model_preferences,
    _get_assistant_llm_config,
    _get_preferred_assistant_config_id,
    _get_preferred_assistant_model,
)
