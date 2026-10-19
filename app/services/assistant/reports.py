"""智能助手：reports。"""
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



async def search_assistant_attachable_reports(
    db,
    user_id: str,
    thread_id: str,
    ref_type: str = "stock_report",
    keyword: Optional[str] = None,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    thread = await db.assistant_threads.find_one({
        "user_id": user_id,
        "thread_id": thread_id,
        "archived": {"$ne": True},
    })
    if not thread or (thread.get("topic_type") or "") == "im_session":
        raise ValueError("当前主题不存在或不支持关联报告")

    normalized_type = (ref_type or "stock_report").strip() or "stock_report"
    normalized_keyword = (keyword or "").strip()
    limit = max(1, min(limit, 20))

    if normalized_type == "position_report":
        from app.services.portfolio_service import get_portfolio_service

        query: Dict[str, Any] = {
            "user_id": user_id,
            "analysis_id": {"$exists": True, "$ne": ""},
        }
        if normalized_keyword:
            query["$or"] = [
                {"code": {"$regex": normalized_keyword, "$options": "i"}},
                {"name": {"$regex": normalized_keyword, "$options": "i"}},
                {"summary": {"$regex": normalized_keyword, "$options": "i"}},
                {"analysis_id": {"$regex": normalized_keyword, "$options": "i"}},
            ]

        docs = await db.position_analysis_reports.find(query).sort("created_at", -1).limit(limit).to_list(length=limit)
        service = get_portfolio_service()
        results: List[Dict[str, Any]] = []
        for doc in docs:
            analysis_id = (doc.get("analysis_id") or "").strip()
            if not analysis_id:
                continue
            report = await service.get_position_analysis_by_id(user_id, analysis_id)
            if not report:
                continue
            results.append(_normalize_report_ref_item(_build_position_report_ref_from_report(report)))
        return [item for item in results if item][:limit]

    if normalized_type == "review_report":
        # 交易复盘报告候选：从 trade_reviews 集合查询
        review_query: Dict[str, Any] = {
            "user_id": user_id,
        }
        if normalized_keyword:
            review_query["$or"] = [
                {"trade_info.code": {"$regex": normalized_keyword, "$options": "i"}},
                {"trade_info.name": {"$regex": normalized_keyword, "$options": "i"}},
                {"review_id": {"$regex": normalized_keyword, "$options": "i"}},
                {"ai_review.summary": {"$regex": normalized_keyword, "$options": "i"}},
            ]

        review_docs = await db["trade_reviews"].find(review_query).sort("created_at", -1).limit(limit).to_list(length=limit)
        review_results: List[Dict[str, Any]] = []
        from app.services.trade_review_service import TradeReviewService

        review_service = TradeReviewService()
        for doc in review_docs:
            review_id = (doc.get("review_id") or "").strip()
            if not review_id:
                continue
            review = await review_service.get_review_detail(user_id, review_id)
            if not review:
                continue
            ref = _normalize_report_ref_item(_build_review_report_ref_from_review(review))
            if ref:
                review_results.append(ref)
            if len(review_results) >= limit:
                break
        return review_results[:limit]

    from app.services.task_analysis_service import get_task_analysis_service

    service = get_task_analysis_service()
    results: List[Dict[str, Any]] = []
    seen_keys = set()

    if normalized_keyword:
        analysis_query: Dict[str, Any] = {
            "$and": [
                {"$or": [{"user_id": user_id}, {"user_id": str(user_id)}]},
                {
                    "$or": [
                        {"stock_symbol": {"$regex": normalized_keyword, "$options": "i"}},
                        {"stock_name": {"$regex": normalized_keyword, "$options": "i"}},
                        {"summary": {"$regex": normalized_keyword, "$options": "i"}},
                        {"analysis_id": {"$regex": normalized_keyword, "$options": "i"}},
                        {"task_id": {"$regex": normalized_keyword, "$options": "i"}},
                    ]
                },
            ]
        }
        analysis_docs = await db.analysis_reports.find(analysis_query).sort("created_at", -1).limit(limit * 3).to_list(length=limit * 3)
        for doc in analysis_docs:
            ref = _normalize_report_ref_item(_build_stock_report_ref_from_analysis_doc(doc))
            ref_key = (ref or {}).get("report_key") if ref else None
            if not ref_key or ref_key in seen_keys:
                continue
            if ref:
                results.append(ref)
                seen_keys.add(ref_key)
            if len(results) >= limit:
                return results[:limit]

    task_query: Dict[str, Any] = {
        "$and": [
            {"status": "completed"},
            {"$or": [{"user_id": user_id}, {"user_id": ObjectId(user_id)}]},
        ]
    }
    if normalized_keyword:
        task_query["$and"].append({
            "$or": [
                {"task_params.symbol": {"$regex": normalized_keyword, "$options": "i"}},
                {"result.summary": {"$regex": normalized_keyword, "$options": "i"}},
                {"result.recommendation": {"$regex": normalized_keyword, "$options": "i"}},
                {"task_id": {"$regex": normalized_keyword, "$options": "i"}},
            ]
        })

    task_docs = await db.unified_analysis_tasks.find(task_query).sort("created_at", -1).limit(limit * 2).to_list(length=limit * 2)
    for doc in task_docs:
        task_id = (doc.get("task_id") or "").strip()
        if not task_id or task_id in seen_keys:
            continue
        task = await service.get_task(task_id)
        if not task or str(task.user_id) != str(user_id) or str(task.status) != "completed":
            continue
        ref = _normalize_report_ref_item(_build_stock_report_ref_from_task(task))
        if ref:
            results.append(ref)
            seen_keys.add(task_id)
        if len(results) >= limit:
            break

    return results[:limit]


async def attach_existing_report_to_thread(
    db,
    user_id: str,
    thread_id: str,
    ref_type: str,
    report_key: str,
) -> Dict[str, Any]:
    normalized_type = (ref_type or "").strip()
    normalized_key = (report_key or "").strip()
    if not normalized_type or not normalized_key:
        raise ValueError("报告类型和报告标识不能为空")

    if normalized_type == "position_report":
        from app.services.portfolio_service import get_portfolio_service

        report = await get_portfolio_service().get_position_analysis_by_id(user_id, normalized_key)
        if not report:
            raise ValueError("未找到可关联的持仓报告")
        ref = _normalize_report_ref_item(_build_position_report_ref_from_report(report))
    elif normalized_type == "review_report":
        from app.services.trade_review_service import TradeReviewService

        service = TradeReviewService()
        review = await service.get_review_detail(user_id, normalized_key)
        if not review:
            raise ValueError("未找到可关联的交易复盘报告")
        ref = _normalize_report_ref_item(_build_review_report_ref_from_review(review))
    else:
        from app.services.task_analysis_service import get_task_analysis_service

        task = await get_task_analysis_service().get_task(normalized_key)
        if task and str(task.user_id) == str(user_id) and str(task.status) == "completed":
            ref = _normalize_report_ref_item(_build_stock_report_ref_from_task(task))
        else:
            report_doc = await db.analysis_reports.find_one(_build_analysis_report_lookup_query(user_id, normalized_key))
            if not report_doc:
                raise ValueError("未找到可关联的历史分析报告")
            ref = _normalize_report_ref_item(_build_stock_report_ref_from_analysis_doc(report_doc))

    if not ref:
        raise ValueError("报告信息不完整，无法关联")

    attached = await attach_report_ref_to_thread(db, user_id, thread_id, ref)
    if not attached:
        raise ValueError("当前主题不支持关联该报告")
    return ref


from app.services.assistant.threads import (
    _build_analysis_report_lookup_query,
    _build_position_report_ref_from_report,
    _build_review_report_ref_from_review,
    _build_stock_report_ref_from_analysis_doc,
    _build_stock_report_ref_from_task,
    _normalize_report_ref_item,
    attach_report_ref_to_thread,
)
