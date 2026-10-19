"""Resolve an analysis model to its provider, endpoint, and parameters.

The v2 workflow and the other analysis entry points share this lookup so they
do not import the legacy analysis service.
"""

import logging
from typing import Optional

from app.services.config_service import ConfigService

logger = logging.getLogger("app.services.llm_model_resolution")
config_service = ConfigService()

# 研究深度档位 → 火山方舟推理模型思考预算（reasoning_effort 七档：none/minimal/low/medium/high/xhigh/max）
# 档位映射仅作为默认值：DB 模型配置中显式设置的 reasoning_effort 优先级更高（见 trading_graph 合并逻辑）
RESEARCH_DEPTH_REASONING_EFFORT = {
    "快速": "minimal",
    "基础": "low",
    "标准": "medium",
    "深度": "high",
    "全面": "xhigh",
}

async def get_provider_by_model_name(model_name: str, preferred_provider: Optional[str] = None) -> str:
    """
    根据模型名称从数据库配置中查找对应的供应商（异步版本）

    Args:
        model_name: 模型名称，如 'qwen-turbo', 'gpt-4' 等

    Returns:
        str: 供应商名称，如 'dashscope', 'openai' 等
    """
    try:
        # 从配置服务获取系统配置
        system_config = await config_service.get_system_config()
        if not system_config or not system_config.llm_configs:
            logger.warning(f"⚠️ 系统配置为空，使用默认供应商映射")
            return _get_default_provider_by_model(model_name)

        normalized_provider = str(preferred_provider or "").strip().lower()

        # 在LLM配置中查找匹配的模型
        for llm_config in system_config.llm_configs:
            if llm_config.model_name != model_name:
                continue
            provider = llm_config.provider.value if hasattr(llm_config.provider, 'value') else str(llm_config.provider)
            if normalized_provider and str(provider).lower() != normalized_provider:
                continue
            logger.info(f"✅ 从数据库找到模型 {model_name} 的供应商: {provider}")
            return provider

        # 如果数据库中没有找到，使用默认映射
        logger.warning(f"⚠️ 数据库中未找到模型 {model_name}，使用默认映射")
        return _get_default_provider_by_model(model_name)

    except Exception as e:
        logger.error(f"❌ 查找模型供应商失败: {e}")
        return _get_default_provider_by_model(model_name)


def get_provider_by_model_name_sync(model_name: str, preferred_provider: Optional[str] = None) -> str:
    """
    根据模型名称从数据库配置中查找对应的供应商（同步版本）

    Args:
        model_name: 模型名称，如 'qwen-turbo', 'gpt-4' 等

    Returns:
        str: 供应商名称，如 'dashscope', 'openai' 等
    """
    provider_info = get_provider_and_url_by_model_sync(model_name, preferred_provider=preferred_provider)
    return provider_info["provider"]


def get_provider_and_url_by_model_sync(
    model_name: str,
    preferred_provider: Optional[str] = None,
    preferred_config_id: Optional[str] = None,
) -> dict:
    """
    根据模型名称从数据库配置中查找对应的供应商和 API URL（同步版本）

    Args:
        model_name: 模型名称，如 'qwen-turbo', 'gpt-4' 等

    Returns:
        dict: {"provider": "google", "backend_url": "https://...", "api_key": "xxx"}
    """
    try:
        # 使用同步 MongoDB 客户端直接查询
        from pymongo import MongoClient
        from app.core.config import settings
        import os

        client = MongoClient(settings.MONGO_URI)
        db = client[settings.MONGO_DB]

        # 查询最新的活跃配置
        configs_collection = db.system_configs
        doc = configs_collection.find_one({"is_active": True}, sort=[("version", -1)])

        # 🔧 解析旧格式 "provider::model_name"（如 jdyun::GLM-5）
        if "::" in model_name:
            parsed_provider, parsed_model = model_name.split("::", 1)
            if not preferred_provider:
                preferred_provider = parsed_provider
            model_name = parsed_model
            logger.info(f"🔧 [同步查询] 解析旧格式模型名: {parsed_provider}::{parsed_model} → provider={preferred_provider}, model={model_name}")

        # 🔧 京东云模式：直接使用环境变量配置，跳过数据库查询
        from app.core.jdyun import (
            is_jdyun_mode, get_jdyun_api_base, get_jdyun_api_key,
            get_jdyun_default_model, JDYUN_PROVIDER_NAME,
        )
        if is_jdyun_mode():
            api_base = get_jdyun_api_base()
            api_key = get_jdyun_api_key()
            if api_base and api_key:
                effective_model = model_name if model_name else get_jdyun_default_model()
                logger.info(f"🔧 [京东云模式] 使用京东云配置: model={effective_model}, api_base={api_base}")
                return {
                    "provider": JDYUN_PROVIDER_NAME,
                    "backend_url": api_base,
                    "api_key": api_key,
                }

        normalized_provider = str(preferred_provider or "").strip().lower()
        normalized_config_id = str(preferred_config_id or "").strip()

        def _build_provider_result(config_dict: dict) -> dict:
            provider = str(config_dict.get("provider") or "").strip().lower()
            if provider == "codex":
                return {"provider": "codex", "backend_url": "", "api_key": None}
            api_base = config_dict.get("api_base")
            model_api_key = config_dict.get("api_key")

            providers_collection = db.llm_providers
            provider_doc = providers_collection.find_one({"name": provider})

            from app.utils.api_key_utils import is_valid_api_key

            api_key = None
            api_key_source = None
            logger.info(f"🔍 [同步查询] 模型 {config_dict.get('model_name')} API Key 获取流程:")

            if provider_doc and provider_doc.get("api_key"):
                provider_api_key = provider_doc["api_key"]
                logger.info(f"   - 厂家配置中的 api_key: {'有值' if provider_api_key else '空'}")
                if is_valid_api_key(provider_api_key):
                    api_key = provider_api_key.strip()
                    api_key_source = "厂家配置"
                    logger.info("✅ [同步查询] ✅ 使用厂家配置的 API Key（优先级1）")
                else:
                    logger.warning("⚠️ [同步查询] 厂家配置的 API Key 无效（可能是占位符或空值）")
            else:
                logger.info(f"   - 厂家配置: {'存在但无API Key' if provider_doc else '不存在'}")

            if not api_key:
                logger.info(f"   - 模型配置中的 api_key: {'有值' if model_api_key else '空'}")
                if is_valid_api_key(model_api_key):
                    api_key = model_api_key.strip()
                    api_key_source = "模型配置"
                    logger.info("✅ [同步查询] ✅ 使用模型配置的 API Key（优先级2，备选）")

            if not api_key:
                env_api_key = _get_env_api_key_for_provider(provider)
                logger.info(f"   - 环境变量中的 api_key: {'有值' if env_api_key else '空'}")
                if env_api_key:
                    api_key = env_api_key
                    api_key_source = "环境变量"
                    logger.info("✅ [同步查询] ✅ 使用环境变量的 API Key（优先级3）")
                else:
                    logger.warning(f"⚠️ [同步查询] ❌ 未找到 {provider} 的 API Key（厂家配置、模型配置、环境变量都没有）")

            if api_key_source:
                logger.info(f"✅ [同步查询] 最终使用的 API Key 来源: {api_key_source}")

            if api_base:
                backend_url = api_base
                logger.info(f"✅ [同步查询] 模型 {config_dict.get('model_name')} 使用自定义 API: {api_base}")
            elif provider_doc and provider_doc.get("default_base_url"):
                backend_url = provider_doc["default_base_url"]
                logger.info(f"✅ [同步查询] 模型 {config_dict.get('model_name')} 使用厂家默认 API: {backend_url}")
            else:
                backend_url = _get_default_backend_url(provider)
                logger.warning(f"⚠️ [同步查询] 厂家 {provider} 没有配置 default_base_url，使用硬编码默认值")

            return {
                "provider": provider,
                "backend_url": backend_url,
                "api_key": api_key,
            }

        if doc and "llm_configs" in doc:
            llm_configs = doc["llm_configs"]
            fallback_config = None

            if normalized_config_id:
                exact_config = next(
                    (
                        config_dict
                        for config_dict in llm_configs
                        if str(config_dict.get("config_id") or "").strip() == normalized_config_id
                        and config_dict.get("enabled", True)
                    ),
                    None,
                )
                if exact_config is not None:
                    result = _build_provider_result(exact_config)
                    client.close()
                    return result

            for config_dict in llm_configs:
                if config_dict.get("model_name") != model_name:
                    continue

                provider = str(config_dict.get("provider") or "").strip().lower()
                if fallback_config is None:
                    fallback_config = config_dict
                if normalized_provider and provider != normalized_provider:
                    continue
                result = _build_provider_result(config_dict)
                client.close()
                return result

            if fallback_config is not None:
                logger.warning(f"⚠️ [同步查询] 模型 {model_name} 未找到 provider={normalized_provider or '-'} 的精确配置，回退到同名模型的首个配置")
                result = _build_provider_result(fallback_config)
                client.close()
                return result

        client.close()

        # 如果数据库中没有找到模型配置，使用默认映射
        logger.warning(f"⚠️ [同步查询] 数据库中未找到模型 {model_name}，使用默认映射")
        provider = _get_default_provider_by_model(model_name)
        if provider == "codex":
            return {"provider": "codex", "backend_url": "", "api_key": None}

        # 尝试从厂家配置中获取 default_base_url 和 API Key
        try:
            client = MongoClient(settings.MONGO_URI)
            db = client[settings.MONGO_DB]
            providers_collection = db.llm_providers
            provider_doc = providers_collection.find_one({"name": provider})

            backend_url = _get_default_backend_url(provider)
            api_key = None

            # 🔥 导入 API Key 验证函数
            from app.utils.api_key_utils import is_valid_api_key
            
            if provider_doc:
                if provider_doc.get("default_base_url"):
                    backend_url = provider_doc["default_base_url"]
                    logger.info(f"✅ [同步查询] 使用厂家 {provider} 的 default_base_url: {backend_url}")

                if provider_doc.get("api_key"):
                    provider_api_key = provider_doc["api_key"]
                    # 🔥 使用 is_valid_api_key 验证厂家配置的 API Key
                    if is_valid_api_key(provider_api_key):
                        api_key = provider_api_key.strip()
                        logger.info(f"✅ [同步查询] 使用厂家 {provider} 的 API Key")

            # 如果厂家配置中没有 API Key，尝试从环境变量获取
            if not api_key:
                api_key = _get_env_api_key_for_provider(provider)
                if api_key:
                    logger.info(f"✅ [同步查询] 使用环境变量的 API Key")

            client.close()
            return {
                "provider": provider,
                "backend_url": backend_url,
                "api_key": api_key
            }
        except Exception as e:
            logger.warning(f"⚠️ [同步查询] 无法查询厂家配置: {e}")

        # 最后回退到硬编码的默认 URL 和环境变量 API Key
        return {
            "provider": provider,
            "backend_url": _get_default_backend_url(provider),
            "api_key": _get_env_api_key_for_provider(provider)
        }

    except Exception as e:
        logger.error(f"❌ [同步查询] 查找模型供应商失败: {e}")
        provider = _get_default_provider_by_model(model_name)
        if provider == "codex":
            return {"provider": "codex", "backend_url": "", "api_key": None}

        # 尝试从厂家配置中获取 default_base_url 和 API Key
        try:
            from pymongo import MongoClient
            from app.core.config import settings

            client = MongoClient(settings.MONGO_URI)
            db = client[settings.MONGO_DB]
            providers_collection = db.llm_providers
            provider_doc = providers_collection.find_one({"name": provider})

            backend_url = _get_default_backend_url(provider)
            api_key = None

            # 🔥 导入 API Key 验证函数
            from app.utils.api_key_utils import is_valid_api_key
            
            if provider_doc:
                if provider_doc.get("default_base_url"):
                    backend_url = provider_doc["default_base_url"]
                    logger.info(f"✅ [同步查询] 使用厂家 {provider} 的 default_base_url: {backend_url}")

                if provider_doc.get("api_key"):
                    provider_api_key = provider_doc["api_key"]
                    # 🔥 使用 is_valid_api_key 验证厂家配置的 API Key
                    if is_valid_api_key(provider_api_key):
                        api_key = provider_api_key.strip()
                        logger.info(f"✅ [同步查询] 使用厂家 {provider} 的 API Key")

            # 如果厂家配置中没有 API Key，尝试从环境变量获取
            if not api_key:
                api_key = _get_env_api_key_for_provider(provider)

            client.close()
            return {
                "provider": provider,
                "backend_url": backend_url,
                "api_key": api_key
            }
        except Exception as e2:
            logger.warning(f"⚠️ [同步查询] 无法查询厂家配置: {e2}")

        # 最后回退到硬编码的默认 URL 和环境变量 API Key
        return {
            "provider": provider,
            "backend_url": _get_default_backend_url(provider),
            "api_key": _get_env_api_key_for_provider(provider)
        }


def _get_env_api_key_for_provider(provider: str) -> str:
    """
    从环境变量获取指定供应商的 API Key

    Args:
        provider: 供应商名称，如 'google', 'dashscope' 等

    Returns:
        str: API Key，如果未找到则返回 None
    """
    import os

    env_key_map = {
        "google": "GOOGLE_API_KEY",
        "dashscope": "DASHSCOPE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "siliconflow": "SILICONFLOW_API_KEY",
        "qianfan": "QIANFAN_API_KEY",
        "302ai": "AI302_API_KEY",
    }

    env_key_name = env_key_map.get(provider.lower())
    if env_key_name:
        api_key = os.getenv(env_key_name)
        # 🔥 使用 is_valid_api_key 验证环境变量中的 API Key
        from app.utils.api_key_utils import is_valid_api_key
        if is_valid_api_key(api_key):
            return api_key.strip()

    return None


def _get_default_backend_url(provider: str) -> str:
    """
    根据供应商名称返回默认的 backend_url

    Args:
        provider: 供应商名称，如 'google', 'dashscope' 等

    Returns:
        str: 默认的 backend_url
    """
    default_urls = {
        "codex": "",
        "google": "https://generativelanguage.googleapis.com/v1beta",
        "dashscope": "https://dashscope.aliyuncs.com/api/v1",
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com",
        "anthropic": "https://api.anthropic.com",
        "openrouter": "https://openrouter.ai/api/v1",
        "qianfan": "https://qianfan.baidubce.com/v2",
        "302ai": "https://api.302.ai/v1",
    }

    url = default_urls.get(provider, "https://dashscope.aliyuncs.com/compatible-mode/v1")
    logger.info(f"🔧 [默认URL] {provider} -> {url}")
    return url


def _get_default_provider_by_model(model_name: str) -> str:
    """
    根据模型名称返回默认的供应商映射
    这是一个后备方案，当数据库查询失败时使用
    """
    if model_name == "gpt-6-astra" or model_name.startswith("codex::"):
        return "codex"
    # 模型名称到供应商的默认映射
    # ⚠️ 注意：这只是后备方案，优先应该从数据库配置中获取
    model_provider_map = {
        # 阿里百炼 (DashScope)
        'qwen-turbo': 'dashscope',
        'qwen-plus': 'dashscope',
        'qwen-max': 'dashscope',
        'qwen-plus-latest': 'dashscope',
        'qwen-max-longcontext': 'dashscope',

        # OpenAI
        'gpt-3.5-turbo': 'openai',
        'gpt-4': 'openai',
        'gpt-4-turbo': 'openai',
        'gpt-4o': 'openai',
        'gpt-4o-mini': 'openai',

        # Google
        'gemini-pro': 'google',
        'gemini-2.0-flash': 'google',
        'gemini-2.0-flash-thinking-exp': 'google',

        # DeepSeek
        'deepseek-chat': 'deepseek',
        'deepseek-coder': 'deepseek',

        # 智谱AI
        'glm-4': 'zhipu',
        'glm-3-turbo': 'zhipu',
        'chatglm3-6b': 'zhipu'
    }

    provider = model_provider_map.get(model_name, 'dashscope')  # 默认使用阿里百炼
    logger.info(f"🔧 使用默认映射: {model_name} -> {provider}")
    return provider


def _load_model_config_from_db(model_name: str) -> dict:
    """
    从数据库读取模型配置参数（max_tokens, temperature, timeout, reasoning_effort 等）

    Args:
        model_name: 模型名称

    Returns:
        dict: 模型配置字典，如 {"max_tokens": 4000, "temperature": 0.7, "reasoning_effort": "low"}
    """
    preferred_provider = None
    if "::" in model_name:
        preferred_provider, model_name = model_name.split("::", 1)
    try:
        from pymongo import MongoClient
        from app.core.config import settings

        client = MongoClient(settings.MONGO_URI)
        db = client[settings.MONGO_DB]
        configs_collection = db.system_configs
        doc = configs_collection.find_one({"is_active": True}, sort=[("version", -1)])

        if doc and "llm_configs" in doc:
            for config_dict in doc["llm_configs"]:
                if (config_dict.get("model_name") == model_name
                        and (not preferred_provider or config_dict.get("provider") == preferred_provider)):
                    result = {
                        "max_tokens": config_dict.get("max_tokens", 4000),
                        "temperature": config_dict.get("temperature", 0.7),
                        "timeout": config_dict.get("timeout", 180),
                        "retry_times": config_dict.get("retry_times", 3),
                        "api_base": config_dict.get("api_base"),
                    }
                    # 🔧 火山方舟推理模型支持 reasoning_effort
                    if config_dict.get("reasoning_effort"):
                        result["reasoning_effort"] = config_dict["reasoning_effort"]
                        logger.info(f"🔧 [模型配置] {model_name} reasoning_effort={result['reasoning_effort']}")
                    else:
                        logger.info(f"🔧 [模型配置] {model_name} reasoning_effort 未设置")
                    client.close()
                    return result

        client.close()
        return {}
    except Exception as e:
        logger.warning(f"⚠️ 读取模型配置失败: {e}")
        return {}

