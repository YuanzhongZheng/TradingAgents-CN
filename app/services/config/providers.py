"""ConfigService.ProviderConfigMixin。"""
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



class ProviderConfigMixin:

    # ========== 大模型厂家管理 ==========

    async def get_llm_providers(self) -> List[LLMProvider]:
        """获取所有大模型厂家（合并环境变量配置）"""
        try:
            db = await self._get_db()
            providers_collection = db.llm_providers

            providers_data = await providers_collection.find().sort({"_id": -1}).to_list(length=None)
            providers = []

            logger.info(f"🔍 [get_llm_providers] 从数据库获取到 {len(providers_data)} 个供应商")

            # 🔥 本地模型列表（不需要 API Key）
            local_models = ["ollama", "localai", "codex"]
            
            for provider_data in providers_data:
                provider = LLMProvider(**provider_data)

                # 初始化 extra_config
                provider.extra_config = provider.extra_config or {}

                # 🔥 本地模型不需要 API Key，跳过检查
                if provider.name.lower() in local_models:
                    provider.extra_config["has_api_key"] = True
                    provider.extra_config["source"] = "chatgpt_login" if provider.name == "codex" else "local_model"
                    provider.extra_config["api_key_required"] = False
                    logger.info(f"✅ [get_llm_providers] 本地模型 {provider.display_name} 不需要 API Key")
                else:
                    # 🔥 判断数据库中的 API Key 是否有效
                    db_key_valid = self._is_valid_api_key(provider.api_key)
                    logger.info(f"🔍 [get_llm_providers] 供应商 {provider.display_name} ({provider.name}): 数据库密钥有效={db_key_valid}")

                    if not db_key_valid:
                        # 数据库中的 Key 无效，尝试从环境变量获取
                        logger.info(f"🔍 [get_llm_providers] 尝试从环境变量获取 {provider.name} 的 API 密钥...")
                        env_key = self._get_env_api_key(provider.name)
                        if env_key:
                            provider.api_key = env_key
                            provider.extra_config["source"] = "environment"
                            provider.extra_config["has_api_key"] = True
                            logger.info(f"✅ [get_llm_providers] 从环境变量为厂家 {provider.display_name} 获取API密钥")
                        else:
                            provider.extra_config["has_api_key"] = False
                            logger.warning(f"⚠️ [get_llm_providers] 厂家 {provider.display_name} 的数据库配置和环境变量都未配置有效的API密钥")
                    else:
                        # 数据库中的 Key 有效，使用数据库配置
                        provider.extra_config["source"] = "database"
                        provider.extra_config["has_api_key"] = True
                        logger.info(f"✅ [get_llm_providers] 使用数据库配置的 {provider.display_name} API密钥")

                providers.append(provider)

            logger.info(f"🔍 [get_llm_providers] 返回 {len(providers)} 个供应商")
            return providers
        except Exception as e:
            logger.error(f"❌ [get_llm_providers] 获取厂家列表失败: {e}", exc_info=True)
            return []

    async def get_embedding_providers(self) -> List[Dict[str, Any]]:
        """获取所有支持 embedding 的厂家和模型列表"""
        try:
            db = await self._get_db()
            providers_collection = db.llm_providers
            
            # 查询支持 embedding 的厂商
            providers_data = await providers_collection.find({
                "supported_features": "embedding",  # 🔥 直接匹配，避免 $in 在某些 MongoDB 版本上返回空结果
                "is_active": True
            }).sort("priority", 1).to_list(length=None)
            
            result = []
            for provider_data in providers_data:
                provider_name = provider_data.get("name", "")
                display_name = provider_data.get("display_name", provider_name)
                embedding_model = provider_data.get("embedding_model", "")
                
                # 如果配置了 embedding_model，使用配置的模型
                if embedding_model:
                    models = [embedding_model]
                else:
                    # 否则使用默认模型列表
                    default_models: Dict[str, List[str]] = {
                        "dashscope": ["text-embedding-v3", "text-embedding-v2"],
                        "openai": ["text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"],
                        "deepseek": ["text-embedding-v3"],
                        "google": ["text-embedding-004", "text-embedding-003"],
                        "qianfan": ["bge-large-zh-v1.5", "bge-base-zh-v1.5"]
                    }
                    models = default_models.get(provider_name, [])
                
                # 只返回有模型的厂家
                if models:
                    result.append({
                        "name": provider_name,
                        "display_name": display_name,
                        "models": models
                    })

            logger.info(f"✅ [get_embedding_providers] 返回 {len(result)} 个支持 embedding 的厂家")
            return result
        except Exception as e:
            logger.error(f"❌ [get_embedding_providers] 获取 Embedding 厂家列表失败: {e}", exc_info=True)
            return []

    async def _get_current_embedding_model(self) -> str:
        """获取当前系统配置的 embedding 模型标识（如 dashscope:text-embedding-v4）"""
        try:
            from app.core.database import get_mongo_db_sync
            db = get_mongo_db_sync()
            # 优先获取激活的配置
            configs = list(db.system_configs.find(
                {"config_type": "system", "is_active": True},
                sort=[("version", -1)]
            ).limit(1))
            if configs:
                return configs[0].get("system_settings", {}).get("default_embedding_model", "")
        except Exception:
            pass
        return os.environ.get("DEFAULT_EMBEDDING_MODEL", "")

    async def _get_migration_records(self) -> Dict[str, Dict[str, Any]]:
        """从 MongoDB 读取已迁移的 collection 记录
        
        Returns:
            {collection_name: {model_used: "dashscope:text-embedding-v4", migrated_at: "..."}}
        """
        try:
            from app.core.database import get_mongo_db_sync
            db = get_mongo_db_sync()
            records = {}
            for doc in db.system_configs.find({"config_key": {"$regex": "^qdrant_migration_"}}):
                col_name = doc["config_key"].replace("qdrant_migration_", "")
                records[col_name] = doc.get("config_value", {})
            return records
        except Exception:
            return {}

    async def _save_migration_record(self, collection_name: str, model_used: str):
        """保存迁移记录到 MongoDB"""
        try:
            from app.core.database import get_mongo_db_sync
            db = get_mongo_db_sync()
            db.system_configs.update_one(
                {"config_key": f"qdrant_migration_{collection_name}"},
                {"$set": {
                    "config_key": f"qdrant_migration_{collection_name}",
                    "config_type": "system",
                    "config_value": {
                        "model_used": model_used,
                        "migrated_at": datetime.utcnow().isoformat(),
                    },
                    "updated_at": datetime.utcnow(),
                }},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"⚠️ 保存迁移记录失败: {e}")

    async def get_qdrant_collections(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取 Qdrant 中所有 collection 信息（主存储 + mem0），用于迁移选择
        
        返回按存储区域分组的数据，包含迁移状态标记，无需初始化 embedder。
        """
        result = {"main": [], "mem0": []}
        try:
            # 获取当前 embedding 模型和迁移记录
            current_model = await self._get_current_embedding_model()
            migration_records = await self._get_migration_records()

            def _migration_status(col_name: str, source: str) -> str:
                """判断迁移状态"""
                record = migration_records.get(col_name)
                if not record:
                    return "pending"
                if record.get("model_used") == current_model:
                    return "migrated"
                return "outdated"

            # 1. 主存储 — 复用 QdrantAdapter 单例的已有客户端，避免锁冲突
            try:
                from core.memory.qdrant_adapter import QdrantAdapter
                adapter = QdrantAdapter()
                for col in adapter.list_collections():
                    result["main"].append({
                        "name": col["name"],
                        "point_count": col["point_count"],
                        "dimensions": col["dimensions"],
                        "source": "main",
                        "migration_status": _migration_status(col["name"], "main"),
                        "current_model": current_model,
                        "last_migration": migration_records.get(col["name"], {}).get("migrated_at") or None,
                    })
                result["main"] = sorted(result["main"], key=lambda x: x["name"])
                logger.info(f"✅ [get_qdrant_collections] 主存储: {len(result['main'])} 个 collection")
            except Exception as e:
                logger.warning(f"⚠️ [get_qdrant_collections] 主存储读取失败: {e}")

            # 2. mem0 存储
            try:
                from qdrant_client import QdrantClient
                qdrant_base = os.environ.get("QDRANT_DATA_DIR", "./data/qdrant")
                mem0_path = os.path.join(os.path.abspath(qdrant_base), "mem0")
                if os.path.exists(mem0_path):
                    mem0_client = QdrantClient(path=mem0_path)
                    for col in mem0_client.get_collections().collections:
                        if not col.name.startswith("mem0_memories"):
                            continue
                        info = mem0_client.get_collection(col.name)
                        result["mem0"].append({
                            "name": col.name,
                            "point_count": info.points_count or 0,
                            "dimensions": info.config.params.vectors.size if info.config.params.vectors else 0,
                            "source": "mem0",
                            "migration_status": _migration_status(col.name, "mem0"),
                            "current_model": current_model,
                            "last_migration": migration_records.get(col.name, {}).get("migrated_at") or None,
                        })
                    mem0_client.close()
                    result["mem0"] = sorted(result["mem0"], key=lambda x: x["name"])
                    logger.info(f"✅ [get_qdrant_collections] mem0 存储: {len(result['mem0'])} 个 collection")
            except Exception as e:
                logger.warning(f"⚠️ [get_qdrant_collections] mem0 存储读取失败: {e}")

            return result
        except Exception as e:
            logger.error(f"❌ [get_qdrant_collections] 获取 collection 列表失败: {e}", exc_info=True)
            return {"main": [], "mem0": []}

    async def get_mem0_collections(self) -> List[Dict[str, Any]]:
        """[兼容旧接口] 获取 Qdrant 中所有 mem0 相关 collection 信息"""
        data = await self.get_qdrant_collections()
        return data.get("mem0", [])

    async def delete_qdrant_collection(self, collection_name: str, source: str = "main", force: bool = False) -> Dict[str, Any]:
        """删除 Qdrant collection
        
        Args:
            collection_name: collection 名称
            source: "main" 或 "mem0"
            force: 是否强制删除（跳过点数和安全性检查）
        """
        if not collection_name:
            raise ValueError("collection 名称不能为空")

        try:
            from qdrant_client import QdrantClient
            from core.memory.qdrant_adapter import QdrantAdapter

            qdrant_base = os.environ.get("QDRANT_DATA_DIR", "./data/qdrant")

            if source == "mem0":
                client = QdrantClient(path=os.path.join(os.path.abspath(qdrant_base), "mem0"))
            else:
                # 主存储，复用单例客户端
                client = QdrantAdapter().get_raw_client()

            # 检查是否存在
            existing = {col.name for col in client.get_collections().collections}
            if collection_name not in existing:
                if source == "mem0":
                    client.close()
                raise RuntimeError(f"collection 不存在: {collection_name}")

            # 安全校验：非强制模式下检查是否为空
            if not force:
                info = client.get_collection(collection_name)
                if (info.points_count or 0) > 0:
                    if source == "mem0":
                        client.close()
                    raise RuntimeError(
                        f"collection {collection_name} 包含 {info.points_count} 条数据，"
                        f"请先迁移或使用 force=true 强制删除"
                    )

            client.delete_collection(collection_name)
            if source == "mem0":
                client.close()

            logger.info(f"🗑️ [delete_qdrant_collection] 已删除 collection: {collection_name} (source={source})")
            return {"success": True, "message": f"已删除 {collection_name}"}
        except Exception as e:
            logger.error(f"❌ [delete_qdrant_collection] 删除失败: {e}", exc_info=True)
            raise

    async def migrate_qdrant_collection(self, collection_name: str, dry_run: bool = False) -> Dict[str, Any]:
        """将主存储中的 collection 向量用当前 embedding 模型重新编码（原地迁移）
        
        Args:
            collection_name: collection 名称（如 memory_bull_researcher_v2）
            dry_run: 是否只预览不执行
        """
        if not collection_name:
            raise ValueError("collection 名称不能为空")

        try:
            from qdrant_client.models import PointStruct
            from core.llm.embedding_manager import EmbeddingManager
            from core.memory.qdrant_adapter import QdrantAdapter

            # 复用 QdrantAdapter 单例的已有客户端
            adapter = QdrantAdapter()
            client = adapter.get_raw_client()

            # 检查 collection 是否存在
            existing = {col.name for col in client.get_collections().collections}
            if collection_name not in existing:
                raise RuntimeError(f"collection 不存在: {collection_name}")

            info = client.get_collection(collection_name)
            target_dims = info.config.params.vectors.size if info.config.params.vectors else 0

            # 获取当前 embedding manager
            embed_manager = EmbeddingManager()

            logger.info(
                f"🔄 [主存储迁移] collection={collection_name} dims={target_dims} dry_run={dry_run}"
            )

            batch_size = 10
            total_seen = 0
            total_migrated = 0
            total_skipped = 0

            if not dry_run:
                offset = None
                while True:
                    points, offset = client.scroll(
                        collection_name=collection_name,
                        limit=batch_size,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    if not points:
                        break

                    to_upsert = []
                    for point in points:
                        payload = dict(point.payload or {})
                        # 提取文本内容用于重新编码
                        text = str(payload.get("document") or payload.get("content") or "").strip()
                        if not text:
                            total_skipped += 1
                            continue

                        embedding, provider = embed_manager.get_embedding(text)
                        if embedding is None:
                            total_skipped += 1
                            continue

                        to_upsert.append(PointStruct(id=point.id, vector=embedding, payload=payload))

                    if to_upsert:
                        client.upsert(collection_name=collection_name, points=to_upsert)

                    total_seen += len(points)
                    total_migrated += len(to_upsert)
                    logger.info(
                        f"  批次: 已处理={total_seen}, 已迁移={total_migrated}, 已跳过={total_skipped}"
                    )

                    if offset is None:
                        break
            else:
                # dry_run: 只统计
                offset = None
                while True:
                    points, offset = client.scroll(
                        collection_name=collection_name,
                        limit=batch_size,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    if not points:
                        break
                    for point in points:
                        payload = dict(point.payload or {})
                        text = str(payload.get("document") or payload.get("content") or "").strip()
                        if text:
                            total_migrated += 1
                        else:
                            total_skipped += 1
                    total_seen += len(points)
                    if offset is None:
                        break

            result = {
                "success": True,
                "collection_name": collection_name,
                "total_seen": total_seen,
                "migrated": total_migrated,
                "skipped": total_skipped,
                "dry_run": dry_run,
            }

            # 迁移成功后记录状态
            if not dry_run and total_migrated > 0:
                current_model = await self._get_current_embedding_model()
                await self._save_migration_record(collection_name, current_model)
                result["model_used"] = current_model

            logger.info(f"✅ [主存储迁移] 完成: {result}")
            return result
        except Exception as e:
            logger.error(f"❌ [主存储迁移] 失败: {e}", exc_info=True)
            raise

    async def migrate_mem0_collection(self, source_collection: str, dry_run: bool = False) -> Dict[str, Any]:
        """将旧 Qdrant mem0 collection 的数据迁移到 MongoDB（向量存储已从 Qdrant 迁移到 MongoDB）

        从 Qdrant 本地存储读取所有点（含 payload + vector），直接写入 MongoDB 同名 collection，
        文档格式与 mongodb_vector_adapter.add() 一致。
        """
        if not source_collection or not source_collection.startswith("mem0_memories"):
            raise ValueError("源 collection 名称必须以 mem0_memories 开头")

        try:
            import sys
            # Qdrant pickle 反序列化需要 core 模块在 path 中
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            from qdrant_client import QdrantClient
            from pymongo import UpdateOne
            from app.core.database import get_mongo_db_sync

            qdrant_base = os.environ.get("QDRANT_DATA_DIR", "./data/qdrant")
            mem0_path = os.path.join(qdrant_base, "mem0")
            client = QdrantClient(path=mem0_path)

            # 检查 Qdrant 源是否存在
            qdrant_collections = {col.name for col in client.get_collections().collections}
            if source_collection not in qdrant_collections:
                client.close()
                raise RuntimeError(f"Qdrant 中不存在源 collection: {source_collection}")

            source_count = client.count(source_collection).count
            logger.info(f"🔄 Qdrant->MongoDB 迁移开始: {source_collection} ({source_count} points), dry_run={dry_run}")

            mongo_db = get_mongo_db_sync()
            mongo_col = mongo_db[source_collection]

            batch_size = 100
            total_migrated = 0
            total_skipped = 0
            offset = None

            while True:
                points, offset = client.scroll(
                    collection_name=source_collection,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
                if not points:
                    break

                operations = []
                for point in points:
                    doc_id = str(point.id)
                    vector = point.vector
                    # 处理 named vector 的情况
                    if isinstance(vector, dict):
                        vector = list(vector.values())[0] if vector else []
                    elif not isinstance(vector, list):
                        vector = list(vector) if vector else []

                    payload = point.payload or {}
                    doc = {
                        "embedding": vector,
                        "document": payload.get("document", payload.get("page_content", payload.get("data", ""))),
                    }
                    # 将 payload 字段平铺到文档顶层
                    for k, v in payload.items():
                        if k not in ("document", "page_content"):
                            doc[k] = v
                    if not doc.get("document") and payload.get("page_content"):
                        doc["document"] = payload["page_content"]

                    operations.append(
                        UpdateOne(
                            {"_id": doc_id},
                            {"$set": doc},
                            upsert=True,
                        )
                    )

                if not dry_run and operations:
                    result = mongo_col.bulk_write(operations, ordered=False)
                    total_migrated += len(points)
                    logger.info(
                        f"  批次: 已处理={total_migrated}/{source_count}, "
                        f"upserted={result.upserted_count}, modified={result.modified_count}"
                    )
                else:
                    total_migrated += len(points)

                if offset is None:
                    break

            client.close()

            # 验证
            mongo_count = mongo_col.count_documents({}) if not dry_run else 0

            result = {
                "success": True,
                "source_collection": source_collection,
                "target": "mongodb",
                "source_count": source_count,
                "migrated": total_migrated,
                "skipped": total_skipped,
                "mongo_count": mongo_count,
                "dry_run": dry_run,
            }
            logger.info(f"✅ Qdrant->MongoDB 迁移完成: {result}")
            return result
        except Exception as e:
            logger.error(f"❌ 迁移失败: {e}", exc_info=True)
            raise

    def _is_valid_api_key(self, api_key: Optional[str]) -> bool:
        """
        判断 API Key 是否有效

        有效条件：
        1. Key 不为空
        2. Key 不是占位符（不以 'your_' 或 'your-' 开头，不以 '_here' 结尾）
        3. Key 不是截断的密钥（不包含 '...'）
        4. Key 长度 > 10（基本的格式验证）

        Args:
            api_key: 待验证的 API Key

        Returns:
            bool: True 表示有效，False 表示无效
        """
        if not api_key:
            return False

        # 去除首尾空格
        api_key = api_key.strip()

        # 检查是否为空
        if not api_key:
            return False

        # 检查是否为占位符（前缀）
        if api_key.startswith('your_') or api_key.startswith('your-'):
            return False

        # 检查是否为占位符（后缀）
        if api_key.endswith('_here') or api_key.endswith('-here'):
            return False

        # 🔥 检查是否为截断的密钥（包含 '...'）
        if '...' in api_key:
            return False

        # 检查长度（允许较短的测试 Key，但仍需排除明显无效值）
        if len(api_key) < 5:
            return False

        return True

    def _get_env_api_key(self, provider_name: str) -> Optional[str]:
        """从环境变量获取API密钥"""
        import os

        # 环境变量映射表
        env_key_mapping = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "dashscope": "DASHSCOPE_API_KEY",
            "qianfan": "QIANFAN_API_KEY",
            "azure": "AZURE_OPENAI_API_KEY",
            "siliconflow": "SILICONFLOW_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            # 🆕 聚合渠道
            "302ai": "AI302_API_KEY",
            "oneapi": "ONEAPI_API_KEY",
            "newapi": "NEWAPI_API_KEY",
            "custom_aggregator": "CUSTOM_AGGREGATOR_API_KEY"
        }

        env_var = env_key_mapping.get(provider_name)
        if env_var:
            api_key = os.getenv(env_var)
            # 使用统一的验证方法
            if self._is_valid_api_key(api_key):
                return api_key

        return None

    async def add_llm_provider(self, provider: LLMProvider) -> str:
        """添加大模型厂家"""
        try:
            db = await self._get_db()
            providers_collection = db.llm_providers

            # 检查厂家名称是否已存在
            existing = await providers_collection.find_one({"name": provider.name})
            if existing:
                raise ValueError(f"厂家 {provider.name} 已存在")

            provider.created_at = now_tz()
            provider.updated_at = now_tz()

            # 修复：删除 _id 字段，让 MongoDB 自动生成 ObjectId
            provider_data = provider.model_dump(by_alias=True, exclude_unset=True)
            if "_id" in provider_data:
                del provider_data["_id"]

            result = await providers_collection.insert_one(provider_data)
            return str(result.inserted_id)
        except Exception as e:
            print(f"添加厂家失败: {e}")
            raise

    async def update_llm_provider(self, provider_id: str, update_data: Dict[str, Any]) -> bool:
        """更新大模型厂家"""
        try:
            db = await self._get_db()
            providers_collection = db.llm_providers

            update_data["updated_at"] = now_tz()

            # 兼容处理：尝试 ObjectId 和字符串两种类型
            # 原因：历史数据可能混用了 ObjectId 和字符串作为 _id
            try:
                # 先尝试作为 ObjectId 查询
                result = await providers_collection.update_one(
                    {"_id": ObjectId(provider_id)},
                    {"$set": update_data}
                )

                # 如果没有匹配到，再尝试作为字符串查询
                if result.matched_count == 0:
                    result = await providers_collection.update_one(
                        {"_id": provider_id},
                        {"$set": update_data}
                    )
            except Exception:
                # 如果 ObjectId 转换失败，直接用字符串查询
                result = await providers_collection.update_one(
                    {"_id": provider_id},
                    {"$set": update_data}
                )

            # 修复：matched_count > 0 表示找到了记录（即使没有修改）
            # modified_count > 0 只有在实际修改了字段时才为真
            # 如果记录存在但值相同，modified_count 为 0，但这不应该返回 404
            return result.matched_count > 0
        except Exception as e:
            print(f"更新厂家失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def delete_llm_provider(self, provider_id: str) -> bool:
        """删除大模型厂家"""
        try:
            print(f"🗑️ 删除厂家 - provider_id: {provider_id}")
            print(f"🔍 ObjectId类型: {type(ObjectId(provider_id))}")

            db = await self._get_db()
            providers_collection = db.llm_providers
            print(f"📊 数据库: {db.name}, 集合: {providers_collection.name}")

            # 先列出所有厂家的ID，看看格式
            all_providers = await providers_collection.find({}, {"_id": 1, "display_name": 1}).to_list(length=None)
            print(f"📋 数据库中所有厂家ID:")
            for p in all_providers:
                print(f"   - {p['_id']} ({type(p['_id'])}) - {p.get('display_name')}")
                if str(p['_id']) == provider_id:
                    print(f"   ✅ 找到匹配的ID!")

            # 尝试不同的查找方式
            print(f"🔍 尝试用ObjectId查找...")
            existing1 = await providers_collection.find_one({"_id": ObjectId(provider_id)})

            print(f"🔍 尝试用字符串查找...")
            existing2 = await providers_collection.find_one({"_id": provider_id})

            print(f"🔍 ObjectId查找结果: {existing1 is not None}")
            print(f"🔍 字符串查找结果: {existing2 is not None}")

            existing = existing1 or existing2
            if not existing:
                print(f"❌ 两种方式都找不到厂家: {provider_id}")
                return False

            print(f"✅ 找到厂家: {existing.get('display_name')}")

            # 使用找到的方式进行删除
            if existing1:
                result = await providers_collection.delete_one({"_id": ObjectId(provider_id)})
            else:
                result = await providers_collection.delete_one({"_id": provider_id})

            success = result.deleted_count > 0

            print(f"🗑️ 删除结果: {success}, deleted_count: {result.deleted_count}")
            return success

        except Exception as e:
            print(f"❌ 删除厂家失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def toggle_llm_provider(self, provider_id: str, is_active: bool) -> bool:
        """切换大模型厂家状态"""
        try:
            db = await self._get_db()
            providers_collection = db.llm_providers

            # 兼容处理：尝试 ObjectId 和字符串两种类型
            try:
                # 先尝试作为 ObjectId 查询
                result = await providers_collection.update_one(
                    {"_id": ObjectId(provider_id)},
                    {"$set": {"is_active": is_active, "updated_at": now_tz()}}
                )

                # 如果没有匹配到，再尝试作为字符串查询
                if result.matched_count == 0:
                    result = await providers_collection.update_one(
                        {"_id": provider_id},
                        {"$set": {"is_active": is_active, "updated_at": now_tz()}}
                    )
            except Exception:
                # 如果 ObjectId 转换失败，直接用字符串查询
                result = await providers_collection.update_one(
                    {"_id": provider_id},
                    {"$set": {"is_active": is_active, "updated_at": now_tz()}}
                )

            return result.matched_count > 0
        except Exception as e:
            print(f"切换厂家状态失败: {e}")
            return False

    async def init_aggregator_providers(self) -> Dict[str, Any]:
        """
        初始化聚合渠道厂家配置

        Returns:
            初始化结果统计
        """
        from app.constants.model_capabilities import AGGREGATOR_PROVIDERS

        try:
            db = await self._get_db()
            providers_collection = db.llm_providers

            added_count = 0
            skipped_count = 0
            updated_count = 0

            for provider_name, config in AGGREGATOR_PROVIDERS.items():
                # 从环境变量获取 API Key
                api_key = self._get_env_api_key(provider_name)

                # 检查是否已存在
                existing = await providers_collection.find_one({"name": provider_name})

                if existing:
                    # 如果已存在但没有 API Key，且环境变量中有，则更新
                    if not existing.get("api_key") and api_key:
                        update_data = {
                            "api_key": api_key,
                            "is_active": True,  # 有 API Key 则自动启用
                            "updated_at": now_tz()
                        }
                        await providers_collection.update_one(
                            {"name": provider_name},
                            {"$set": update_data}
                        )
                        updated_count += 1
                        print(f"✅ 更新聚合渠道 {config['display_name']} 的 API Key")
                    else:
                        skipped_count += 1
                        print(f"⏭️ 聚合渠道 {config['display_name']} 已存在，跳过")
                    continue

                # 创建聚合渠道厂家配置
                provider_data = {
                    "name": provider_name,
                    "display_name": config["display_name"],
                    "description": config["description"],
                    "website": config.get("website"),
                    "api_doc_url": config.get("api_doc_url"),
                    "default_base_url": config["default_base_url"],
                    "is_active": bool(api_key),  # 有 API Key 则自动启用
                    "supported_features": ["chat", "completion", "function_calling", "streaming"],
                    "api_key": api_key or "",
                    "extra_config": {
                        "supported_providers": config.get("supported_providers", []),
                        "source": "environment" if api_key else "manual"
                    },
                    # 🆕 聚合渠道标识
                    "is_aggregator": True,
                    "aggregator_type": "openai_compatible",
                    "model_name_format": config.get("model_name_format", "{provider}/{model}"),
                    "created_at": now_tz(),
                    "updated_at": now_tz()
                }

                provider = LLMProvider(**provider_data)
                # 修复：删除 _id 字段，让 MongoDB 自动生成 ObjectId
                insert_data = provider.model_dump(by_alias=True, exclude_unset=True)
                if "_id" in insert_data:
                    del insert_data["_id"]
                await providers_collection.insert_one(insert_data)
                added_count += 1

                if api_key:
                    print(f"✅ 添加聚合渠道: {config['display_name']} (已从环境变量获取 API Key)")
                else:
                    print(f"✅ 添加聚合渠道: {config['display_name']} (需手动配置 API Key)")

            message_parts = []
            if added_count > 0:
                message_parts.append(f"成功添加 {added_count} 个聚合渠道")
            if updated_count > 0:
                message_parts.append(f"更新 {updated_count} 个")
            if skipped_count > 0:
                message_parts.append(f"跳过 {skipped_count} 个已存在的")

            return {
                "success": True,
                "added": added_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "message": "，".join(message_parts) if message_parts else "无变更"
            }

        except Exception as e:
            print(f"❌ 初始化聚合渠道失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e),
                "message": "初始化聚合渠道失败"
            }

    async def migrate_env_to_providers(self) -> Dict[str, Any]:
        """将环境变量配置迁移到厂家管理"""
        import os

        try:
            db = await self._get_db()
            providers_collection = db.llm_providers

            # 预设厂家配置
            default_providers = [
                {
                    "name": "openai",
                    "display_name": "OpenAI",
                    "description": "OpenAI是人工智能领域的领先公司，提供GPT系列模型",
                    "website": "https://openai.com",
                    "api_doc_url": "https://platform.openai.com/docs",
                    "default_base_url": "https://api.openai.com/v1",
                    "supported_features": ["chat", "completion", "embedding", "image", "vision", "function_calling", "streaming"]
                },
                {
                    "name": "anthropic",
                    "display_name": "Anthropic",
                    "description": "Anthropic专注于AI安全研究，提供Claude系列模型",
                    "website": "https://anthropic.com",
                    "api_doc_url": "https://docs.anthropic.com",
                    "default_base_url": "https://api.anthropic.com",
                    "supported_features": ["chat", "completion", "function_calling", "streaming"]
                },
                {
                    "name": "dashscope",
                    "display_name": "阿里云百炼",
                    "description": "阿里云百炼大模型服务平台，提供通义千问等模型",
                    "website": "https://bailian.console.aliyun.com",
                    "api_doc_url": "https://help.aliyun.com/zh/dashscope/",
                    "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "supported_features": ["chat", "completion", "embedding", "function_calling", "streaming"]
                },
                {
                    "name": "deepseek",
                    "display_name": "DeepSeek",
                    "description": "DeepSeek提供高性能的AI推理服务",
                    "website": "https://www.deepseek.com",
                    "api_doc_url": "https://platform.deepseek.com/api-docs",
                    "default_base_url": "https://api.deepseek.com",
                    "supported_features": ["chat", "completion", "function_calling", "streaming"]
                }
            ]

            migrated_count = 0
            updated_count = 0
            skipped_count = 0

            for provider_config in default_providers:
                # 从环境变量获取API密钥
                api_key = self._get_env_api_key(provider_config["name"])

                # 检查是否已存在
                existing = await providers_collection.find_one({"name": provider_config["name"]})

                if existing:
                    # 如果已存在但没有API密钥，且环境变量中有密钥，则更新
                    if not existing.get("api_key") and api_key:
                        update_data = {
                            "api_key": api_key,
                            "is_active": True,
                            "extra_config": {"migrated_from": "environment"},
                            "updated_at": now_tz()
                        }
                        await providers_collection.update_one(
                            {"name": provider_config["name"]},
                            {"$set": update_data}
                        )
                        updated_count += 1
                        print(f"✅ 更新厂家 {provider_config['display_name']} 的API密钥")
                    else:
                        skipped_count += 1
                        print(f"⏭️ 跳过厂家 {provider_config['display_name']} (已有配置)")
                    continue

                # 创建新厂家配置
                provider_data = {
                    **provider_config,
                    "api_key": api_key,
                    "is_active": bool(api_key),  # 有密钥的自动启用
                    "extra_config": {"migrated_from": "environment"} if api_key else {},
                    "created_at": now_tz(),
                    "updated_at": now_tz()
                }

                await providers_collection.insert_one(provider_data)
                migrated_count += 1
                print(f"✅ 创建厂家 {provider_config['display_name']}")

            total_changes = migrated_count + updated_count
            message_parts = []
            if migrated_count > 0:
                message_parts.append(f"新建 {migrated_count} 个厂家")
            if updated_count > 0:
                message_parts.append(f"更新 {updated_count} 个厂家的API密钥")
            if skipped_count > 0:
                message_parts.append(f"跳过 {skipped_count} 个已配置的厂家")

            if total_changes > 0:
                message = "迁移完成：" + "，".join(message_parts)
            else:
                message = "所有厂家都已配置，无需迁移"

            return {
                "success": True,
                "migrated_count": migrated_count,
                "updated_count": updated_count,
                "skipped_count": skipped_count,
                "message": message
            }

        except Exception as e:
            print(f"环境变量迁移失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": "环境变量迁移失败"
            }
