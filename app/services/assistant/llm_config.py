"""智能助手：llm_config。"""
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



def _resolve_models(config_doc) -> tuple:
    """从配置文档解析默认模型选择，优先 config_id，其次兼容旧 model_name。"""
    return (
        _resolve_model_selection(config_doc, "quick_analysis_model"),
        _resolve_model_selection(config_doc, "deep_analysis_model"),
        _resolve_model_selection(config_doc, "deep_reasoning_model"),
        _resolve_model_selection(config_doc, "coding_model"),
    )


def _normalize_model_name(model_name: Optional[str]) -> Optional[str]:
    if not model_name:
        return None
    text = str(model_name).strip()
    return text or None


def _normalize_config_id(config_id: Optional[str]) -> Optional[str]:
    if not config_id:
        return None
    text = str(config_id).strip()
    return text or None


def _get_config_setting_value(config_doc: Dict[str, Any], key: str) -> Any:
    default_models = config_doc.get("default_models") or {}
    system_settings = config_doc.get("system_settings") or {}
    return default_models.get(key) or system_settings.get(key)


def _get_default_provider(config_doc: Dict[str, Any]) -> str:
    return str(_get_config_setting_value(config_doc, "default_provider") or "").strip().lower()


def _select_llm_config_from_doc(
    config_doc: Dict[str, Any],
    *,
    config_id: Optional[str] = None,
    model_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    llm_configs = [cfg for cfg in (config_doc.get("llm_configs") or []) if cfg.get("enabled", True)]
    normalized_config_id = _normalize_config_id(config_id)
    normalized_model_name = _normalize_model_name(model_name)

    if normalized_config_id:
        exact_config = next(
            (
                cfg
                for cfg in llm_configs
                if _normalize_config_id(cfg.get("config_id")) == normalized_config_id
            ),
            None,
        )
        if exact_config is not None:
            return exact_config

    if not normalized_model_name:
        return None

    model_configs = [cfg for cfg in llm_configs if _normalize_model_name(cfg.get("model_name")) == normalized_model_name]
    if not model_configs:
        return None

    preferred_provider = _get_default_provider(config_doc)
    if preferred_provider:
        preferred_config = next(
            (
                cfg
                for cfg in model_configs
                if str(cfg.get("provider") or "").strip().lower() == preferred_provider
            ),
            None,
        )
        if preferred_config is not None:
            return preferred_config

    return model_configs[0]


def _resolve_model_selection(config_doc: Dict[str, Any], model_key: str) -> Optional[Dict[str, Any]]:
    config_id = _normalize_config_id(_get_config_setting_value(config_doc, f"{model_key}_config_id"))
    model_name = _normalize_model_name(_get_config_setting_value(config_doc, model_key))
    matched_config = _select_llm_config_from_doc(
        config_doc,
        config_id=config_id,
        model_name=model_name,
    )
    if matched_config is not None:
        return {
            "config_id": _normalize_config_id(matched_config.get("config_id")),
            "model_name": _normalize_model_name(matched_config.get("model_name")),
            "provider": str(matched_config.get("provider") or "").strip().lower(),
        }
    if not config_id and not model_name:
        return None
    return {
        "config_id": config_id,
        "model_name": model_name,
        "provider": None,
    }


def _selection_model_name(selection: Optional[Dict[str, Any]]) -> Optional[str]:
    if not selection:
        return None
    return _normalize_model_name(selection.get("model_name"))


def _selection_config_id(selection: Optional[Dict[str, Any]]) -> Optional[str]:
    if not selection:
        return None
    return _normalize_config_id(selection.get("config_id"))


def _normalize_preferred_models(preferred_models: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not preferred_models:
        return {}

    normalized: Dict[str, str] = {}
    unified_model = _normalize_model_name(preferred_models.get("model"))
    unified_model_config_id = _normalize_config_id(preferred_models.get("model_config_id"))
    quick_model = _normalize_model_name(preferred_models.get("quick_model"))
    quick_model_config_id = _normalize_config_id(preferred_models.get("quick_model_config_id"))
    deep_model = _normalize_model_name(preferred_models.get("deep_model"))
    deep_model_config_id = _normalize_config_id(preferred_models.get("deep_model_config_id"))

    if unified_model:
        normalized["model"] = unified_model
    if unified_model_config_id:
        normalized["model_config_id"] = unified_model_config_id

    if quick_model:
        normalized["quick_model"] = quick_model
    if quick_model_config_id:
        normalized["quick_model_config_id"] = quick_model_config_id
    if deep_model:
        normalized["deep_model"] = deep_model
    if deep_model_config_id:
        normalized["deep_model_config_id"] = deep_model_config_id

    if unified_model and not quick_model:
        normalized["quick_model"] = unified_model
    if unified_model and not deep_model:
        normalized["deep_model"] = unified_model
    if unified_model_config_id and not quick_model_config_id:
        normalized["quick_model_config_id"] = unified_model_config_id
    if unified_model_config_id and not deep_model_config_id:
        normalized["deep_model_config_id"] = unified_model_config_id

    return normalized


def _get_preferred_assistant_model(preferred_models: Optional[Dict[str, Any]]) -> Optional[str]:
    normalized = _normalize_preferred_models(preferred_models)
    return (
        normalized.get("model")
        or normalized.get("deep_model")
        or normalized.get("quick_model")
    )


def _get_preferred_assistant_config_id(preferred_models: Optional[Dict[str, Any]]) -> Optional[str]:
    normalized = _normalize_preferred_models(preferred_models)
    return (
        normalized.get("model_config_id")
        or normalized.get("deep_model_config_id")
        or normalized.get("quick_model_config_id")
    )


def _build_assistant_model_preferences(
    model: Optional[str] = None,
    model_config_id: Optional[str] = None,
    quick_model: Optional[str] = None,
    quick_model_config_id: Optional[str] = None,
    deep_model: Optional[str] = None,
    deep_model_config_id: Optional[str] = None,
) -> Dict[str, str]:
    normalized = _normalize_preferred_models(
        {
            "model": model,
            "model_config_id": model_config_id,
            "quick_model": quick_model,
            "quick_model_config_id": quick_model_config_id,
            "deep_model": deep_model,
            "deep_model_config_id": deep_model_config_id,
        }
    )
    assistant_model = _get_preferred_assistant_model(normalized)
    assistant_model_config_id = _get_preferred_assistant_config_id(normalized)
    if not assistant_model and not assistant_model_config_id:
        return {}
    preferred: Dict[str, str] = {}
    if assistant_model:
        preferred["model"] = assistant_model
    if assistant_model_config_id:
        preferred["model_config_id"] = assistant_model_config_id
    return preferred


async def _get_llm_config_for_model(
    db,
    target_model: Optional[str],
    target_config_id: Optional[str] = None,
    purpose: str = "",
) -> Optional[LLMConfig]:
    """根据模型选择获取 LLM 配置，优先 config_id，兼容旧 model_name。"""
    if not target_model and not target_config_id:
        return None
    config_doc = await db.system_configs.find_one(
        {"is_active": True},
        sort=[("version", -1)]
    )
    return resolve_llm_config_from_config_doc(
        config_doc,
        target_model=target_model,
        target_config_id=target_config_id,
        purpose=purpose,
    )


def resolve_llm_config_from_config_doc(
    config_doc: Optional[Dict[str, Any]],
    *,
    target_model: Optional[str],
    target_config_id: Optional[str] = None,
    purpose: str = "",
) -> Optional[LLMConfig]:
    """基于 system_configs 文档解析统一 LLMConfig，供各子系统共享。"""
    if not config_doc or "llm_configs" not in config_doc:
        return None

    model_config = _select_llm_config_from_doc(
        config_doc,
        config_id=target_config_id,
        model_name=target_model,
    )
    if model_config is None:
        return None

    resolved_model_name = _normalize_model_name(model_config.get("model_name")) or _normalize_model_name(target_model)
    if not resolved_model_name:
        return None

    from app.services.llm_model_resolution import get_provider_and_url_by_model_sync

    provider_info = get_provider_and_url_by_model_sync(
        resolved_model_name,
        preferred_provider=str(model_config.get("provider") or _get_default_provider(config_doc) or "").strip().lower(),
        preferred_config_id=_normalize_config_id(model_config.get("config_id")) or target_config_id,
    )
    if not provider_info:
        return None

    provider_str = str(model_config.get("provider") or provider_info.get("provider") or "").lower()
    provider_map = {
        "codex": LLMProvider.CODEX,
        "openai": LLMProvider.OPENAI, "deepseek": LLMProvider.DEEPSEEK,
        "dashscope": LLMProvider.DASHSCOPE, "qwen": LLMProvider.DASHSCOPE,
        "zhipu": LLMProvider.ZHIPU, "google": LLMProvider.GOOGLE,
        "anthropic": LLMProvider.ANTHROPIC, "siliconflow": LLMProvider.SILICONFLOW,
        "kimi": LLMProvider.OPENAI, "moonshot": LLMProvider.OPENAI,
        "ollama": LLMProvider.OLLAMA, "openrouter": LLMProvider.OPENROUTER,
    }
    provider = provider_map.get(provider_str, LLMProvider.DASHSCOPE)
    logger.info(
        "[智能助手] 模型路由解析: provider=%s model=%s config_id=%s backend_provider=%s",
        provider_str or "unknown",
        resolved_model_name,
        _normalize_config_id(model_config.get("config_id")) or target_config_id or "",
        provider.value,
    )
    return LLMConfig(
        provider=provider, model=resolved_model_name,
        api_key=provider_info.get("api_key"), base_url=provider_info.get("backend_url"),
        temperature=float(model_config.get("temperature", 0.2)),
        max_tokens=int(model_config.get("max_tokens", 4000)),
        timeout=int(model_config.get("timeout", 60)),
        retry_times=int(model_config.get("retry_times", 3)),
        reasoning_effort=model_config.get("reasoning_effort"),
    )


def get_system_llm_config_from_config_doc(
    config_doc: Optional[Dict[str, Any]],
    *,
    model_keys: Optional[List[str]] = None,
) -> Optional[LLMConfig]:
    """按系统设置中的模型选择顺序解析统一 LLMConfig。"""
    if not config_doc or "llm_configs" not in config_doc:
        return None

    resolved_model_keys = model_keys or ["quick_analysis_model", "deep_analysis_model"]
    for model_key in resolved_model_keys:
        selection = _resolve_model_selection(config_doc, model_key)
        if not selection:
            continue

        llm_config = resolve_llm_config_from_config_doc(
            config_doc,
            target_model=_selection_model_name(selection),
            target_config_id=_selection_config_id(selection),
            purpose=model_key,
        )
        if llm_config is not None:
            return llm_config

    for llm_config_doc in config_doc.get("llm_configs") or []:
        if not llm_config_doc.get("enabled", True):
            continue
        llm_config = resolve_llm_config_from_config_doc(
            config_doc,
            target_model=_normalize_model_name(llm_config_doc.get("model_name")),
            target_config_id=_normalize_config_id(llm_config_doc.get("config_id")),
            purpose="first_enabled",
        )
        if llm_config is not None:
            return llm_config

    return None


async def _get_assistant_llm_config(
    db,
    use_reasoning_model: bool = False,
    preferred_models: Optional[Dict[str, Any]] = None,
) -> Optional[LLMConfig]:
    """
    从数据库获取智能助手使用的 LLM 配置
    优先使用深度思考模型（deep_analysis_model），以保证时间逻辑、财报披露等推理准确
    use_reasoning_model=True 时，优先使用深度推理模型（deep_reasoning_model），适用于策略生成、规划等
    若未配置则回退到快速模型或默认模型
    """
    config_doc = await db.system_configs.find_one(
        {"is_active": True},
        sort=[("version", -1)]
    )
    if not config_doc or "llm_configs" not in config_doc:
        logger.warning("[智能助手] 数据库中没有 LLM 配置")
        return None

    llm_configs = config_doc.get("llm_configs", [])
    default_llm = config_doc.get("default_llm")
    quick_selection, deep_selection, reasoning_selection, _ = _resolve_models(config_doc)
    quick_model = _selection_model_name(quick_selection)
    deep_model = _selection_model_name(deep_selection)
    reasoning_model = _selection_model_name(reasoning_selection)
    preferred_assistant_model = _get_preferred_assistant_model(preferred_models)
    preferred_assistant_config_id = _get_preferred_assistant_config_id(preferred_models)
    target_config_id: Optional[str] = None

    if use_reasoning_model and (preferred_assistant_model or preferred_assistant_config_id):
        target_model = preferred_assistant_model
        target_config_id = preferred_assistant_config_id
        logger.info(
            "[智能助手] 使用用户选择的助手模型: model=%s config_id=%s",
            target_model or "",
            target_config_id or "",
        )
    elif use_reasoning_model and reasoning_selection:
        target_model = _selection_model_name(reasoning_selection)
        target_config_id = _selection_config_id(reasoning_selection)
        logger.info(f"[智能助手] 使用深度推理模型: {target_model}")
    else:
        target_selection = deep_selection or quick_selection
        target_model = preferred_assistant_model or _selection_model_name(target_selection) or default_llm
        if preferred_assistant_config_id:
            target_config_id = preferred_assistant_config_id
        elif not preferred_assistant_model and target_selection is not None:
            target_config_id = _selection_config_id(target_selection)
        if preferred_assistant_model or preferred_assistant_config_id:
            logger.info(
                "[智能助手] 使用用户选择的助手模型: model=%s config_id=%s",
                preferred_assistant_model or "",
                preferred_assistant_config_id or "",
            )
        elif deep_selection:
            logger.info(f"[智能助手] 使用深度分析模型: {deep_model}")
        elif quick_selection:
            logger.info(f"[智能助手] 深度模型未配置，使用快速模型: {quick_model}")

    if not target_model:
        target_model = preferred_assistant_model or deep_model or quick_model or default_llm
    if not target_model and llm_configs:
        for cfg in llm_configs:
            if cfg.get("enabled", True):
                target_model = cfg.get("model_name")
                target_config_id = _normalize_config_id(cfg.get("config_id"))
                break

    if not target_model:
        logger.warning("[智能助手] 未找到可用的模型配置")
        return None

    return await _get_llm_config_for_model(db, target_model, target_config_id=target_config_id)


async def get_reasoning_llm_config(db=None):
    """
    获取深度推理模型配置，用于策略 DSL 生成、规划等需要强推理的场景
    优先 deep_reasoning_model，未配置时回退到 deep_analysis_model
    """
    from app.core.database import get_mongo_db
    db = db if db is not None else get_mongo_db()
    return await _get_assistant_llm_config(db, use_reasoning_model=True)


async def get_quick_llm_config(db=None):
    """
    获取快速模型配置，用于低延迟轻量判断场景
    （Skill 需求对话的清晰度评估、边界检测、多轮回复等）
    """
    from app.core.database import get_mongo_db
    db = db if db is not None else get_mongo_db()
    config_doc = await db.system_configs.find_one(
        {"is_active": True},
        sort=[("version", -1)]
    )
    if not config_doc or "llm_configs" not in config_doc:
        return None
    quick_selection = _resolve_model_selection(config_doc, "quick_analysis_model")
    model_name = _selection_model_name(quick_selection)
    if not model_name:
        return None
    return await _get_llm_config_for_model(
        db,
        model_name,
        target_config_id=_selection_config_id(quick_selection),
    )


async def get_coding_llm_config(db=None):
    """
    获取编程模型配置，用于策略 DSL 生成等需要结构化 JSON 输出的场景
    优先 coding_model，未配置时回退到 deep_reasoning_model → deep_analysis_model
    """
    from app.core.database import get_mongo_db
    db = db if db is not None else get_mongo_db()
    config_doc = await db.system_configs.find_one(
        {"is_active": True},
        sort=[("version", -1)]
    )
    if not config_doc or "llm_configs" not in config_doc:
        return await get_reasoning_llm_config(db)
    _, deep_selection, reasoning_selection, coding_selection = _resolve_models(config_doc)
    deep_model = _selection_model_name(deep_selection)
    reasoning_model = _selection_model_name(reasoning_selection)
    coding_model = _selection_model_name(coding_selection)
    target_selection = coding_selection or reasoning_selection or deep_selection
    target_model = _selection_model_name(target_selection)
    target_config_id = _selection_config_id(target_selection)
    if target_model:
        logger.info(f"[编程模型] 使用: {target_model} (coding={bool(coding_model)}, reasoning={bool(reasoning_model)})")
    return await _get_llm_config_for_model(db, target_model, target_config_id=target_config_id) if target_model else await get_reasoning_llm_config(db)
