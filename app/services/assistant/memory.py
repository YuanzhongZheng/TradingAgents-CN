"""智能助手：memory。"""
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



def _extract_memory_subject(user_msg: str, assistant_msg: str) -> str:
    title_match = re.search(r"##\s*[📌✅⚠️🔍💡]*\s*([A-Za-z0-9\u4e00-\u9fff]{2,20})(?:[（(][^\n]{0,20}[)）])?(?:——|\-|：|:)", assistant_msg or "")
    if title_match:
        return title_match.group(1).strip()

    user_match = re.search(r"([A-Za-z0-9\u4e00-\u9fff]{2,20})(?:股票|走势|业绩|表现|分红|怎么看|为什么|如何)", user_msg or "")
    if user_match:
        return user_match.group(1).strip()

    return ""


def _clean_memory_line(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^#+\s*", "", cleaned)
    cleaned = re.sub(r"^[>\-*\d\.\s✅⚠️📌🔍💡]+", "", cleaned)
    cleaned = re.sub(r"\*+", "", cleaned)
    cleaned = re.sub(r"`+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" |：:-")


def _build_fallback_memory_facts(user_msg: str, assistant_msg: str, limit: int = 5) -> List[str]:
    subject = _extract_memory_subject(user_msg, assistant_msg)
    candidates: List[str] = []

    for raw_line in (assistant_msg or "").splitlines():
        line = _clean_memory_line(raw_line)
        if not line or len(line) < 10:
            continue
        if line.startswith("|") or set(line) <= {"-", "|", ":", " "}:
            continue
        if "数据来源" in line:
            continue
        if any(keyword in line for keyword in _MEMORY_FACT_KEYWORDS):
            if subject and subject not in line and len(line) < 70:
                line = f"{subject}：{line}"
            candidates.append(line[:120])

    seen = set()
    facts: List[str] = []
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        facts.append(item)
        if len(facts) >= limit:
            break
    return facts


# 数据相关关键词——用于判断用户问题是否涉及股票数据查询
_STOCK_DATA_KEYWORDS = [
    "财报", "利润", "营收", "收入", "净利润", "毛利率", "现金流", "资产", "负债",
    "估值", "市盈率", "市净率", "pe", "pb", "ps",
    "销量", "产量", "出栏", "交付",
    "价格", "股价", "行情", "涨跌", "涨幅", "跌幅",
    "入手", "买入", "卖出", "加仓", "减仓", "持有", "清仓", "值得", "能不能买", "该不该",
    "分析", "研究", "怎么样", "如何", "技术面", "基本面", "基本面",
]


def _extract_stock_code(user_message: str) -> Optional[str]:
    """从用户消息中提取 6 位 A 股股票代码（正则事实源：core.assistant.lexicon）。"""
    return extract_a_share_code(user_message)


async def _resolve_stock_name_to_code(user_message: str, db=None) -> Optional[str]:
    """尝试从用户消息中匹配股票名称并返回股票代码。

    事实源：core.assistant.lexicon.stock_directory（stock_basic_info 全量
    名称→代码目录 + TTL 缓存），替代历史上的硬编码常见名 + 每请求 regex 查库。
    db 参数优先；未传时尝试 get_mongo_db()（某些上下文会失败）。
    """
    if not user_message:
        return None
    try:
        if db is None:
            from app.core.database import get_mongo_db
            db = get_mongo_db()
        return await stock_directory.resolve_symbol(user_message, db)
    except Exception as e:
        logger.warning("[智能助手] 股票名称解析失败: %s", e)
        return None


async def _check_stock_data_sync(symbol: str, db=None) -> dict:
    """检查单只股票的数据同步状态，返回 {has_historical, has_financial, has_basic, ...}。

    直接查询实际数据集合（与前端数据检验接口一致）：
    - 基础数据: stock_basic_info（按 symbol）
    - 历史行情: stock_daily_quotes（按 symbol/code）
    - 财务数据: stock_financial_data（按 symbol）
    """
    try:
        if db is None:
            from app.core.database import get_mongo_db
            db = get_mongo_db()

        normalized = str(symbol).strip().upper()
        if "." in normalized:
            normalized = normalized.split(".", 1)[0]

        # 基础数据
        basic_doc = await db["stock_basic_info"].find_one(
            {"symbol": normalized}, {"symbol": 1, "name": 1}
        )

        # 历史行情（stock_daily_quotes，兼容 symbol/code 两种字段）
        hist_count = await db["stock_daily_quotes"].count_documents({"symbol": normalized})
        if hist_count == 0:
            hist_count = await db["stock_daily_quotes"].count_documents({"code": normalized})
        hist_latest = await db["stock_daily_quotes"].find_one(
            {"symbol": normalized}, sort=[("trade_date", -1)]
        ) or await db["stock_daily_quotes"].find_one(
            {"code": normalized}, sort=[("trade_date", -1)]
        )
        hist_last_date = None
        if hist_latest:
            d = hist_latest.get("trade_date") or hist_latest.get("date")
            hist_last_date = str(d)[:10] if d else None

        # 财务数据
        fin_count = await db["stock_financial_data"].count_documents({"symbol": normalized})

        return {
            "has_historical": hist_count > 0,
            "has_financial": fin_count > 0,
            "has_basic": basic_doc is not None,
            "hist_records": hist_count,
            "fin_records": fin_count,
            "hist_last_date": hist_last_date,
            "stock_name": basic_doc.get("name") if basic_doc else None,
            "symbol": normalized,
        }
    except Exception as e:
        logger.warning("[智能助手] 检查股票数据同步状态失败(%s): %s", symbol, e)
        return {"error": str(e)}


# 回复质量防线（编造检测正则/数字溯源/重试指令/透明化附注）已收编至
# core.assistant.defense（A11，单一事实源），符号见文件顶部 import。


def _retry_message(role: str, content: str) -> Message:
    """构造重试反馈消息。

    注意：achat 要求 messages 为 core.llm.models.Message 对象（pydantic），
    直接 append dict 会触发 'dict' object has no attribute 'role'。
    """
    return Message(role=MessageRole(role), content=content)


# 四道主防线（编造JSON/伪造引用/未溯源数字/高风险数字溯源）已收编至
# core.assistant.defense（A11），由 ReplyGate 统一调用。


def _extract_history_tool_contents(messages: List[Any]) -> List[str]:
    """从对话历史中提取可溯源的可信文本（多轮溯源用）。

    覆盖两类内容：
    1. 历史 tool 消息（工具返回原文）；
    2. 历史 assistant 回复——这些回复均通过四道防线验证才落库，
       属已验证可信文本。LLM 引用自己上一轮回复中的数字（如推导值、
       上轮搜索结论）不应被判为编造。
    """
    contents = []
    for m in (messages or []):
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        if role in ("tool", "assistant") and content:
            contents.append(str(content))
    return contents


def _recent_user_context(messages: List[Any], max_messages: int = 6) -> str:
    """提取会话近期用户消息拼接文本（用于记忆保存时提取关联股票）。

    行业类记忆（如"生猪养殖成本"）的主题不含股票名，但保存该记忆的
    会话上下文里通常有（如"牧原股份值得入手吗"）——据此建立
    股票↔行业记忆的关联。
    """
    texts = []
    for m in (messages or []):
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content")
        if role == "user" and content:
            texts.append(str(content))
    return " ".join(texts[-max_messages:])[-2000:]


# ── 事实记忆层（带时间标签，跨会话复用工具获取的真实数据）──────────────────────
# 设计要点：
# 1. 仅对 web_search/fetch_url 等在线工具的成功结果建立记忆（含来源、跨会话有价值）；
#    本地数据类工具（财务/行情）不建记忆——数据库每次查询都是最新值，缓存无意义。
# 2. 每条记忆带 fetched_at（采集时间）+ category（数据类别）+ valid_until（失效时间），
#    检索时只返回未过期记忆；过期数据不注入、由新一轮工具结果覆盖。
# 3. 注入 prompt 的记忆加入溯源候选集：助手引用记忆数字不会被第4道防线误拦。
_MEMORY_FACTS_COLLECTION = "assistant_memory_facts"
# 可保存为事实记忆的在线工具（结果含外部世界数据，跨会话有复用价值）
# 注意：tool_id 必须与 core/tools 注册名一致（此前误写 web_search/fetch_url
# 导致从未命中，现修正为真实 ID；旧名保留做兼容）
_MEMORY_FACT_TOOLS = {
    "search_recent_information",  # 联网搜索（真实工具ID）
    "get_stock_news_unified",     # 个股新闻聚合
    "web_search",                 # 兼容保留
    "fetch_url",                  # 兼容保留
}
# 类别规则：(类别名, 关键词列表, 有效天数)，顺序即优先级
_FACT_CATEGORY_RULES = [
    ("realtime_quote", ["价格", "行情", "均价", "现价", "股价", "涨跌", "实时", "点位"], 3),
    ("industry_metric", ["成本", "存栏", "产能", "出栏", "销量", "市场份额", "行业"], 30),
    ("company_financial", ["财报", "营收", "净利", "季报", "年报", "毛利", "现金流", "业绩"], 90),
    ("company_static", ["上市日期", "主营业务", "公司简介", "注册地", "成立时间", "发展历程"], 365),
]
_FACT_DEFAULT_VALID_DAYS = 7
# 事实句提取：回复/工具原文中"含数字 + 命中这些关键词"的短句才作为记忆落库
_FACT_SENTENCE_KEYWORDS = [
    "销量", "交付", "产量", "出栏", "存栏", "产能",
    "成本", "价格", "均价", "毛利", "利润", "营收", "收入", "净利", "亏损", "盈利",
    "同比", "环比", "增长", "下滑", "下降", "上升", "上涨", "下跌", "回落", "涨幅", "跌幅",
    "占比", "份额", "市占率", "分红", "股息", "估值", "市盈率",
    "调研", "评级", "公告",
    "亿元", "万元", "元/", "公斤", "千克", "kg", "吨", "斤", "万辆", "万头",
]
# 每轮最多落库的事实条数 / 单条事实最大长度
_MEMORY_FACT_MAX_PER_TURN = 6
_MEMORY_FACT_MAX_CHARS = 150
# 检索返回条数上限 / 单条注入长度上限
_MEMORY_RECALL_TOP_K = 3
_MEMORY_INJECT_MAX_CHARS = 1500
# 主题匹配：bigram 交集达到该数量视为命中
_MEMORY_MATCH_THRESHOLD = 2


def _classify_fact_category(text: str) -> str:
    """根据文本关键词判定数据类别（决定记忆有效期）。"""
    t = text or ""
    for category, keywords, _days in _FACT_CATEGORY_RULES:
        if any(k in t for k in keywords):
            return category
    return "default"


def _fact_valid_days(category: str) -> int:
    for cat, _kw, days in _FACT_CATEGORY_RULES:
        if cat == category:
            return days
    return _FACT_DEFAULT_VALID_DAYS


def _extract_match_tokens(text: str) -> set:
    """提取文本匹配词元：中文 bigram + 英文单词 + 数字串。"""
    if not text:
        return set()
    tokens = set()
    # 中文 bigram（"生猪养殖" → {生猪, 猪养, 养殖}）
    chinese_runs = re.findall(r"[\u4e00-\u9fa5]+", text)
    for run in chinese_runs:
        for i in range(len(run) - 1):
            tokens.add(run[i : i + 2])
    # 英文单词 / 数字串
    for w in re.findall(r"[A-Za-z]{2,}|\d{4,}", text):
        tokens.add(w.lower())
    return tokens


def _extract_symbol_hint(text: str) -> Optional[str]:
    """从文本中提取 6 位 A 股代码（记忆关联股票，可空）。"""
    m = re.search(r"\b(\d{6})\b", text or "")
    return m.group(1) if m else None


# 关联主体提取：股票名（XX股份/XX集团等常见后缀）与 6 位代码。
# 用途：行业类记忆（如"生猪养殖成本"）保存时记录当时会话涉及的股票
# （如"牧原股份"），后续问该股任何问题（如"牧原反弹了吗"）都能带出行业背景
_RELATED_NAME_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,4}(?:股份|集团|科技|生物|能源|牧业|农业|银行|证券|药业|重工|电工|汽车|医药)"
)


def _extract_related_terms(
    *texts: str, name_map: Optional[Dict[str, str]] = None
) -> List[str]:
    """从多段文本中提取关联主体（股票名+代码），最多8个。

    name_map: 股票名→代码 映射（None 时仅正则提取名称）。
    传入映射后，"赛力斯"这类不带后缀的公司名也能得到关联代码（如 601127），
    保证行业/个股类记忆保存与检索两侧提取结果一致。
    """
    terms = set()
    _connectives = "与和及对比跟或者、，。？?；;：: "
    joined = " ".join(t for t in texts if t)
    for t in texts:
        if not t:
            continue
        for m in _RELATED_NAME_RE.finditer(t):
            name = m.group(0).lstrip(_connectives)
            if len(name) >= 4:  # 至少2字公司名+2字后缀
                terms.add(name)
        for code in re.findall(r"\b\d{6}\b", t):
            terms.add(code)
    for name, sym in (name_map or {}).items():
        if name in joined:
            terms.add(sym)
    return sorted(terms)[:8]


# 记忆链路的股票名称→代码映射改用 core.assistant.lexicon.stock_directory
# （全量 + TTL 缓存），不再维护本模块私有缓存。语义桥梁说明：行业类记忆
# （如"生猪养殖完全成本"）虽不含股票名，但保存时的会话上下文（用户在聊牧原）
# 可建立关联，检索时问该股票即命中——解决"问个股时想要行业背景记忆"的问题


def _clean_fact_line(text: str) -> str:
    """清洗候选事实句：仅去除列表符号/星号/反引号，保留数字开头的原样。

    注意：不复用 _clean_memory_line——它会剥离行首数字（把"1-7月累计销量"
    误清理成"月累计销量"），破坏销量/产量等以数字开头的事实。
    """
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^[>\-\*\s✅⚠️📌🔍💡]+", "", cleaned)
    cleaned = re.sub(r"\*+", "", cleaned)
    cleaned = re.sub(r"`+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _extract_fact_sentences(text: str, max_facts: int = _MEMORY_FACT_MAX_PER_TURN) -> List[str]:
    """从文本中提取"含数字 + 命中事实关键词"的短句（记忆落库用，不额外调 LLM）。

    适用场景（按优先级）：
    1. 助手最终回复——已通过四道数字溯源防线，句中数字可回溯到本轮工具结果，
       作为事实直接落库（如"赛力斯2026年7月新能源销量24,229辆"）；
    2. 工具返回原文兜底——搜索结果摘要本身往往就是一条事实。

    规则：按句号/分号/换行切句，保留含数字且命中事实关键词的句子，清洗去重。
    """
    if not text:
        return []
    facts: List[str] = []
    seen = set()
    for raw in re.split(r"[。！？；;\n]+", text):
        sent = _clean_fact_line(raw)
        if len(sent) < 8:
            continue
        if sent.startswith("|"):  # 表格行
            continue
        if any(t in sent for t in ("数据来源", "免责声明", "不构成投资建议", "以上内容")):
            continue
        if not re.search(r"\d", sent):
            continue  # 无数字不成事实
        if not any(k in sent for k in _FACT_SENTENCE_KEYWORDS):
            continue
        sent = sent[:_MEMORY_FACT_MAX_CHARS]
        if sent in seen:
            continue
        seen.add(sent)
        facts.append(sent)
        if len(facts) >= max_facts:
            break
    return facts


def _fact_topic(
    fact: str, user_message: str, name_map: Optional[Dict[str, str]] = None
) -> str:
    """为单条事实生成记忆主题（主体+概念），如"赛力斯 销量"。

    主体来源（按优先级）：
    1. 用户消息中的明确主体（正则匹配"股票/走势/业绩…"等后缀）；
    2. 股票名称映射中最长命中名（如"赛力斯"不带后缀也能识别，取最长
       避免"力斯"之类子串误配）；
    3. 事实文本中的"XX股份/XX汽车"类名称。
    概念为事实命中的第一个核心词（销量/成本/价格…）。
    检索时以"主体+概念"的 bigram 命中即可召回。
    """
    subject = _extract_memory_subject(user_message, fact)
    if not subject and name_map:
        text = f"{user_message or ''} {fact or ''}"
        name_hits = [name for name in name_map if len(name) >= 2 and name in text]
        if name_hits:
            subject = max(name_hits, key=len)
    if not subject:
        m = _RELATED_NAME_RE.search(fact or "")
        if m:
            subject = m.group(0)
    concept = next((k for k in _FACT_SENTENCE_KEYWORDS if k in (fact or "")), "")
    parts = [p for p in (subject, concept) if p]
    if not parts:
        return ""
    return " ".join(parts)[:60]


async def _persist_memory_facts(
    db,
    tool_results: List[Dict[str, Any]],
    user_message: str,
    thread_id: str,
    assistant_reply: str = "",
    related_context: str = "",
) -> int:
    """将本轮在线工具背后的"提取事实"保存为记忆（不再存工具返回全文）。

    事实来源（按优先级）：
    1. 助手最终回复——已通过数字溯源防线，其中"含数字+事实关键词"的短句
       即可信事实（如"赛力斯2026年7月新能源销量24,229辆"）；
    2. 兜底：从工具返回原文中抽取同样格式的短句（搜索结果摘要往往本身就是
       一条事实，如"牧原获11家机构调研：2025年完全成本12元/公斤"）。

    相比旧实现（把工具返回全文存 content[:8000]）：
    - 每条记忆≤150字符，体积小、检索注入无压力；
    - 数字直接存在于 content，换会话召回后可进入溯源候选集（memory_trusted_contents）；
    - 主题按"主体+概念"生成（如"赛力斯 销量"），bigram 命中即可召回。

    Args:
        assistant_reply: 本轮助手最终回复（已通过数字溯源防线）。
        related_context: 会话近期上下文（如最近几条用户消息拼接），
            用于提取关联股票存入 related_terms——行业类事实借此在该股
            的后续问题中被召回。

    Returns:
        保存条数（0 表示无可保存事实）。
    """
    saved = 0
    try:
        name_map = await stock_directory.get_name_map(db)
        related_terms = _extract_related_terms(user_message, related_context, name_map=name_map)
        facts = _extract_fact_sentences(assistant_reply or "")
        if not facts:
            # 回复里无可提取事实（如纯会话回复），退回从工具原文抽取
            for item in (tool_results or []):
                if item.get("tool") not in _MEMORY_FACT_TOOLS or item.get("is_error"):
                    continue
                facts.extend(_extract_fact_sentences(item.get("content") or ""))
                if len(facts) >= _MEMORY_FACT_MAX_PER_TURN:
                    break
        if not facts:
            return 0
        tools_used_str = ",".join(
            sorted({str(i.get("tool", "")) for i in (tool_results or []) if i.get("tool")})
        )
        now = datetime.now()
        for fact in facts[:_MEMORY_FACT_MAX_PER_TURN]:
            topic = _fact_topic(fact, user_message, name_map)
            if not topic:
                continue
            category = _classify_fact_category(fact)
            valid_until = now + timedelta(days=_fact_valid_days(category))
            doc = {
                "topic": topic,
                "symbol": _extract_symbol_hint(fact),
                "related_terms": related_terms,
                "tool_id": tools_used_str,
                "content": fact,
                "fetched_at": now.isoformat(timespec="seconds"),
                "category": category,
                "valid_until": valid_until.isoformat(timespec="seconds"),
                "thread_id": thread_id,
            }
            # 相同事实句（topic+content）upsert 刷新，不同事实各自成条
            await db[_MEMORY_FACTS_COLLECTION].update_one(
                {"topic": topic, "content": fact}, {"$set": doc}, upsert=True
            )
            saved += 1
            logger.info(
                "[智能助手·记忆] 已保存事实: topic=%.40s category=%s fact=%.50s",
                topic, category, fact,
            )
    except Exception as e:
        logger.warning("[智能助手·记忆] 保存事实记忆失败（不影响回复）: %s", e)
    return saved


async def _recall_memory_facts(
    db,
    user_message: str,
    current_thread_id: str,
) -> List[Dict[str, Any]]:
    """检索与当前问题匹配且未过期的事实记忆（排除本会话自己保存的）。

    匹配策略（双通道）：
    1. 主题/内容匹配：用户消息与记忆的"主题+事实内容"词元交集（bigram 为主）
       ——事实句（≤150字）整体参与匹配，问"赛力斯7月销量"可直接命中
       "赛力斯2026年7月新能源销量24,229辆"这类落库事实；
    2. 关联主体匹配：用户消息提及的股票（如"牧原股份"/"002714"，含名称→代码
       映射）与记忆保存时记录的 related_terms 交集——行业类记忆（如
       "生猪养殖成本"）借此在个股问题（如"牧原反弹了吗"）中被召回。
    """
    try:
        now_iso = datetime.now().isoformat(timespec="seconds")
        user_tokens = _extract_match_tokens(user_message or "")
        name_map = await stock_directory.get_name_map(db)
        user_terms = set(_extract_related_terms(user_message, name_map=name_map))
        if not user_tokens and not user_terms:
            return []
        cursor = (
            db[_MEMORY_FACTS_COLLECTION]
            .find({"valid_until": {"$gte": now_iso}})
            .sort("fetched_at", -1)
            .limit(100)
        )
        scored = []
        async for doc in cursor:
            if current_thread_id and doc.get("thread_id") == current_thread_id:
                continue  # 本会话已有该数据在上下文里，无需重复注入
            match_text = f"{doc.get('topic') or ''} {doc.get('content') or ''}"
            topic_tokens = _extract_match_tokens(match_text)
            overlap = user_tokens & topic_tokens
            related_hit = bool(
                user_terms & set(doc.get("related_terms") or [])
            )
            if len(overlap) >= _MEMORY_MATCH_THRESHOLD or related_hit:
                # 主题直接命中权重高于关联主体命中
                scored.append((len(overlap) * 10 + (1 if related_hit else 0), doc))
        scored.sort(key=lambda x: -x[0])
        recalled = [doc for _score, doc in scored[:_MEMORY_RECALL_TOP_K]]
        if recalled:
            logger.info(
                "[智能助手·记忆] 命中 %d 条事实记忆: %s",
                len(recalled),
                [d.get("topic", "")[:30] for d in recalled],
            )
        return recalled
    except Exception as e:
        logger.warning("[智能助手·记忆] 检索事实记忆失败（降级为无记忆）: %s", e)
        return []


def _build_memory_injection(recalled: List[Dict[str, Any]]) -> str:
    """将命中的记忆构建为注入 prompt 的文本区块。"""
    if not recalled:
        return ""
    blocks = [
        "【历史事实记忆】以下是之前会话中通过工具获取的真实数据（已通过时效校验），"
        "可直接引用，但引用时必须标注数据采集日期；若用户在追问最新情况，"
        "应主动说明数据采集时间并建议重新核实。"
    ]
    for i, doc in enumerate(recalled, 1):
        fetched = str(doc.get("fetched_at", ""))[:16].replace("T", " ")
        topic = doc.get("topic", "")
        content = str(doc.get("content", ""))[:_MEMORY_INJECT_MAX_CHARS]
        blocks.append(
            f"--- 记忆 {i} | 采集于 {fetched} | 来源 {doc.get('tool_id', '')} | 主题：{topic} ---\n{content}"
        )
    return "\n\n".join(blocks)


def _should_force_tool_retry(user_message: str) -> bool:
    """判断是否属于“应优先调用工具”的个人数据查询请求。"""
    text = (user_message or "").strip().lower()
    if not text:
        return False

    # 个人数据+操作查询意图（计划/持仓/复盘/任务/报告等）
    personal_markers = ["我的", "我", "当前", "现在"]
    domain_markers = [
        "交易计划", "投资计划", "计划", "持仓", "仓位", "复盘", "报告", "分析任务", "任务状态",
        "股票关注列表", "定时分析", "提醒", "schedule", "watchlist", "position", "review", "plan",
    ]
    query_markers = ["查看", "列出", "查询", "获取", "多少", "有没有", "是什么", "详情", "状态"]

    has_personal = any(k in text for k in personal_markers)
    has_domain = any(k in text for k in domain_markers)
    has_query = any(k in text for k in query_markers)

    market_markers = [
        "大盘", "指数", "上证", "深证", "创业板", "沪深300", "中证", "点位", "收盘", "开盘",
        "成交额", "量能", "涨跌家数", "北向", "南向", "主力资金", "技术面", "技术指标",
        "macd", "rsi", "kdj", "boll", "布林", "均线", "ma", "ema", "超买", "超卖",
        "背离", "支撑", "阻力", "回调", "反弹", "突破", "压力位", "自然周期",
    ]
    recency_markers = ["最新", "当前", "现在", "今日", "今天", "近期", "最近", "截至目前", "今年", "本周", "本月"]

    has_market = any(k in text for k in market_markers)
    has_recency = any(k in text for k in recency_markers)

    # 至少满足：个人+领域，或 领域+查询，或 市场+近期，或 股票+数据
    # 股票数据类问题：股票代码/名称 + 数据相关词汇（如"牧原股份值得入手吗"）
    import re
    has_stock_code = bool(re.search(r"(00|30|60|68)\d{4}", text))
    has_stock_name = any(k in text for k in ["股份", "集团", "科技", "新能", "汽车", "银行", "证券", "保险", "能源", "牧原", "赛力斯", "比亚迪", "宁德"])
    stock_data_markers = [
        "财报", "利润", "营收", "收入", "净利润", "毛利率", "现金流", "资产", "负债",
        "估值", "市盈率", "市净率", "pe", "pb", "ps",
        "销量", "产量", "出栏", "交付",
        "价格", "股价", "行情", "涨跌", "涨幅", "跌幅",
        "入手", "买入", "卖出", "加仓", "减仓", "持有", "清仓", "值得", "能不能买", "该不该",
        "分析", "研究", "怎么样", "如何",
    ]
    has_stock_data = any(k in text for k in stock_data_markers)
    stock_data_query = (has_stock_code or has_stock_name) and has_stock_data

    return (has_personal and has_domain) or (has_domain and has_query) or (has_market and has_recency) or stock_data_query


async def _record_assistant_token_usage(
    resp_obj: Any,
    session_id: str,
    analysis_type: str = "assistant",
) -> None:
    """记录 LLM 调用的 token 使用情况（失败不影响主流程）。"""
    try:
        from app.services.usage_statistics_service import usage_statistics_service
        await usage_statistics_service.record_llm_usage(
            response=resp_obj,
            session_id=session_id,
            analysis_type=analysis_type,
        )
    except Exception as e:
        logger.warning(f"记录 token 使用失败: {e}")


# =============================================================================
# 用户记忆元工具：让 LLM 在用户表达"请记住""请帮我记下"等意图时主动登记
# 设计文档参考：docs/05-design/v3.0/unified-memory-layer-mem0.md §15.5 P0-1
# =============================================================================

REMEMBER_THIS_TOOL_NAME = "remember_this"
