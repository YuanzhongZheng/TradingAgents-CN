"""智能助手：chat。"""
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



async def chat_with_assistant(
    db,
    user_message: str,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    current_thread_id: Optional[str] = None,
    is_im: bool = False,
    model: Optional[str] = None,
    model_config_id: Optional[str] = None,
    quick_model: Optional[str] = None,
    quick_model_config_id: Optional[str] = None,
    deep_model: Optional[str] = None,
    deep_model_config_id: Optional[str] = None,
    assistant_role: str = "general",
    user_context: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    便捷函数：使用智能助手进行对话
    user_id: 用于持久化会话；若提供则对话会后保存
    is_im: 是否来自 IM 渠道（QQ Bot 等），为 True 时注入移动端格式规范
    assistant_role: 助手角色 general=通用分析助理 / usage_helper=使用问答机器人
    user_context: 前端上下文（仅 usage_helper 模式使用）
    """
    from app.services.assistant.common import public_assistant_binding

    _load_assistant_settings = public_assistant_binding("_load_assistant_settings")
    load_user_profile = public_assistant_binding("load_user_profile")
    get_assistant_conversation = public_assistant_binding("get_assistant_conversation")
    _recall_memory_block = public_assistant_binding("_recall_memory_block")
    IntelligentAssistantService = public_assistant_binding("IntelligentAssistantService")
    save_assistant_message = public_assistant_binding("save_assistant_message")
    get_or_create_assistant_thread = public_assistant_binding("get_or_create_assistant_thread")

    # 设置用户上下文，供 assistant_ops 工具获取当前用户
    if user_id:
        set_current_user_id(user_id)
    if current_thread_id:
        set_current_assistant_thread_id(current_thread_id)
    elif conversation_id:
        set_current_assistant_thread_id(conversation_id)

    # 加载用户助理个性化设置
    assistant_settings = None
    if user_id:
        assistant_settings = await _load_assistant_settings(db, user_id)

    # 加载用户画像（跨会话持久记忆）
    user_profile: Optional[dict] = None
    current_topic_title: Optional[str] = None
    if user_id:
        try:
            user_profile = await load_user_profile(db, user_id)
            if user_profile and user_profile.get("recently_analyzed"):
                logger.info(f"[智能助手] ✅ 用户画像加载成功，最近分析 {len(user_profile['recently_analyzed'])} 条")
        except Exception as e:
            logger.warning(f"[智能助手] 加载用户画像失败（已跳过）: {e}")

    if user_id and current_thread_id:
        try:
            current_topic = await db.assistant_threads.find_one(
                {
                    "user_id": user_id,
                    "thread_id": current_thread_id,
                    "archived": {"$ne": True},
                },
                {"title": 1},
            )
            if current_topic:
                current_topic_title = (current_topic.get("title") or "").strip() or None
        except Exception as e:
            logger.warning(f"[智能助手] 加载当前主题失败（已跳过）: {e}")

    # 加载最近会话历史（最多 10 条，注入到 LLM 上下文）
    history_messages: Optional[List[Message]] = None
    if user_id:
        try:
            raw_history = await get_assistant_conversation(db, user_id, conversation_id)
            if raw_history:
                recent = raw_history[-ASSISTANT_MAX_CONTEXT_MESSAGES:]
                history_messages = []
                for m in recent:
                    role = MessageRole.USER if m.get("role") == "user" else MessageRole.ASSISTANT
                    content = (m.get("content") or "").strip()
                    if content:
                        history_messages.append(Message(role=role, content=content[:800]))
                if not history_messages:
                    history_messages = None
        except Exception as e:
            logger.warning(f"[智能助手] 加载历史会话失败（已跳过）: {e}")

    # mem0 记忆召回（降级安全）
    memory_block = ""
    if user_id:
        memory_block = await _recall_memory_block(db, user_id, user_message)

    service = IntelligentAssistantService(
        db,
        preferred_models=_build_assistant_model_preferences(
            model=model,
            model_config_id=model_config_id,
            quick_model=quick_model,
            quick_model_config_id=quick_model_config_id,
            deep_model=deep_model,
            deep_model_config_id=deep_model_config_id,
        ),
        user_id=user_id or "",
        assistant_role=assistant_role,
        user_context=user_context,
    )
    result = await service.chat(
        user_message,
        conversation_id,
        assistant_settings,
        is_im=is_im,
        history_messages=history_messages,
        user_profile=user_profile,
        current_topic_title=current_topic_title,
        memory_block=memory_block,
    )
    if user_id and user_message.strip():
        # 回复为空（异常/超时等）也保存：用户提问不能因失败而丢失
        assistant_content = (result.get("reply") or "").strip() or "⚠️ 本轮回复生成失败（未收到任何内容）"
        try:
            await save_assistant_message(
                db,
                user_id=user_id,
                user_content=user_message.strip(),
                assistant_content=assistant_content,
                tools_used=result.get("tools_used") or [],
                conversation_id=conversation_id,
            )
        except Exception as e:
            logger.warning("[智能助手] 保存会话失败: %s", e)

        # 注：mem0 conversation scope 已废弃，对话不再自动写入长期记忆。
        # 用户偏好通过 `remember_this` 元工具显式登记，详见 mem0 §15.5 P0-1。
    if user_id:
        thread = await get_or_create_assistant_thread(db, user_id, conversation_id)
        result["conversation_id"] = thread["thread_id"]
    return result


async def stream_chat_with_assistant(
    db,
    user_message: str,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    current_thread_id: Optional[str] = None,
    model: Optional[str] = None,
    model_config_id: Optional[str] = None,
    quick_model: Optional[str] = None,
    quick_model_config_id: Optional[str] = None,
    deep_model: Optional[str] = None,
    deep_model_config_id: Optional[str] = None,
    assistant_role: str = "general",
    user_context: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """
    流式便捷函数：使用智能助手进行流式对话，返回 SSE 事件字符串的 async generator。
    assistant_role: 助手角色 general=通用分析助理 / usage_helper=使用问答机器人
    user_context: 前端上下文（仅 usage_helper 模式使用）
    """
    from app.services.assistant.common import public_assistant_binding

    _load_assistant_settings = public_assistant_binding("_load_assistant_settings")
    load_user_profile = public_assistant_binding("load_user_profile")
    get_assistant_conversation = public_assistant_binding("get_assistant_conversation")
    _recall_memory_block = public_assistant_binding("_recall_memory_block")
    IntelligentAssistantService = public_assistant_binding("IntelligentAssistantService")
    save_assistant_message = public_assistant_binding("save_assistant_message")
    get_or_create_assistant_thread = public_assistant_binding("get_or_create_assistant_thread")

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    if user_id:
        set_current_user_id(user_id)
    if current_thread_id:
        set_current_assistant_thread_id(current_thread_id)
    elif conversation_id:
        set_current_assistant_thread_id(conversation_id)

    assistant_settings = None
    if user_id:
        assistant_settings = await _load_assistant_settings(db, user_id)

    user_profile: Optional[dict] = None
    current_topic_title: Optional[str] = None
    if user_id:
        try:
            user_profile = await load_user_profile(db, user_id)
        except Exception:
            pass

    if user_id and (current_thread_id or conversation_id):
        tid = current_thread_id or conversation_id
        try:
            current_topic = await db.assistant_threads.find_one(
                {"user_id": user_id, "thread_id": tid, "archived": {"$ne": True}},
                {"title": 1},
            )
            if current_topic:
                current_topic_title = (current_topic.get("title") or "").strip() or None
        except Exception:
            pass

    history_messages: Optional[List[Message]] = None
    if user_id:
        try:
            raw_history = await get_assistant_conversation(db, user_id, conversation_id)
            if raw_history:
                recent = raw_history[-ASSISTANT_MAX_CONTEXT_MESSAGES:]
                history_messages = []
                for m in recent:
                    role = MessageRole.USER if m.get("role") == "user" else MessageRole.ASSISTANT
                    content = (m.get("content") or "").strip()
                    if content:
                        history_messages.append(Message(role=role, content=content[:800]))
                if not history_messages:
                    history_messages = None
        except Exception:
            pass

    # mem0 记忆召回
    memory_block = ""
    if user_id:
        memory_block = await _recall_memory_block(db, user_id, user_message)

    logger.info("[智能助手·流式] mem0 召回完成，memory_block 长度=%d，准备创建 service", len(memory_block))

    service = IntelligentAssistantService(
        db,
        preferred_models=_build_assistant_model_preferences(
            model=model,
            model_config_id=model_config_id,
            quick_model=quick_model,
            quick_model_config_id=quick_model_config_id,
            deep_model=deep_model,
            deep_model_config_id=deep_model_config_id,
        ),
        user_id=user_id or "",
        assistant_role=assistant_role,
        user_context=user_context,
    )

    collected_reply = ""
    collected_tools: List[str] = []
    collected_data_refs: List[Dict[str, Any]] = []
    error_note = ""  # 本轮错误说明（内层超时/异常/取消，或外层生成器异常），随会话落库

    logger.info("[智能助手·流式] 准备调用 service.chat_stream")

    try:
        async for event_str in service.chat_stream(
            user_message,
            conversation_id,
            assistant_settings,
            history_messages=history_messages,
            user_profile=user_profile,
            current_topic_title=current_topic_title,
            memory_block=memory_block,
        ):
            if event_str.startswith("event: done\n"):
                try:
                    data_line = event_str.split("\ndata: ", 1)[1].rstrip("\n")
                    data = json.loads(data_line)
                    collected_tools = data.get("tools_used", [])
                    collected_data_refs = data.get("data_refs", [])
                except Exception:
                    pass
                continue
            if event_str.startswith("event: token\n"):
                try:
                    data_line = event_str.split("\ndata: ", 1)[1].rstrip("\n")
                    data = json.loads(data_line)
                    collected_reply += data.get("content", "")
                except Exception:
                    pass
            elif event_str.startswith("event: error\n"):
                # 记录内层错误（超时/异常/取消），随会话落库便于事后排查
                try:
                    data_line = event_str.split("\ndata: ", 1)[1].rstrip("\n")
                    data = json.loads(data_line)
                    if data.get("message"):
                        error_note = str(data["message"])
                except Exception:
                    pass
            yield event_str
    except Exception as e:
        logger.error("[智能助手·流式] chat_stream 生成器抛出异常: %s", e, exc_info=True)
        error_note = f"处理过程发生异常：{e}"
        # 发 error 事件让前端明确感知失败（而不是把异常伪装成回复文本）
        yield _sse("error", {"message": error_note})
    finally:
        # 会话持久化兜底：无论正常结束、内部错误还是客户端中途断开，
        # 用户提问必须落库，已生成的部分回复与错误说明也要落库。
        # 历史教训：原先只在「回复完整收集」时才保存，超时/中断后刷新
        # 页面用户提问直接消失——既丢上下文，也无从排查当时出了什么错。
        if user_id and user_message.strip():
            assistant_content = collected_reply.strip()
            if error_note:
                assistant_content = (
                    f"{assistant_content}\n\n---\n⚠️ {error_note}"
                    if assistant_content
                    else f"⚠️ {error_note}"
                )
            if not assistant_content:
                assistant_content = "⚠️ 本轮回复生成失败（未收到任何内容）"
            try:
                await save_assistant_message(
                    db,
                    user_id=user_id,
                    user_content=user_message.strip(),
                    assistant_content=assistant_content,
                    tools_used=collected_tools,
                    conversation_id=conversation_id,
                )
            except Exception as e:
                logger.warning("[智能助手·流式] 保存会话失败: %s", e)

        # 注：mem0 conversation scope 已废弃，对话不再自动写入长期记忆。
        # 用户偏好通过 `remember_this` 元工具显式登记，详见 mem0 §15.5 P0-1。

    conv_id = conversation_id
    if user_id:
        thread = await get_or_create_assistant_thread(db, user_id, conversation_id)
        conv_id = thread["thread_id"]

    yield _sse("done", {
        "tools_used": collected_tools,
        "data_refs": collected_data_refs,
        "conversation_id": conv_id,
        **({"error": True} if error_note else {}),
    })


from app.services.assistant.llm_config import (
    _build_assistant_model_preferences,
)
from app.services.assistant.service import (
    IntelligentAssistantService,
)
from app.services.assistant.thread_summary import (
    _load_assistant_settings,
    _recall_memory_block,
    get_assistant_conversation,
    get_or_create_assistant_thread,
    load_user_profile,
    save_assistant_message,
)
