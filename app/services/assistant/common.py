"""智能助手共享常量和依赖。"""
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
