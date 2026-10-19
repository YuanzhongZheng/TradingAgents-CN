"""智能助手：threads。"""
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




def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _generate_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex[:12]}"


def _normalize_thread_title(title: str) -> str:
    text = (title or "").strip().lower()
    if not text:
        return ""

    replacements = {
        "新能源汽车": "新能源车",
        "新能源汽車": "新能源车",
        "个股": "公司",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    for token in ["（", "）", "(", ")", "-", "_", " ", "　", "/", "\\", "·", "、"]:
        text = text.replace(token, "")

    removable_suffixes = [
        "研究主题",
        "产业链研究",
        "行业研究",
        "板块研究",
        "公司研究",
        "个股研究",
        "深度研究",
        "专题研究",
        "课题研究",
        "产业链",
        "行业",
        "板块",
        "研究",
        "分析",
        "跟踪",
        "专题",
        "主题",
    ]
    changed = True
    while changed and len(text) > 2:
        changed = False
        for suffix in removable_suffixes:
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = text[: -len(suffix)]
                changed = True
                break

    return text


def _thread_titles_match(first: str, second: str) -> bool:
    normalized_first = _normalize_thread_title(first)
    normalized_second = _normalize_thread_title(second)
    if not normalized_first or not normalized_second:
        return False
    return normalized_first == normalized_second


def _find_similar_thread(
    docs: List[Dict[str, Any]],
    title: str,
    parent_thread_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    normalized_parent_id = (parent_thread_id or "").strip() or None
    candidates = [
        doc for doc in docs
        if (doc.get("parent_thread_id") or None) == normalized_parent_id
    ]
    for doc in candidates:
        if _thread_titles_match(doc.get("title") or "", title):
            return doc
    return None


def _is_external_session_thread_id(thread_id: Optional[str]) -> bool:
    return bool(thread_id and ":" in thread_id)


def _is_visible_assistant_topic(doc: Dict[str, Any]) -> bool:
    if doc.get("archived") is True:
        return False
    if (doc.get("topic_type") or "") == "im_session":
        return False
    return not _is_external_session_thread_id(doc.get("thread_id"))


def _sanitize_thread_summary(summary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not summary:
        return None

    abstract = str(summary.get("abstract") or "").strip()
    confirmed_facts = summary.get("confirmed_facts") or []
    open_questions = summary.get("open_questions") or []
    next_actions = summary.get("next_actions") or []
    focus_score = float(summary.get("focus_score") or 0.0)

    # 历史坏数据特征：只有 abstract，被 assistant 原文切片覆盖，且仍保留初始化占位值。
    if (
        abstract
        and not confirmed_facts
        and not open_questions
        and not next_actions
        and focus_score <= 0
        and (
            "##" in abstract
            or "|" in abstract
            or "---" in abstract
            or "**" in abstract
            or len(abstract) > 100
        )
    ):
        return None

    return {
        "abstract": abstract,
        "confirmed_facts": confirmed_facts,
        "open_questions": open_questions,
        "next_actions": next_actions,
        "focus_score": focus_score,
    }


def _thread_doc_to_item(doc: Dict[str, Any]) -> Dict[str, Any]:
    summary = _sanitize_thread_summary(doc.get("current_summary") or {})
    updated_at = doc.get("updated_at")
    if isinstance(updated_at, datetime):
        updated_at = updated_at.isoformat()
    report_refs = []
    for ref in doc.get("report_refs") or []:
        created_at = ref.get("created_at")
        linked_at = ref.get("linked_at")
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()
        if isinstance(linked_at, datetime):
            linked_at = linked_at.isoformat()
        report_refs.append({
            "ref_type": ref.get("ref_type") or "unknown",
            "report_key": ref.get("report_key") or "",
            "source_collection": ref.get("source_collection") or "",
            "title": ref.get("title") or "未命名报告",
            "symbol": ref.get("symbol") or None,
            "summary": ref.get("summary") or "",
            "status": ref.get("status") or None,
            "task_id": ref.get("task_id") or None,
            "analysis_id": ref.get("analysis_id") or None,
            "created_at": created_at,
            "linked_at": linked_at,
        })
    return {
        "thread_id": doc.get("thread_id", ASSISTANT_DEFAULT_THREAD_ID),
        "title": doc.get("title") or "默认主题",
        "parent_thread_id": doc.get("parent_thread_id"),
        "topic_type": doc.get("topic_type") or "general",
        "pinned": bool(doc.get("pinned", False)),
        "archived": bool(doc.get("archived", False)),
        "message_count": int(doc.get("message_count", 0)),
        "last_user_message": doc.get("last_user_message", ""),
        "updated_at": updated_at,
        "report_refs": report_refs,
        "current_summary": {
            "abstract": summary.get("abstract") or "",
            "confirmed_facts": summary.get("confirmed_facts") or [],
            "open_questions": summary.get("open_questions") or [],
            "next_actions": summary.get("next_actions") or [],
            "focus_score": float(summary.get("focus_score") or 0.0),
        } if summary else None,
    }


def serialize_assistant_report_ref(ref: Dict[str, Any]) -> Dict[str, Any]:
    created_at = ref.get("created_at")
    linked_at = ref.get("linked_at")
    return {
        "ref_type": ref.get("ref_type") or "unknown",
        "report_key": ref.get("report_key") or "",
        "source_collection": ref.get("source_collection") or "",
        "title": ref.get("title") or "未命名报告",
        "symbol": ref.get("symbol") or None,
        "summary": ref.get("summary") or "",
        "status": ref.get("status") or None,
        "task_id": ref.get("task_id") or None,
        "analysis_id": ref.get("analysis_id") or None,
        "created_at": _to_iso_datetime(created_at),
        "linked_at": _to_iso_datetime(linked_at),
    }


def _normalize_summary_items(items: List[str], limit: int = 3) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for item in items:
        text = (item or "").strip().replace("\n", " ")
        if not text:
            continue
        text = text[:120]
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_report_ref_item(report_ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ref_type = (report_ref.get("ref_type") or "").strip()
    report_key = (report_ref.get("report_key") or report_ref.get("task_id") or report_ref.get("analysis_id") or "").strip()
    if not ref_type or not report_key:
        return None

    now = _now_utc()
    created_at = report_ref.get("created_at")
    if not isinstance(created_at, datetime):
        created_at = now

    return {
        "ref_type": ref_type,
        "report_key": report_key,
        "source_collection": (report_ref.get("source_collection") or "").strip(),
        "title": (report_ref.get("title") or "未命名报告").strip(),
        "symbol": (report_ref.get("symbol") or "").strip() or None,
        "summary": (report_ref.get("summary") or "").strip()[:600],
        "status": (report_ref.get("status") or "").strip() or None,
        "task_id": (report_ref.get("task_id") or "").strip() or None,
        "analysis_id": (report_ref.get("analysis_id") or "").strip() or None,
        "created_at": created_at,
        "linked_at": now,
    }


async def attach_report_ref_to_thread(
    db,
    user_id: str,
    thread_id: Optional[str],
    report_ref: Dict[str, Any],
) -> bool:
    normalized_thread_id = (thread_id or "").strip()
    if not normalized_thread_id or _is_external_session_thread_id(normalized_thread_id):
        return False

    normalized_ref = _normalize_report_ref_item(report_ref)
    if not normalized_ref:
        return False

    thread = await db.assistant_threads.find_one({
        "user_id": user_id,
        "thread_id": normalized_thread_id,
        "archived": {"$ne": True},
    })
    if not thread or (thread.get("topic_type") or "") == "im_session":
        return False

    existing_refs = thread.get("report_refs") or []
    merged_refs = [
        ref for ref in existing_refs
        if not (
            (ref.get("ref_type") or "") == normalized_ref["ref_type"]
            and (ref.get("report_key") or "") == normalized_ref["report_key"]
        )
    ]
    merged_refs.insert(0, normalized_ref)
    merged_refs = merged_refs[:ASSISTANT_MAX_REPORT_REFS]

    await db.assistant_threads.update_one(
        {"user_id": user_id, "thread_id": normalized_thread_id},
        {
            "$set": {
                "report_refs": merged_refs,
                "updated_at": _now_utc(),
            }
        },
    )
    return True


async def resolve_thread_for_report_writeback(
    db,
    user_id: str,
    preferred_thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_thread_id = (preferred_thread_id or "").strip()
    if normalized_thread_id and not _is_external_session_thread_id(normalized_thread_id):
        thread = await db.assistant_threads.find_one({
            "user_id": user_id,
            "thread_id": normalized_thread_id,
            "archived": {"$ne": True},
        })
        if thread and (thread.get("topic_type") or "") != "im_session":
            return thread

    fallback_thread = await create_assistant_thread(
        db,
        user_id=user_id,
        title=ASSISTANT_REPORT_INBOX_TITLE,
        parent_thread_id=None,
    )
    thread = await db.assistant_threads.find_one({
        "user_id": user_id,
        "thread_id": fallback_thread["thread_id"],
        "archived": {"$ne": True},
    })
    return thread or fallback_thread


async def write_report_back_to_assistant_thread(
    db,
    user_id: str,
    report_ref: Dict[str, Any],
    preferred_thread_id: Optional[str] = None,
    summary_message: Optional[str] = None,
) -> Optional[str]:
    target_thread = await resolve_thread_for_report_writeback(db, user_id, preferred_thread_id)
    if not target_thread:
        return None

    target_thread_id = target_thread.get("thread_id")
    attached = await attach_report_ref_to_thread(db, user_id, target_thread_id, report_ref)
    if not attached:
        return None

    text = (summary_message or "").strip()
    if text:
        await append_assistant_message(
            db,
            user_id=user_id,
            role="assistant",
            content=text,
            conversation_id=target_thread_id,
            tools_used=["report_writeback"],
        )
        await summarize_assistant_thread(
            db,
            user_id=user_id,
            conversation_id=target_thread_id,
            summary_type="report_writeback",
        )

    return target_thread_id


def _to_iso_datetime(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    return text or None


def _build_stock_report_detail_content(task: Any) -> str:
    result = task.result or {}
    symbol = task.task_params.get("symbol", "未知") if task.task_params else "未知"
    lines = [f"# {symbol} 分析报告"]

    recommendation = result.get("recommendation")
    if recommendation:
        lines.append(f"\n## 投资建议\n{recommendation}")

    summary = result.get("summary") or result.get("executive_summary")
    if summary:
        lines.append(f"\n## 摘要\n{summary}")

    decision = result.get("decision") or {}
    if decision:
        decision_lines: List[str] = []
        if decision.get("action"):
            decision_lines.append(f"- 操作观点: {decision['action']}")
        if decision.get("confidence") is not None:
            decision_lines.append(f"- 置信度: {decision['confidence']}")
        if decision.get("risk_score") is not None:
            decision_lines.append(f"- 风险分: {decision['risk_score']}")
        reasoning = decision.get("reasoning")
        if reasoning:
            decision_lines.append(f"- 决策依据: {reasoning}")
        if decision_lines:
            lines.append("\n## 决策信息\n" + "\n".join(decision_lines))

    key_points = result.get("key_points") or []
    if key_points:
        lines.append("\n## 关键要点\n" + "\n".join(f"- {item}" for item in key_points[:8]))

    reports = result.get("reports") or {}
    if reports:
        lines.append("\n## 分析师摘录")
        for key, text in reports.items():
            cleaned = str(text or "").strip()
            if not cleaned:
                continue
            lines.append(f"\n### {key}\n{cleaned[:1500]}")

    lines.append(f"\n---\n任务ID: {task.task_id}")
    return "\n".join(lines)


def _build_stock_report_detail_content_from_doc(report_doc: Dict[str, Any]) -> str:
    stock_symbol = (report_doc.get("stock_symbol") or "未知").strip() or "未知"
    stock_name = (report_doc.get("stock_name") or "").strip()
    title = f"{stock_name}({stock_symbol})" if stock_name else stock_symbol
    lines = [f"# {title} 分析报告"]

    recommendation = str(report_doc.get("recommendation") or "").strip()
    if recommendation:
        lines.append(f"\n## 投资建议\n{recommendation}")

    summary = str(report_doc.get("summary") or "").strip()
    if summary:
        lines.append(f"\n## 摘要\n{summary}")

    decision = report_doc.get("decision") or {}
    if isinstance(decision, dict) and decision:
        decision_lines: List[str] = []
        if decision.get("action"):
            decision_lines.append(f"- 操作观点: {decision['action']}")
        if decision.get("confidence") is not None:
            decision_lines.append(f"- 置信度: {decision['confidence']}")
        if decision.get("risk_score") is not None:
            decision_lines.append(f"- 风险分: {decision['risk_score']}")
        reasoning = decision.get("reasoning")
        if reasoning:
            decision_lines.append(f"- 决策依据: {reasoning}")
        if decision_lines:
            lines.append("\n## 决策信息\n" + "\n".join(decision_lines))

    key_points = report_doc.get("key_points") or []
    if key_points:
        lines.append("\n## 关键要点\n" + "\n".join(f"- {item}" for item in key_points[:8]))

    reports = report_doc.get("reports") or {}
    if isinstance(reports, dict) and reports:
        lines.append("\n## 分析师摘录")
        for key, text in reports.items():
            cleaned = str(text or "").strip()
            if not cleaned:
                continue
            lines.append(f"\n### {key}\n{cleaned[:1500]}")

    if report_doc.get("analysis_id"):
        lines.append(f"\n---\n分析ID: {report_doc['analysis_id']}")
    if report_doc.get("task_id"):
        lines.append(f"任务ID: {report_doc['task_id']}")
    return "\n".join(lines)


def _build_position_report_detail_content(report: Dict[str, Any]) -> str:
    code = report.get("code") or "未知"
    name = report.get("name") or code
    lines = [f"# {name}({code}) 持仓分析报告"]

    status = report.get("status")
    action = report.get("action")
    confidence = report.get("confidence")
    meta: List[str] = []
    if status:
        meta.append(f"- 状态: {status}")
    if action:
        meta.append(f"- 研究观察: {action}")
    if confidence is not None:
        meta.append(f"- 置信度: {confidence}")
    if meta:
        lines.append("\n## 核心结论\n" + "\n".join(meta))

    summary = report.get("summary") or {}
    summary_lines: List[str] = []
    if summary.get("quantity") is not None:
        summary_lines.append(f"- 持仓数量: {summary['quantity']}")
    if summary.get("cost_price") is not None:
        summary_lines.append(f"- 成本价: {summary['cost_price']}")
    if summary.get("current_price") is not None:
        summary_lines.append(f"- 当前价: {summary['current_price']}")
    if summary.get("unrealized_pnl") is not None:
        summary_lines.append(f"- 浮动盈亏: {summary['unrealized_pnl']}")
    if summary.get("unrealized_pnl_pct") is not None:
        summary_lines.append(f"- 盈亏比例: {summary['unrealized_pnl_pct']}%")
    if summary_lines:
        lines.append("\n## 持仓概览\n" + "\n".join(summary_lines))

    targets = report.get("price_targets") or {}
    target_lines: List[str] = []
    for label, key in (("关键验证价位", "target_price"), ("风险关注价格", "stop_loss"), ("收益关注价格", "take_profit_price")):
        if targets.get(key) is not None:
            target_lines.append(f"- {label}: {targets[key]}")
    if target_lines:
        lines.append("\n## 关键价位\n" + "\n".join(target_lines))

    action_reason = str(report.get("action_reason") or "").strip()
    if action_reason:
        lines.append(f"\n## 分析详情\n{action_reason}")

    detailed = str(report.get("detailed_analysis") or "").strip()
    if detailed:
        lines.append(f"\n## 详细分析\n{detailed}")

    analysis_id = report.get("analysis_id")
    if analysis_id:
        lines.append(f"\n---\n分析ID: {analysis_id}")
    return "\n".join(lines)


def _build_review_report_detail_content(review_data: Dict[str, Any]) -> str:
    """将交易复盘报告数据构建为 Markdown 详情内容。"""
    trade_info = review_data.get("trade_info") or {}
    code = trade_info.get("code") or "未知"
    name = trade_info.get("name") or code
    lines = [f"# {name}({code}) 交易复盘报告"]

    status = review_data.get("status")
    execution_time = review_data.get("execution_time")
    meta: List[str] = []
    if status:
        meta.append(f"- 状态: {status}")
    if execution_time is not None:
        meta.append(f"- 执行耗时: {execution_time:.1f}s")
    source = review_data.get("source")
    if source:
        meta.append(f"- 来源: {'实盘' if source == 'position' else '模拟'}")
    if meta:
        lines.append("\n## 核心信息\n" + "\n".join(meta))

    # 交易概览
    summary_lines: List[str] = []
    if trade_info.get("code"):
        summary_lines.append(f"- 股票代码: {trade_info['code']}")
    if trade_info.get("name"):
        summary_lines.append(f"- 股票名称: {trade_info['name']}")
    if trade_info.get("realized_pnl") is not None:
        summary_lines.append(f"- 实现盈亏: {trade_info['realized_pnl']}")
    if trade_info.get("remaining_quantity") is not None:
        summary_lines.append(f"- 剩余持仓: {trade_info['remaining_quantity']} 股")
    if summary_lines:
        lines.append("\n## 交易概览\n" + "\n".join(summary_lines))

    # AI 复盘内容
    ai_review = review_data.get("ai_review") or {}
    if ai_review.get("summary"):
        lines.append(f"\n## 复盘结论\n{ai_review['summary']}")
    if ai_review.get("strengths"):
        lines.append("\n## 做得好的地方\n" + "\n".join(f"- {s}" for s in ai_review["strengths"]))
    if ai_review.get("weaknesses"):
        lines.append("\n## 需要改进的地方\n" + "\n".join(f"- {w}" for w in ai_review["weaknesses"]))
    if ai_review.get("suggestions"):
        lines.append("\n## 后续观察点\n" + "\n".join(f"- {s}" for s in ai_review["suggestions"]))
    if ai_review.get("plan_deviation"):
        lines.append(f"\n## 计划偏离\n{ai_review['plan_deviation']}")

    review_id = review_data.get("review_id")
    if review_id:
        lines.append(f"\n---\n复盘ID: {review_id}")
    return "\n".join(lines)


async def get_assistant_thread_report_detail(
    db,
    user_id: str,
    thread_id: str,
    ref_type: str,
    report_key: str,
) -> Dict[str, Any]:
    thread = await db.assistant_threads.find_one({
        "user_id": user_id,
        "thread_id": thread_id,
        "archived": {"$ne": True},
    })
    if not thread:
        raise ValueError("主题不存在或已删除")

    target_ref = None
    for ref in thread.get("report_refs") or []:
        if (ref.get("ref_type") or "") == ref_type and (ref.get("report_key") or "") == report_key:
            target_ref = ref
            break
    if not target_ref:
        raise ValueError("当前主题下未找到对应的关联报告")

    detail = {
        "ref_type": target_ref.get("ref_type") or ref_type,
        "report_key": target_ref.get("report_key") or report_key,
        "title": target_ref.get("title") or "未命名报告",
        "symbol": target_ref.get("symbol") or None,
        "status": target_ref.get("status") or None,
        "task_id": target_ref.get("task_id") or None,
        "analysis_id": target_ref.get("analysis_id") or None,
        "created_at": _to_iso_datetime(target_ref.get("created_at")),
        "linked_at": _to_iso_datetime(target_ref.get("linked_at")),
        "content": (target_ref.get("summary") or "").strip(),
        "content_format": "markdown",
    }

    if ref_type == "stock_report":
        from app.services.task_analysis_service import get_task_analysis_service

        task_id = (target_ref.get("task_id") or report_key or "").strip()
        task = await get_task_analysis_service().get_task(task_id) if task_id else None
        if task and str(task.user_id) == str(user_id):
            detail["status"] = str(task.status)
            detail["created_at"] = _to_iso_datetime(task.created_at)
            detail["content"] = _build_stock_report_detail_content(task)
            return detail

        analysis_key = (target_ref.get("analysis_id") or report_key or task_id or "").strip()
        report_doc = await db.analysis_reports.find_one(_build_analysis_report_lookup_query(user_id, analysis_key))
        if not report_doc:
            raise ValueError("原始报告不存在或已被删除")
        detail["status"] = report_doc.get("status") or detail["status"]
        detail["task_id"] = report_doc.get("task_id") or detail["task_id"]
        detail["analysis_id"] = report_doc.get("analysis_id") or detail["analysis_id"]
        detail["created_at"] = _to_iso_datetime(report_doc.get("created_at"))
        detail["content"] = _build_stock_report_detail_content_from_doc(report_doc)
        return detail

    if ref_type == "position_report":
        from app.services.portfolio_service import get_portfolio_service

        analysis_id = (target_ref.get("analysis_id") or report_key or "").strip()
        report = await get_portfolio_service().get_position_analysis_by_id(user_id, analysis_id)
        if not report:
            raise ValueError("原始持仓报告不存在或已被删除")
        detail["created_at"] = _to_iso_datetime(report.get("created_at"))
        detail["content"] = _build_position_report_detail_content(report)
        detail["status"] = report.get("status") or detail["status"]
        return detail

    if ref_type == "review_report":
        from app.services.trade_review_service import TradeReviewService

        review_id = (target_ref.get("task_id") or report_key or "").strip()
        service = TradeReviewService()
        review = await service.get_review_detail(user_id, review_id)
        if not review:
            raise ValueError("原始交易复盘报告不存在或已被删除")
        review_data = review.model_dump() if hasattr(review, "model_dump") else dict(review)
        detail["created_at"] = _to_iso_datetime(review_data.get("created_at"))
        detail["status"] = review_data.get("status") or detail["status"]
        detail["content"] = _build_review_report_detail_content(review_data)
        return detail

    if ref_type == "position_task":
        task_id = (target_ref.get("task_id") or report_key or "").strip()
        task_doc = await db.unified_analysis_tasks.find_one({"task_id": task_id})
        if not task_doc:
            raise ValueError("原始任务不存在或已被删除")
        if str(task_doc.get("user_id")) != str(user_id):
            raise ValueError("无权访问该任务")

        result = task_doc.get("result") or {}
        analysis_id = (result.get("analysis_id") or target_ref.get("analysis_id") or "").strip()
        if analysis_id:
            from app.services.portfolio_service import get_portfolio_service

            report = await get_portfolio_service().get_position_analysis_by_id(user_id, analysis_id)
            if report:
                detail["analysis_id"] = analysis_id
                detail["created_at"] = _to_iso_datetime(report.get("created_at"))
                detail["status"] = report.get("status") or detail["status"]
                detail["content"] = _build_position_report_detail_content(report)
                return detail

        status = task_doc.get("status") or detail["status"] or "unknown"
        detail["status"] = str(status)
        detail["created_at"] = _to_iso_datetime(task_doc.get("created_at"))
        detail["content"] = (
            f"# {detail['title']}\n\n"
            f"- 任务状态: {status}\n"
            f"- 任务ID: {task_id}\n\n"
            f"{(target_ref.get('summary') or '该任务尚未生成完整报告，后续完成后可在这里查看。').strip()}"
        )
        return detail

    return detail


def _build_stock_report_ref_from_task(task: Any, stock_name: Optional[str] = None) -> Dict[str, Any]:
    result = task.result or {}
    task_params = task.task_params or {}
    symbol = (task_params.get("symbol") or "").strip()
    display_name = (stock_name or "").strip()
    title_prefix = f"{display_name}({symbol})" if display_name and symbol else display_name or symbol or "未知标的"
    return {
        "ref_type": "stock_report",
        "report_key": task.task_id,
        "source_collection": "unified_analysis_tasks",
        "title": f"{title_prefix} 分析报告",
        "symbol": symbol or None,
        "summary": ((result.get("summary") or result.get("recommendation") or "")[:400]).strip(),
        "status": str(task.status),
        "task_id": task.task_id,
        "created_at": getattr(task, "created_at", None),
    }


def _build_stock_report_ref_from_analysis_doc(report_doc: Dict[str, Any]) -> Dict[str, Any]:
    stock_symbol = (report_doc.get("stock_symbol") or "").strip()
    stock_name = (report_doc.get("stock_name") or "").strip()
    title_prefix = f"{stock_name}({stock_symbol})" if stock_name and stock_symbol else stock_name or stock_symbol or "未知标的"
    report_key = (report_doc.get("task_id") or report_doc.get("analysis_id") or str(report_doc.get("_id") or "")).strip()
    return {
        "ref_type": "stock_report",
        "report_key": report_key,
        "source_collection": "analysis_reports",
        "title": f"{title_prefix} 分析报告",
        "symbol": stock_symbol or None,
        "summary": ((report_doc.get("summary") or report_doc.get("recommendation") or "")[:400]).strip(),
        "status": report_doc.get("status") or "completed",
        "task_id": (report_doc.get("task_id") or "").strip() or None,
        "analysis_id": (report_doc.get("analysis_id") or "").strip() or None,
        "created_at": report_doc.get("created_at"),
    }


def _build_analysis_report_lookup_query(user_id: str, report_key: str) -> Dict[str, Any]:
    report_query = {
        "$or": [
            {"task_id": report_key},
            {"analysis_id": report_key},
        ]
    }
    if ObjectId.is_valid(report_key):
        report_query["$or"].append({"_id": ObjectId(report_key)})

    return {
        "$and": [
            {"$or": [{"user_id": user_id}, {"user_id": str(user_id)}]},
            report_query,
        ]
    }


def _build_position_report_ref_from_report(report: Dict[str, Any]) -> Dict[str, Any]:
    code = (report.get("code") or "").strip()
    name = (report.get("name") or "").strip()
    title_prefix = f"{name}({code})" if name and code else name or code or "未知持仓"
    return {
        "ref_type": "position_report",
        "report_key": report.get("analysis_id") or "",
        "source_collection": "position_analysis_reports",
        "title": f"{title_prefix} 持仓分析",
        "symbol": code or None,
        "summary": ((report.get("summary") or report.get("action_reason") or "")[:400]).strip(),
        "status": report.get("status") or None,
        "analysis_id": report.get("analysis_id") or None,
        "created_at": report.get("created_at"),
    }


def _build_review_report_ref_from_review(review: Any) -> Dict[str, Any]:
    """从交易复盘报告对象构建主题关联引用。

    兼容 TradeReviewReport 对象(Pydantic) 和字典两种形式。
    """
    if hasattr(review, "model_dump"):
        data = review.model_dump()
    elif hasattr(review, "dict"):
        data = review.dict()
    else:
        data = dict(review)

    trade_info = data.get("trade_info") or {}
    code = (trade_info.get("code") or "").strip()
    name = (trade_info.get("name") or "").strip()
    title_prefix = f"{name}({code})" if name and code else name or code or "未知标的"

    ai_review = data.get("ai_review") or {}
    summary_text = (ai_review.get("summary") or "").strip()

    return {
        "ref_type": "review_report",
        "report_key": data.get("review_id") or data.get("task_id") or "",
        "source_collection": "trade_reviews",
        "title": f"{title_prefix} 交易复盘",
        "symbol": code or None,
        "summary": summary_text[:600],
        "status": data.get("status") or "completed",
        "task_id": data.get("review_id") or data.get("task_id") or None,
        "created_at": data.get("created_at"),
    }


from app.services.assistant.thread_summary import (
    append_assistant_message,
    create_assistant_thread,
    summarize_assistant_thread,
)
