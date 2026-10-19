"""Register the optional local Codex provider without changing existing defaults."""
import os
import shutil

from app.models.config import LLMConfig


async def ensure_codex_config(db):
    if os.getenv("CODEX_LOCAL_ENABLED", "true").lower() in {"0", "false", "no"}:
        return
    if not shutil.which(os.getenv("CODEX_CLI_PATH", "codex")):
        return
    await db.llm_providers.update_one({"name": "codex"}, {"$setOnInsert": {
        "name": "codex", "display_name": "本机 Codex（ChatGPT 登录）",
        "description": "通过本机 Codex CLI 使用 Astra，消耗 ChatGPT/Codex 套餐额度。",
        "is_active": True, "api_key": None, "default_base_url": None,
        "test_model": "gpt-6-astra", "supported_features": ["chat", "tool_calling"],
        "extra_config": {"api_key_required": False, "source": "chatgpt_login"},
    }}, upsert=True)
    await db.model_catalog.update_one({"provider": "codex"}, {"$setOnInsert": {
        "provider": "codex", "provider_name": "本机 Codex",
        "models": [{"name": "gpt-6-astra", "display_name": "Astra（本机 Codex）",
                    "capabilities": ["function_calling", "reasoning"],
                    "description": "使用本机 ChatGPT 登录，无需 API Key"}],
    }}, upsert=True)
    doc = await db.system_configs.find_one({"is_active": True}, sort=[("version", -1)])
    if not doc:
        return
    config = LLMConfig(
        config_id="codex-gpt-6-astra", provider="codex", model_name="gpt-6-astra",
        model_display_name="Astra（本机 Codex）", timeout=600, retry_times=0,
        reasoning_effort="medium", max_tokens=8000, capability_level=5,
        suitable_roles=["both"], features=["tool_calling", "reasoning", "long_context"],
        recommended_depths=["快速", "基础", "标准", "深度", "全面"],
        description="直接参与分析；通过本机 Codex 使用 ChatGPT 套餐，输出预算为软目标。",
    ).model_dump()
    await db.system_configs.update_one({
        "_id": doc["_id"], "llm_configs": {"$not": {"$elemMatch": {
            "provider": "codex", "model_name": "gpt-6-astra",
        }}},
    }, {"$push": {"llm_configs": config}})
