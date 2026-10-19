"""智能助手：tools。"""
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



def _build_remember_this_tool_def() -> Dict[str, Any]:
    """构造 `remember_this` 的 OpenAI Function Calling 定义"""
    return {
        "type": "function",
        "function": {
            "name": REMEMBER_THIS_TOOL_NAME,
            "description": (
                "当用户明确表达「请记住」「请帮我记下」「我希望以后也按这个偏好」等意图时调用此工具，"
                "把这条偏好/事实写入用户长期记忆（user_preference）。"
                "调用后请告知用户『已记下』，然后继续完成原本的任务。"
                "禁止滥用：仅当用户**明确表达需要记忆**时调用；用户随口提到的偏好不要写入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "需要记住的事实文本（一句话，包含足够上下文，如『用户偏好分红率>3%的高股息蓝筹股，回避高市盈率成长股』）",
                    },
                    "category": {
                        "type": "string",
                        "description": "偏好类型：investment_style（投资风格）/ risk_tolerance（风险偏好）/ industry_preference（行业偏好）/ trading_discipline（交易纪律）/ other",
                        "enum": [
                            "investment_style",
                            "risk_tolerance",
                            "industry_preference",
                            "trading_discipline",
                            "other",
                        ],
                    },
                },
                "required": ["content"],
            },
        },
    }


def _make_remember_this_callable(db, user_id: str):
    """生成绑定了 db + user_id 的 remember_this 异步实现。"""

    async def _remember_this(content: str, category: str = "other") -> str:
        if not user_id:
            return "记忆失败：当前会话未关联用户。"
        text = (content or "").strip()
        if not text:
            return "记忆失败：内容为空。"
        try:
            from core.memory.service import get_memory_service
            from core.memory.models import MemoryScope
            svc = get_memory_service(db)
            result = await svc.store(
                [{"role": "user", "content": text}],
                user_id=user_id,
                agent_id="assistant",
                scope=MemoryScope.USER_PREFERENCE,
                metadata={
                    "source": "user_explicit_remember",
                    "category": (category or "other").strip().lower(),
                },
                infer=False,  # 用户已明确表达，不需要 LLM 再提取
            )
            if not result.success:
                logger.warning(f"[智能助手] remember_this 写入失败: {result.error}")
                return f"记忆失败：{result.error or '记忆层不可用'}"
            logger.info(f"[智能助手] remember_this 已写入: user={user_id} category={category}")
            return f"已记下：{text[:60]}{'...' if len(text) > 60 else ''}。下次对话会自动参考此偏好。"
        except Exception as e:
            logger.warning(f"[智能助手] remember_this 异常: {e}")
            return f"记忆失败：{e}"

    return _remember_this


def _should_use_recent_search(user_message: str) -> bool:
    """判断是否属于应优先走近期搜索的事实确认类问题。"""
    text = (user_message or "").strip().lower()
    if not text:
        return False

    recency_markers = [
        "截至目前", "截至现在", "最新", "近期", "最近", "刚刚", "今日", "现在",
        "确认", "核实", "联网", "公告", "新闻", "消息", "进展",
    ]
    fact_markers = [
        "持股", "持股比例", "减持", "增持", "伯克希尔", "巴菲特", "公告",
        "有没有", "是否", "还持有", "变化", "最新情况",
    ]

    has_recency = any(marker in text for marker in recency_markers)
    has_fact = any(marker in text for marker in fact_markers)
    return has_recency and has_fact


# 领域词匹配的噪声词：接口工程术语、时间词、通用动词/虚词等，
# 出现在任何工具描述里都不代表业务领域。数据驱动匹配时必须剔除，
# 否则「销量数据接口」这类描述会因「数据/接口」与任意问题误匹配。
_SKILL_DOMAIN_STOPGRAMS = {
    "接口", "数据", "参数", "返回", "结果", "查询", "获取", "调用", "支持",
    "提供", "相关", "信息", "时间", "月份", "月度", "类型", "情况", "变化",
    "最新", "最近", "全国", "各类", "可以", "使用", "通过", "按照", "指定",
    "功能", "工具", "能力", "记录", "列表", "名称", "条件", "筛选",
    "输入", "输出", "字段", "结构", "格式", "内容", "自动", "进行", "根据",
    "以及", "或者", "如果", "需要", "请求", "响应", "服务", "系统", "方法",
}


def _domain_terms(text: str) -> set:
    """从文本提取领域词集合：连续中文段的 2-gram + 长度≥2 的英文/数字词。

    不用分词库：2-gram 对「车型/销量/问界/懂车帝」这类领域词召回稳定，
    噪声由 _SKILL_DOMAIN_STOPGRAMS 通用词表统一剔除。
    """
    terms: set = set()
    text = (text or "").lower()
    # 英文/数字词（品牌英文、型号等，如 model、m9）
    for token in re.findall(r"[a-z0-9]{2,}", text):
        terms.add(token)
    # 连续中文段逐字取 2-gram
    for segment in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        for i in range(len(segment) - 1):
            gram = segment[i:i + 2]
            if gram not in _SKILL_DOMAIN_STOPGRAMS:
                terms.add(gram)
    return terms


# 每次对话都带上的工具。其余工具按问题和描述的领域词交集补充，
# 避免把全部工具 schema 塞进首轮推理。
_CORE_ASSISTANT_TOOL_NAMES = frozenset({
    "get_stock_market_data_unified",
    "get_technical_indicators",
    "get_technical_factor_bundle_tool",
    "get_stock_fundamentals_unified",
    "get_stock_news_unified",
    "get_stock_news_headlines_unified",
    "get_stock_sentiment_unified",
    "get_china_market_overview",
    "search_recent_information",
    "web_search",
    "fetch_url",
    "search_user_manual",
    "read_user_manual_section",
    "get_data_sync_status",
    "sync_single_stock_data",
    "get_paper_trading_status",
    "get_token_usage",
    "get_running_tasks",
    "get_user_current_state",
    "get_system_status",
    "get_user_recent_activity",
    "search_reports",
    "get_latest_report",
    "get_report_detail",
    "trigger_stock_analysis",
    "get_analysis_status",
    "list_positions",
    "get_position_analysis",
    "analyze_chanlun",
    "analyze_elliott_wave",
    "remember_this",
})
_ASSISTANT_TOOL_SOFT_LIMIT = 64
_ASSISTANT_EXTRA_TOOL_LIMIT = 24


_FOLLOWUP_STOPGRAMS = {"好的", "可以", "继续", "一下", "这个", "那个", "还是", "然后", "帮我", "请你"}
_MISSING_TOOL_MARKERS = (
    "没有对应的工具",
    "没有可用工具",
    "没有找到合适的工具",
    "工具列表里没有",
    "工具表里没有",
    "当前工具无法",
    "无法导出",
    "不能导出",
    "没法导出",
)


def assistant_history_text(history_messages) -> str:
    """取最近几轮对话文本，供工具筛选使用。"""
    parts = []
    for message in list(history_messages or [])[-8:]:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip()[:500])
    return "\n".join(parts)


def reply_requests_missing_tool(reply: str) -> bool:
    text = reply or ""
    return any(marker in text for marker in _MISSING_TOOL_MARKERS)


def select_assistant_tools(
    openai_tools: List[Dict[str, Any]],
    user_message: str,
    history_text: str = "",
    used_tool_names: Optional[List[str]] = None,
    recover: bool = False,
) -> List[Dict[str, Any]]:
    """收窄本轮发给模型的工具表。

    常用工具和本会话已经用过的工具始终保留。当前问题和最近对话一起做领域词匹配。
    很短的追问即使只有一个有效词，也保留直接对上的工具。recover 为真时把匹配门槛降到一个词，
    用来补上第一次没放进工具表的能力。
    """
    if not openai_tools or len(openai_tools) <= _ASSISTANT_TOOL_SOFT_LIMIT:
        return openai_tools

    used_names = {str(name) for name in (used_tool_names or []) if name}
    message_terms = _domain_terms(user_message) - _FOLLOWUP_STOPGRAMS
    context_terms = message_terms | (_domain_terms(history_text) - _FOLLOWUP_STOPGRAMS)
    context_threshold = 1 if recover else 2
    core: List[Dict[str, Any]] = []
    seen = set()
    context_ranked: List[tuple] = []
    followup_ranked: List[tuple] = []
    for tool in openai_tools:
        function = tool.get("function") or {}
        name = str(function.get("name") or "")
        if name in _CORE_ASSISTANT_TOOL_NAMES or name in used_names:
            if name not in seen:
                core.append(tool)
                seen.add(name)
            continue
        description = f"{name} {function.get('description') or ''}"
        tool_terms = _domain_terms(description)
        context_overlap = context_terms & tool_terms
        message_overlap = message_terms & tool_terms
        if len(context_overlap) >= context_threshold:
            context_ranked.append((len(context_overlap), name, tool))
        elif message_terms and len(message_terms) <= 3 and len(message_overlap) >= 1:
            followup_ranked.append((len(message_overlap), name, tool))

    context_ranked.sort(key=lambda item: (-item[0], item[1]))
    followup_ranked.sort(key=lambda item: (-item[0], item[1]))
    extra = []
    for _score, name, tool in followup_ranked[:8] + context_ranked[:_ASSISTANT_EXTRA_TOOL_LIMIT]:
        if name in seen:
            continue
        extra.append(tool)
        seen.add(name)
    selected = core + extra
    logger.info(
        "[智能助手] 工具表从 %d 收窄到 %d（常驻 %d，问题匹配 %d，补救=%s）",
        len(openai_tools), len(selected), len(core), len(extra), recover,
    )
    return selected


def _matches_generated_skill(user_message: str) -> Optional[tuple]:
    """判断问题是否能被某个「用户生成的外部 Skill」结构化承接。

    近期搜索快路径是股票时代留下的关键词规则（「最近」+「变化/持股」即
    直接网页搜索），系统支持自定义外部 Skill 后，它会在 LLM 工具选择之前
    抢先拦截本应由专用工具回答的问题（真实案例：「问界各车型最近半年销量
    变化」→ 没走懂车帝销量 Skill，直接网页搜索，只拿到新闻标题）。

    匹配完全数据驱动：只读取注册表里 source=generated 的 Skill 元数据
    （名称/描述/when_to_use），与用户问题做领域词交集；交集≥2 个不同领域
    词才算命中（单词命中过宽，容易被「品牌/销量」之类的偶然共现误判）。
    不写死任何具体 Skill 名称或行业关键词——新装任何领域的 Skill 同样生效。

    Returns:
        命中时返回 (tool_id, tool_name)；否则 None
    """
    msg_terms = _domain_terms(user_message)
    if len(msg_terms) < 2:
        return None
    try:
        from core.tools import get_tool_registry
        registry = get_tool_registry()
    except Exception:
        return None
    for skill in registry.get_external_skills():
        if not getattr(skill, "fc_enabled", True):
            continue
        skill_text = " ".join(
            str(getattr(skill, attr, "") or "")
            for attr in ("name", "description", "when_to_use")
        )
        overlap = msg_terms & _domain_terms(skill_text)
        if len(overlap) >= 2:
            logger.info(
                "[智能助手] 问题命中专用 Skill「%s」领域词 %s，搜索快路径让路",
                getattr(skill, "id", ""), sorted(overlap),
            )
            return (getattr(skill, "id", ""), getattr(skill, "name", ""))
    return None


def _extract_numbered_options(text: str) -> Dict[int, str]:
    options: Dict[int, str] = {}
    for raw_line in (text or "").splitlines():
        match = _NUMBERED_OPTION_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        option_no = int(match.group(1))
        option_text = re.sub(r"\*+", "", match.group(2)).strip()
        option_text = re.sub(r"\s+", " ", option_text)
        if option_text:
            options[option_no] = option_text[:200]
    return options


def _rewrite_number_selection_reply(user_message: str, history_messages: Optional[List[Message]]) -> str:
    original_text = (user_message or "").strip()
    if not original_text:
        return original_text

    number_match = _NUMBER_ONLY_REPLY_RE.match(original_text)
    if not number_match or not history_messages:
        return original_text

    selected_index = int(number_match.group(1))
    for message in reversed(history_messages):
        if message.role != MessageRole.ASSISTANT:
            continue
        options = _extract_numbered_options(message.content or "")
        selected_option = options.get(selected_index)
        if not selected_option:
            continue
        logger.info("[智能助手] 将裸数字回复重写为编号选项选择: %s -> %s", original_text, selected_option)
        return (
            f"用户上一轮只回复了数字“{selected_index}”，这是在选择你上一条消息里的第{selected_index}项，不是新的独立问题。\n"
            f"用户选择的选项是：{selected_option}\n"
            "请直接沿着这个选项继续回答或继续分析，不要再次猜测这个数字可能代表别的含义，也不要重复发起二次确认。"
        )

    return original_text


from app.services.assistant.memory import REMEMBER_THIS_TOOL_NAME
