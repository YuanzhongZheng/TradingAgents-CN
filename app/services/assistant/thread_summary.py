"""智能助手：thread_summary。"""
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



def _extract_sentences(text: str) -> List[str]:
    raw = (text or "").replace("\r", "\n")
    fragments: List[str] = []
    for chunk in raw.split("\n"):
        chunk = chunk.strip(" -•\t")
        if not chunk:
            continue
        parts = [part.strip() for part in chunk.replace("！", "。").replace("？", "。").split("。")]
        for part in parts:
            if part:
                fragments.append(part)
    return fragments


def _format_messages_for_summary(messages: List[Dict[str, Any]], limit: int = 12) -> str:
    """格式化最近消息供主题摘要使用。

    双层预算：最近 limit 条、每条 800 字符，总量封顶 6000 字符——
    摘要只需把握主题焦点，不需要完整正文；超长输入既费 token 又抬高
    慢端点下的超时概率。超预算时优先保留最近消息（从尾部累积）。
    """
    _PER_MSG_BUDGET = 800
    _TOTAL_BUDGET = 6000
    recent = messages[-limit:]
    # 倒序累积（优先保住最新消息），最后再按时间顺序输出
    picked: List[str] = []
    used = 0
    for back_index, message in enumerate(reversed(recent), start=1):
        role = str(message.get("role") or "assistant").strip() or "assistant"
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        content = re.sub(r"\s+", " ", content)
        index = len(recent) - back_index + 1
        line = f"{index}. [{role}] {content[:_PER_MSG_BUDGET]}"
        if used + len(line) > _TOTAL_BUDGET:
            # 更老的消息不再纳入
            break
        picked.append(line)
        used += len(line)
    return "\n".join(reversed(picked))


def _strip_json_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, count=1, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _load_summary_json(text: str) -> Dict[str, Any]:
    cleaned = _strip_json_code_fence(text)
    if not cleaned:
        raise ValueError("摘要模型返回为空")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise ValueError("摘要模型返回的不是 JSON object")
    return data


def _normalize_thread_summary_payload(payload: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    abstract = str(payload.get("abstract") or "").strip()
    focus_score = payload.get("focus_score")
    try:
        focus_score = float(focus_score)
    except (TypeError, ValueError):
        focus_score = fallback.get("focus_score") or 0.0
    focus_score = max(0.0, min(round(float(focus_score), 2), 1.0))

    return {
        "abstract": abstract[:160] or fallback.get("abstract") or "当前主题尚未形成摘要",
        "confirmed_facts": _normalize_summary_items(payload.get("confirmed_facts") or fallback.get("confirmed_facts") or []),
        "open_questions": _normalize_summary_items(payload.get("open_questions") or fallback.get("open_questions") or []),
        "next_actions": _normalize_summary_items(payload.get("next_actions") or fallback.get("next_actions") or []),
        "focus_score": focus_score,
    }


async def _build_thread_summary_with_llm(
    db,
    messages: List[Dict[str, Any]],
    title: str = "",
) -> Dict[str, Any]:
    from app.services.assistant.common import public_assistant_binding

    get_coding_llm_config = public_assistant_binding("get_coding_llm_config")
    UnifiedLLMClient = public_assistant_binding("UnifiedLLMClient")

    fallback = _build_thread_summary_from_messages(messages, title)
    transcript = _format_messages_for_summary(messages)
    if not transcript:
        return fallback

    try:
        llm_config = await get_coding_llm_config(db)
        if not llm_config:
            logger.warning("[智能助手摘要] 未找到可用 LLM 配置，回退规则摘要")
            return fallback

        summary_config = llm_config.model_copy(deep=True) if hasattr(llm_config, "model_copy") else llm_config.copy(deep=True)
        summary_config.temperature = 0.1
        summary_config.max_tokens = min(int(summary_config.max_tokens or 600), 600)
        # 摘要是后台展示任务（失败即回退规则摘要），超时与重试必须收紧：
        # 输出仅 600 token 的 JSON，正常几秒返回；25s 不返回基本是端点卡死，
        # 再指数退避重试 3 次（旧配置 45s×3 ≈ 2.5 分钟）毫无收益。
        summary_config.timeout = 25
        summary_config.retry_times = 0

        client = UnifiedLLMClient.from_config(summary_config)
        messages_for_llm = [
            Message(
                role=MessageRole.SYSTEM,
                content=(
                    "你是研究主题摘要器。你的任务是为主题会话生成结构化摘要，供侧边栏和主题卡片展示。\n"
                    "必须只输出一个 JSON object，不要输出 markdown、代码块、解释、前后缀。\n"
                    "字段要求：\n"
                    "- abstract: 1-2 句中文摘要，40-90 字，突出当前结论或讨论焦点，不要复制大段原文，不要保留 markdown 标题、表格、分隔线。\n"
                    "- confirmed_facts: 1-3 条已被会话明确给出的事实，必须具体，不要写空话。\n"
                    "- open_questions: 0-3 条仍待回答的问题；如果问题已经回答，不要重复放进去。\n"
                    "- next_actions: 1-3 条下一步建议，必须贴合当前主题。\n"
                    "- focus_score: 0 到 1 之间的小数，表示当前主题是否聚焦。\n"
                    "如果信息不足，宁可简短，也不要抄录正文碎片。"
                ),
            ),
            Message(
                role=MessageRole.USER,
                content=(
                    f"主题标题: {title or '未命名主题'}\n"
                    "最近消息如下：\n"
                    f"{transcript}\n\n"
                    "请输出 JSON：\n"
                    "{\n"
                    '  "abstract": "",\n'
                    '  "confirmed_facts": [],\n'
                    '  "open_questions": [],\n'
                    '  "next_actions": [],\n'
                    '  "focus_score": 0.0\n'
                    "}"
                ),
            ),
        ]

        response = await client.achat(messages_for_llm)
        await _record_assistant_token_usage(response, "assistant_summary", analysis_type="assistant_summary")
        payload = _load_summary_json(response.content or "")
        summary = _normalize_thread_summary_payload(payload, fallback)
        logger.info("[智能助手摘要] 使用 LLM 生成结构化摘要成功")
        return summary
    except Exception as exc:
        logger.warning("[智能助手摘要] LLM 摘要生成失败，回退规则摘要: %s", exc)
        return fallback


def _build_thread_summary_from_messages(messages: List[Dict[str, Any]], title: str = "") -> Dict[str, Any]:
    user_messages = [m.get("content", "").strip() for m in messages if m.get("role") == "user" and m.get("content")]
    assistant_messages = [m.get("content", "").strip() for m in messages if m.get("role") == "assistant" and m.get("content")]

    latest_user = user_messages[-1] if user_messages else ""
    latest_assistant = assistant_messages[-1] if assistant_messages else ""
    abstract = latest_assistant[:160] or latest_user[:160] or (title[:160] if title else "") or "当前主题尚未形成摘要"

    confirmed_candidates: List[str] = []
    for content in reversed(assistant_messages[-4:]):
        confirmed_candidates.extend(_extract_sentences(content))

    question_candidates: List[str] = []
    for content in reversed(user_messages[-4:]):
        question_candidates.extend(_extract_sentences(content))

    next_actions: List[str] = []
    if latest_user:
        next_actions.append(f"围绕“{latest_user[:40]}”继续追问或补数")
    if latest_assistant:
        next_actions.append("结合当前结论继续细化估值、行业或风险主线")
    if not next_actions:
        next_actions.append("继续在当前主题下补充问题或触发深度分析")

    total_messages = len(messages)
    assistant_ratio = len(assistant_messages) / total_messages if total_messages else 0
    focus_score = 0.55
    if total_messages >= 4:
        focus_score += 0.1
    if total_messages >= 8:
        focus_score += 0.05
    if latest_user and title and any(token for token in title.split() if token and token in latest_user):
        focus_score += 0.1
    if assistant_ratio >= 0.4:
        focus_score += 0.1
    focus_score = min(round(focus_score, 2), 0.95)

    return {
        "abstract": abstract,
        "confirmed_facts": _normalize_summary_items(confirmed_candidates),
        "open_questions": _normalize_summary_items(question_candidates),
        "next_actions": _normalize_summary_items(next_actions),
        "focus_score": focus_score,
    }


async def _create_thread_document(
    db,
    user_id: str,
    thread_id: str,
    title: str,
    parent_thread_id: Optional[str] = None,
    topic_type: str = "general",
) -> Dict[str, Any]:
    now = _now_utc()
    doc = {
        "thread_id": thread_id,
        "user_id": user_id,
        "title": title,
        "parent_thread_id": parent_thread_id,
        "topic_type": topic_type,
        "pinned": thread_id == ASSISTANT_DEFAULT_THREAD_ID,
        "archived": False,
        "message_count": 0,
        "last_user_message": "",
        "report_refs": [],
        "current_summary": {
            "abstract": "",
            "confirmed_facts": [],
            "open_questions": [],
            "next_actions": [],
            "focus_score": 0.0,
        },
        "created_at": now,
        "updated_at": now,
    }
    await db.assistant_threads.update_one(
        {"user_id": user_id, "thread_id": thread_id},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return await db.assistant_threads.find_one({"user_id": user_id, "thread_id": thread_id})


async def _migrate_legacy_conversation_if_needed(db, user_id: str, thread_id: str) -> None:
    if thread_id != ASSISTANT_DEFAULT_THREAD_ID:
        return

    message_count = await db.assistant_thread_messages.count_documents({
        "user_id": user_id,
        "thread_id": thread_id,
    })
    if message_count > 0:
        return

    legacy_doc = await db.assistant_conversations.find_one({"user_id": user_id})
    legacy_messages = (legacy_doc or {}).get("messages") or []
    if not legacy_messages:
        return

    now = _now_utc()
    payload = []
    last_user_message = ""
    for idx, message in enumerate(legacy_messages):
        content = (message.get("content") or "").strip()
        if not content:
            continue
        role = message.get("role") or "assistant"
        if role == "user":
            last_user_message = content
        payload.append({
            "message_id": f"legacy_{idx}_{uuid.uuid4().hex[:8]}",
            "thread_id": thread_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "tools_used": message.get("tools_used") or [],
            "created_at": now,
        })

    if not payload:
        return

    await db.assistant_thread_messages.insert_many(payload)
    abstract = ""
    for message in reversed(payload):
        if message["role"] == "assistant":
            abstract = message["content"][:120]
            break

    await db.assistant_threads.update_one(
        {"user_id": user_id, "thread_id": thread_id},
        {
            "$set": {
                "message_count": len(payload),
                "last_user_message": last_user_message,
                "current_summary": {
                    "abstract": abstract,
                    "confirmed_facts": [],
                    "open_questions": [],
                    "next_actions": [],
                    "focus_score": 0.5,
                },
                "updated_at": now,
            }
        },
    )


async def get_or_create_assistant_thread(
    db,
    user_id: str,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    thread_id = (conversation_id or ASSISTANT_DEFAULT_THREAD_ID).strip() if conversation_id else ASSISTANT_DEFAULT_THREAD_ID
    topic_type = "im_session" if _is_external_session_thread_id(thread_id) else "general"
    doc = await db.assistant_threads.find_one({"user_id": user_id, "thread_id": thread_id})
    if doc is None:
        title = "默认主题" if thread_id == ASSISTANT_DEFAULT_THREAD_ID else ("外部会话" if topic_type == "im_session" else f"新主题 {datetime.now().strftime('%m-%d %H:%M')}")
        doc = await _create_thread_document(db, user_id, thread_id, title, topic_type=topic_type)
    elif topic_type == "im_session" and doc.get("topic_type") != "im_session":
        await db.assistant_threads.update_one(
            {"user_id": user_id, "thread_id": thread_id},
            {
                "$set": {
                    "topic_type": "im_session",
                    "title": doc.get("title") or "外部会话",
                    "updated_at": _now_utc(),
                }
            },
        )
    await _migrate_legacy_conversation_if_needed(db, user_id, thread_id)
    return await db.assistant_threads.find_one({"user_id": user_id, "thread_id": thread_id})


async def list_assistant_threads(db, user_id: str) -> List[Dict[str, Any]]:
    await get_or_create_assistant_thread(db, user_id)
    items: List[Dict[str, Any]] = []
    cursor = db.assistant_threads.find({
        "user_id": user_id,
        "archived": {"$ne": True},
    }).sort([("pinned", -1), ("parent_thread_id", 1), ("updated_at", -1)])
    async for doc in cursor:
        if _is_visible_assistant_topic(doc):
            items.append(_thread_doc_to_item(doc))
    return items


async def create_assistant_thread(
    db,
    user_id: str,
    title: Optional[str] = None,
    parent_thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_parent_id = (parent_thread_id or "").strip() or None
    if normalized_parent_id:
        parent = await db.assistant_threads.find_one({
            "user_id": user_id,
            "thread_id": normalized_parent_id,
            "archived": {"$ne": True},
        })
        if not parent:
            raise ValueError("父主题不存在或已删除")

    thread_id = _generate_thread_id()
    thread_title = (title or "").strip() or f"新主题 {datetime.now().strftime('%m-%d %H:%M')}"
    existing_docs = await db.assistant_threads.find({
        "user_id": user_id,
        "archived": {"$ne": True},
    }).to_list(length=None)
    existing_docs = [doc for doc in existing_docs if _is_visible_assistant_topic(doc)]
    similar_doc = _find_similar_thread(existing_docs, thread_title, normalized_parent_id)
    if similar_doc:
        return _thread_doc_to_item(similar_doc)

    doc = await _create_thread_document(
        db,
        user_id,
        thread_id,
        thread_title,
        parent_thread_id=normalized_parent_id,
        topic_type="subtopic" if normalized_parent_id else "general",
    )
    return _thread_doc_to_item(doc)


async def delete_assistant_thread(db, user_id: str, thread_id: str) -> List[str]:
    normalized_thread_id = (thread_id or "").strip()
    if not normalized_thread_id:
        raise ValueError("主题 ID 不能为空")
    if normalized_thread_id == ASSISTANT_DEFAULT_THREAD_ID:
        raise ValueError("默认主题不能删除")

    docs = await db.assistant_threads.find({
        "user_id": user_id,
        "archived": {"$ne": True},
    }).to_list(length=None)
    docs = [doc for doc in docs if _is_visible_assistant_topic(doc)]

    by_id = {doc.get("thread_id"): doc for doc in docs}
    if normalized_thread_id not in by_id:
        raise ValueError("主题不存在或已删除")

    children_by_parent: Dict[str, List[str]] = {}
    for doc in docs:
        parent_id = doc.get("parent_thread_id")
        child_id = doc.get("thread_id")
        if parent_id and child_id:
            children_by_parent.setdefault(parent_id, []).append(child_id)

    pending = [normalized_thread_id]
    deleted_ids: List[str] = []
    while pending:
        current_id = pending.pop()
        if current_id in deleted_ids:
            continue
        deleted_ids.append(current_id)
        pending.extend(children_by_parent.get(current_id, []))

    await db.assistant_thread_messages.delete_many({
        "user_id": user_id,
        "thread_id": {"$in": deleted_ids},
    })
    await db.assistant_thread_checkpoints.delete_many({
        "user_id": user_id,
        "thread_id": {"$in": deleted_ids},
    })
    await db.assistant_threads.delete_many({
        "user_id": user_id,
        "thread_id": {"$in": deleted_ids},
    })

    return deleted_ids


async def get_assistant_thread_messages(
    db,
    user_id: str,
    conversation_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    thread = await get_or_create_assistant_thread(db, user_id, conversation_id)
    messages: List[Dict[str, Any]] = []
    cursor = db.assistant_thread_messages.find({
        "user_id": user_id,
        "thread_id": thread["thread_id"],
    }).sort("created_at", 1)
    async for doc in cursor:
        messages.append({
            "role": doc.get("role", "assistant"),
            "content": doc.get("content", ""),
            "tools_used": doc.get("tools_used") or None,
        })
    return _thread_doc_to_item(thread), messages


async def append_assistant_message(
    db,
    user_id: str,
    role: str,
    content: str,
    conversation_id: Optional[str] = None,
    tools_used: Optional[List[str]] = None,
) -> str:
    thread = await get_or_create_assistant_thread(db, user_id, conversation_id)
    thread_id = thread["thread_id"]
    text = (content or "").strip()
    if not text:
        return thread_id

    await db.assistant_thread_messages.insert_one({
        "message_id": f"msg_{uuid.uuid4().hex}",
        "thread_id": thread_id,
        "user_id": user_id,
        "role": role,
        "content": text,
        "tools_used": tools_used or [],
        "created_at": _now_utc(),
    })

    updates: Dict[str, Any] = {"updated_at": _now_utc()}
    if role == "user":
        updates["last_user_message"] = text[:200]

    await db.assistant_threads.update_one(
        {"user_id": user_id, "thread_id": thread_id},
        {
            "$set": updates,
            "$inc": {"message_count": 1},
        },
    )
    return thread_id


async def summarize_assistant_thread(
    db,
    user_id: str,
    conversation_id: Optional[str] = None,
    summary_type: str = "manual",
) -> Dict[str, Any]:
    thread = await get_or_create_assistant_thread(db, user_id, conversation_id)
    _, messages = await get_assistant_thread_messages(db, user_id, thread["thread_id"])
    summary = await _build_thread_summary_with_llm(db, messages[-12:], thread.get("title") or "")
    now = _now_utc()

    await db.assistant_threads.update_one(
        {"user_id": user_id, "thread_id": thread["thread_id"]},
        {
            "$set": {
                "current_summary": summary,
                "updated_at": now,
            }
        },
    )

    await db.assistant_thread_checkpoints.insert_one({
        "checkpoint_id": f"chk_{uuid.uuid4().hex[:12]}",
        "thread_id": thread["thread_id"],
        "user_id": user_id,
        "summary_type": summary_type,
        "summary_text": summary.get("abstract", ""),
        "structured_summary": summary,
        "created_at": now,
    })

    await _store_thread_summary_memory(
        db,
        user_id=user_id,
        thread_id=thread["thread_id"],
        title=thread.get("title") or "",
        summary=summary,
        summary_type=summary_type,
    )

    return summary


async def _store_thread_summary_memory(
    db,
    *,
    user_id: str,
    thread_id: str,
    title: str,
    summary: Dict[str, Any],
    summary_type: str,
) -> None:
    """主题摘要只保留在 Mongo 线程摘要集合，不再写入 mem0 长期记忆。"""
    return


async def save_assistant_message(
    db,
    user_id: str,
    user_content: str,
    assistant_content: str,
    tools_used: List[str],
    conversation_id: Optional[str] = None,
) -> None:
    """将会话追加写入 MongoDB。

    消息持久化同步完成（用户刷新页面必须能看到本轮对话）；
    主题摘要（侧边栏卡片用，LLM 生成、失败有规则摘要兜底）改为后台任务，
    绝不阻塞回复返回——历史教训：coding 端点偶发慢响应时，45s 超时 × 3 次
    指数退避重试让用户为一个纯展示维护任务干等约 2.5 分钟。
    """
    from app.services.assistant.common import public_assistant_binding

    append_assistant_message = public_assistant_binding("append_assistant_message")
    thread_id = await append_assistant_message(
        db,
        user_id=user_id,
        role="user",
        content=user_content,
        conversation_id=conversation_id,
    )
    await append_assistant_message(
        db,
        user_id=user_id,
        role="assistant",
        content=assistant_content,
        conversation_id=thread_id,
        tools_used=tools_used,
    )
    _schedule_thread_summary(db, user_id, thread_id)


def _schedule_thread_summary(db, user_id: str, thread_id: str) -> None:
    """后台生成线程摘要（fire-and-forget）。

    必须自行兜底全部异常：create_task 的异常若无人消费会抛
    "Task exception was never retrieved"。
    """
    from app.services.assistant.common import public_assistant_binding

    summarize_assistant_thread = public_assistant_binding("summarize_assistant_thread")

    async def _run() -> None:
        try:
            await summarize_assistant_thread(
                db,
                user_id=user_id,
                conversation_id=thread_id,
                summary_type="auto",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[智能助手摘要] 后台摘要任务失败（不影响会话）: %s", exc)

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        # 无运行中的事件循环（极端场景）：摘要本就是可丢弃的展示任务，直接跳过
        logger.debug("[智能助手摘要] 无事件循环，跳过本轮后台摘要")


async def get_assistant_conversation(
    db,
    user_id: str,
    conversation_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """获取用户的智能助手会话历史"""
    _, messages = await get_assistant_thread_messages(db, user_id, conversation_id)
    return messages


async def clear_assistant_conversation(
    db,
    user_id: str,
    conversation_id: Optional[str] = None,
) -> None:
    """清空用户的智能助手会话"""
    thread = await get_or_create_assistant_thread(db, user_id, conversation_id)
    await db.assistant_thread_messages.delete_many(
        {"user_id": user_id, "thread_id": thread["thread_id"]}
    )
    await db.assistant_threads.update_one(
        {"user_id": user_id, "thread_id": thread["thread_id"]},
        {
            "$set": {
                "message_count": 0,
                "last_user_message": "",
                "current_summary": {
                    "abstract": "",
                    "confirmed_facts": [],
                    "open_questions": [],
                    "next_actions": [],
                    "focus_score": 0.0,
                },
                "updated_at": _now_utc(),
            }
        },
    )


async def load_user_profile(db, user_id: str) -> Optional[dict]:
    """从 assistant_user_profiles 集合加载用户画像"""
    try:
        doc = await db.assistant_user_profiles.find_one({"user_id": user_id})
        return doc
    except Exception as e:
        logger.warning("[智能助手] 加载用户画像失败: %s", e)
    return None


async def update_user_profile_with_analysis(
    db,
    user_id: str,
    symbol: str,
    name: str,
    conclusion: str,
    task_id: str,
) -> None:
    """
    分析任务完成后，将结果追加到用户画像的 recently_analyzed 列表。
    保留最近 20 条，upsert 方式写入 assistant_user_profiles 集合。
    """
    try:
        now = datetime.now(timezone.utc)
        entry = {
            "symbol": symbol,
            "name": name,
            "analyzed_at": now.strftime("%Y-%m-%d"),
            "conclusion": conclusion,
            "task_id": task_id,
        }
        await db.assistant_user_profiles.update_one(
            {"user_id": user_id},
            {
                "$push": {
                    "recently_analyzed": {
                        "$each": [entry],
                        "$slice": -20,
                    }
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        logger.info(f"[用户画像] ✅ 已更新 {user_id} 的 recently_analyzed: {symbol}({name})")
    except Exception as e:
        logger.warning(f"[用户画像] 更新失败（已忽略）: {e}")


async def _load_assistant_settings(db, user_id: str) -> Optional[dict]:
    """从数据库加载用户的助理个性化设置"""
    try:
        from bson import ObjectId
        user_doc = await db.users.find_one(
            {"_id": ObjectId(user_id)},
            {"preferences.assistant_settings": 1},
        )
        if user_doc:
            prefs = user_doc.get("preferences", {})
            return prefs.get("assistant_settings")
    except Exception as e:
        logger.warning("[智能助手] 加载助理设置失败: %s", e)
    return None


# ------------------------------------------------------------------
#  mem0 统一记忆层 — 助手对话集成
# ------------------------------------------------------------------

async def _recall_memory_block(db, user_id: str, user_message: str) -> str:
    """从 mem0 召回与当前用户/问题相关的记忆，格式化为可注入 prompt 的文本。

    召回范围：
    - user_preference: 用户偏好（通过 remember_this 显式登记）
    - trade_pattern:   交易纪律记忆（复盘完成后写入）
    """
    try:
        from core.memory.service import get_memory_service
        svc = get_memory_service(db)
        return await svc.recall_formatted(
            query=user_message,
            user_id=user_id,
            scopes=["user_preference", "trade_pattern"],
            limit=6,
            max_chars=800,
        )
    except Exception as e:
        logger.debug("[mem0] 记忆召回失败（已忽略）: %s", e)
        return ""


from app.services.assistant.memory import (
    _record_assistant_token_usage,
)
from app.services.assistant.llm_config import (
    get_coding_llm_config,
)
from app.services.assistant.threads import (
    _find_similar_thread,
    _generate_thread_id,
    _is_external_session_thread_id,
    _is_visible_assistant_topic,
    _normalize_summary_items,
    _now_utc,
    _thread_doc_to_item,
)
