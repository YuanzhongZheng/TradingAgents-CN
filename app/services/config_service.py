"""
配置管理服务
"""

import os
import time
import asyncio
import logging
import uuid
from typing import List, Optional, Dict, Any
from datetime import datetime
from app.utils.timezone import now_tz
from bson import ObjectId

from app.core.database import get_mongo_db
from app.core.unified_config import unified_config
from app.models.config import (
    SystemConfig, LLMConfig, DataSourceConfig, DatabaseConfig,
    ModelProvider, DataSourceType, DatabaseType, LLMProvider,
    MarketCategory, DataSourceGrouping, ModelCatalog, ModelInfo
)

logger = logging.getLogger("app.services.config_service")



from app.services.config.market import MarketConfigMixin
from app.services.config.system import SystemConfigMixin
from app.services.config.llm_testing import LlmTestingMixin
from app.services.config.datasource_testing import DataSourceTestingMixin
from app.services.config.database import DatabaseConfigMixin
from app.services.config.model_catalog import ModelCatalogMixin
from app.services.config.providers import ProviderConfigMixin
from app.services.config.provider_api import ProviderApiMixin

class ConfigService(MarketConfigMixin, SystemConfigMixin, LlmTestingMixin, DataSourceTestingMixin, DatabaseConfigMixin, ModelCatalogMixin, ProviderConfigMixin, ProviderApiMixin):

    def __init__(self, db_manager=None):
        self.db = None
        self.db_manager = db_manager

    async def _get_db(self):
        """获取数据库连接"""
        if self.db is None:
            if self.db_manager and self.db_manager.mongo_db is not None:
                # 如果有DatabaseManager实例，直接使用
                self.db = self.db_manager.mongo_db
            else:
                # 否则使用全局函数
                self.db = get_mongo_db()
        return self.db

    @staticmethod
    def _read_llm_config_field(llm_config: Any, field_name: str) -> Any:
        if isinstance(llm_config, dict):
            return llm_config.get(field_name)
        return getattr(llm_config, field_name, None)

    @staticmethod
    def _write_llm_config_field(llm_config: Any, field_name: str, value: Any) -> None:
        if isinstance(llm_config, dict):
            llm_config[field_name] = value
            return
        setattr(llm_config, field_name, value)

    def _build_deterministic_llm_config_id(self, llm_config: Any, index: int) -> str:
        provider = str(self._read_llm_config_field(llm_config, "provider") or "").strip().lower()
        model_name = str(self._read_llm_config_field(llm_config, "model_name") or "").strip()
        api_base = str(self._read_llm_config_field(llm_config, "api_base") or "").strip()
        description = str(self._read_llm_config_field(llm_config, "description") or "").strip()
        seed = f"tradingagents-llm-config|{provider}|{model_name}|{api_base}|{description}|{index}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

    def _ensure_llm_config_ids(self, llm_configs: List[Any]) -> bool:
        changed = False
        seen_ids = set()

        for index, llm_config in enumerate(llm_configs):
            raw_config_id = self._read_llm_config_field(llm_config, "config_id")
            config_id = str(raw_config_id or "").strip()

            if not config_id or config_id in seen_ids:
                candidate = self._build_deterministic_llm_config_id(llm_config, index)
                suffix = 1
                while candidate in seen_ids:
                    suffix += 1
                    candidate = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{candidate}|{suffix}"))
                self._write_llm_config_field(llm_config, "config_id", candidate)
                config_id = candidate
                changed = True

            seen_ids.add(config_id)

        return changed

    def _select_llm_config(
        self,
        llm_configs: List[Any],
        *,
        config_id: Optional[str] = None,
        model_name: Optional[str] = None,
        preferred_provider: Optional[str] = None,
    ) -> Optional[Any]:
        normalized_config_id = str(config_id or "").strip()
        normalized_model_name = str(model_name or "").strip()
        normalized_provider = str(preferred_provider or "").strip().lower()

        if normalized_config_id:
            for llm_config in llm_configs:
                existing_config_id = str(self._read_llm_config_field(llm_config, "config_id") or "").strip()
                if existing_config_id == normalized_config_id:
                    return llm_config

        if not normalized_model_name:
            return None

        candidates = [
            llm_config
            for llm_config in llm_configs
            if str(self._read_llm_config_field(llm_config, "model_name") or "").strip() == normalized_model_name
        ]
        if not candidates:
            return None

        if normalized_provider:
            for llm_config in candidates:
                provider = str(self._read_llm_config_field(llm_config, "provider") or "").strip().lower()
                if provider == normalized_provider:
                    return llm_config

        return candidates[0]

    def _normalize_default_model_settings(
        self,
        settings: Dict[str, Any],
        existing_settings: Dict[str, Any],
        llm_configs: List[Any],
    ) -> Dict[str, Any]:
        normalized_settings = dict(settings)
        preferred_provider = str(
            normalized_settings.get("default_provider")
            or existing_settings.get("default_provider")
            or ""
        ).strip().lower()
        field_pairs = [
            ("quick_analysis_model", "quick_analysis_model_config_id"),
            ("deep_analysis_model", "deep_analysis_model_config_id"),
            ("deep_reasoning_model", "deep_reasoning_model_config_id"),
            ("coding_model", "coding_model_config_id"),
        ]

        for model_key, config_id_key in field_pairs:
            raw_config_id = normalized_settings.get(config_id_key)
            raw_model_name = normalized_settings.get(model_key)
            config_id_present = config_id_key in normalized_settings
            model_name_present = model_key in normalized_settings
            config_id = str(raw_config_id or "").strip()
            model_name = str(raw_model_name or "").strip()

            if config_id_present and not config_id:
                normalized_settings[config_id_key] = ""
                normalized_settings[model_key] = ""
                continue

            if model_name_present and not model_name and not config_id:
                normalized_settings[config_id_key] = ""
                normalized_settings[model_key] = ""
                continue

            if not config_id_present and not model_name_present:
                continue

            matched_config = self._select_llm_config(
                llm_configs,
                config_id=config_id,
                model_name=model_name,
                preferred_provider=preferred_provider,
            )
            if matched_config is None:
                if config_id_present:
                    normalized_settings[config_id_key] = ""
                if model_name_present:
                    normalized_settings[model_key] = ""
                continue

            normalized_settings[config_id_key] = str(self._read_llm_config_field(matched_config, "config_id") or "")
            normalized_settings[model_key] = str(self._read_llm_config_field(matched_config, "model_name") or "")

        return normalized_settings


# 创建全局实例
config_service = ConfigService()
