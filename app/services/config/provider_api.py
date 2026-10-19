"""ConfigService.ProviderApiMixin。"""
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



class ProviderApiMixin:

    async def test_provider_api(self, provider_id: str) -> dict:
        """测试厂家API密钥"""
        try:
            print(f"🔍 测试厂家API - provider_id: {provider_id}")

            db = await self._get_db()
            providers_collection = db.llm_providers

            # 兼容处理：尝试 ObjectId 和字符串两种类型
            from bson import ObjectId
            provider_data = None
            try:
                # 先尝试作为 ObjectId 查询
                provider_data = await providers_collection.find_one({"_id": ObjectId(provider_id)})
            except Exception:
                pass

            # 如果没有找到，再尝试作为字符串查询
            if not provider_data:
                provider_data = await providers_collection.find_one({"_id": provider_id})

            if not provider_data:
                return {
                    "success": False,
                    "message": f"厂家不存在 (ID: {provider_id})"
                }

            provider_name = provider_data.get("name")
            api_key = provider_data.get("api_key")
            display_name = provider_data.get("display_name", provider_name)

            if provider_name == "codex":
                return await self.test_llm_config(LLMConfig(
                    provider="codex", model_name="gpt-6-astra", timeout=600, retry_times=0,
                ))

            # 🔥 判断数据库中的 API Key 是否有效
            if not self._is_valid_api_key(api_key):
                # 数据库中的 Key 无效，尝试从环境变量读取
                env_api_key = self._get_env_api_key(provider_name)
                if env_api_key:
                    api_key = env_api_key
                    print(f"✅ 数据库配置无效，从环境变量读取到 {display_name} 的 API Key")
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} 未配置有效的API密钥（数据库和环境变量中都未找到）"
                    }
            else:
                print(f"✅ 使用数据库配置的 {display_name} API密钥")

            # 根据厂家类型调用相应的测试函数
            test_result = await self._test_provider_connection(provider_name, api_key, display_name)

            return test_result

        except Exception as e:
            print(f"测试厂家API失败: {e}")
            return {
                "success": False,
                "message": f"测试失败: {str(e)}"
            }

    async def _test_provider_connection(self, provider_name: str, api_key: str, display_name: str) -> dict:
        """测试具体厂家的连接"""
        import asyncio

        def normalize_provider_aliases(value: Any) -> set[str]:
            provider_value = getattr(value, "value", value)
            normalized = str(provider_value or "").strip().lower()

            if not normalized:
                return set()

            alias_groups = {
                "kimi": {"kimi", "moonshot"},
                "moonshot": {"kimi", "moonshot"},
            }
            return alias_groups.get(normalized, {normalized})

        # 获取厂家的配置信息（包括 test_model）
        db = await self._get_db()
        providers_collection = db.llm_providers
        provider_data = await providers_collection.find_one({"name": provider_name})
        test_model = provider_data.get("test_model") if provider_data else None

        llm_temperature = 0.1
        llm_max_tokens = 200
        llm_timeout = 15

        try:
            system_config = await self.get_system_config()
            llm_configs = system_config.llm_configs if system_config else []
            provider_aliases = normalize_provider_aliases(provider_name)

            def matches_provider(llm: Any) -> bool:
                llm_provider_aliases = normalize_provider_aliases(getattr(llm, "provider", None))
                return bool(provider_aliases & llm_provider_aliases)

            candidate_configs = [
                llm for llm in llm_configs
                if matches_provider(llm)
            ]

            matched_llm_config = None
            if test_model:
                matched_llm_config = next(
                    (
                        llm for llm in candidate_configs
                        if llm.model_name == test_model
                    ),
                    None
                )

            if matched_llm_config is None:
                matched_llm_config = next(
                    (
                        llm for llm in candidate_configs
                        if llm.enabled
                    ),
                    None
                )

            if matched_llm_config is not None:
                llm_temperature = matched_llm_config.temperature
                llm_max_tokens = matched_llm_config.max_tokens
                llm_timeout = matched_llm_config.timeout
                logger.info(
                    f"🔧 [{display_name}] API测试使用配置参数: "
                    f"model={matched_llm_config.model_name}, "
                    f"temperature={llm_temperature}, max_tokens={llm_max_tokens}, timeout={llm_timeout}"
                )
            else:
                candidate_summary = [
                    {
                        "provider": getattr(llm, "provider", None),
                        "model_name": getattr(llm, "model_name", None),
                        "enabled": getattr(llm, "enabled", None),
                    }
                    for llm in candidate_configs
                ]
                logger.info(
                    f"⚠️ [{display_name}] 未找到匹配的模型配置，使用测试默认值: "
                    f"temperature={llm_temperature}, max_tokens={llm_max_tokens}, timeout={llm_timeout}; "
                    f"provider_name={provider_name}, provider_aliases={sorted(provider_aliases)}, "
                    f"test_model={test_model}, candidate_configs={candidate_summary}"
                )
        except Exception as config_error:
            logger.warning(f"⚠️ [{display_name}] 读取模型测试配置失败，回退默认测试参数: {config_error}")

        try:
            # 聚合渠道（使用 OpenAI 兼容 API）
            if provider_name in ["302ai", "oneapi", "newapi", "custom_aggregator"]:
                # 获取厂家的 base_url
                base_url = provider_data.get("default_base_url") if provider_data else None
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._test_openai_compatible_api, api_key, display_name, base_url, provider_name, test_model,
                    llm_temperature, llm_max_tokens, llm_timeout
                )
            elif provider_name == "google":
                # 获取厂家的 base_url
                base_url = provider_data.get("default_base_url") if provider_data else None
                return await asyncio.get_event_loop().run_in_executor(None, self._test_google_api, api_key, display_name, base_url, test_model)
            elif provider_name == "deepseek":
                return await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._test_deepseek_api,
                    api_key,
                    display_name,
                    test_model,
                    llm_temperature,
                    llm_max_tokens,
                    llm_timeout,
                )
            elif provider_name == "dashscope":
                return await asyncio.get_event_loop().run_in_executor(None, self._test_dashscope_api, api_key, display_name, test_model)
            elif provider_name == "openrouter":
                return await asyncio.get_event_loop().run_in_executor(None, self._test_openrouter_api, api_key, display_name, test_model)
            elif provider_name == "openai":
                return await asyncio.get_event_loop().run_in_executor(None, self._test_openai_api, api_key, display_name, test_model)
            elif provider_name == "anthropic":
                return await asyncio.get_event_loop().run_in_executor(None, self._test_anthropic_api, api_key, display_name, test_model)
            elif provider_name == "qianfan":
                return await asyncio.get_event_loop().run_in_executor(None, self._test_qianfan_api, api_key, display_name, test_model)
            elif provider_name == "openai_compatible":
                # 🔥 通用的 OpenAI 兼容 API 测试
                logger.info(f"🔍 使用 OpenAI 兼容 API 测试通用兼容厂家: {provider_name}")
                # 获取厂家的 base_url
                base_url = provider_data.get("default_base_url") if provider_data else None

                if not base_url:
                    return {
                        "success": False,
                        "message": f"OpenAI 兼容厂家 {display_name} 未配置 API 基础 URL (default_base_url)"
                    }

                return await asyncio.get_event_loop().run_in_executor(
                    None, self._test_openai_compatible_api, api_key, display_name, base_url, provider_name, test_model,
                    llm_temperature, llm_max_tokens, llm_timeout
                )
            else:
                # 🔧 对于未知的自定义厂家，使用 OpenAI 兼容 API 测试
                logger.info(f"🔍 使用 OpenAI 兼容 API 测试自定义厂家: {provider_name}")
                # 获取厂家的 base_url
                base_url = provider_data.get("default_base_url") if provider_data else None

                if not base_url:
                    return {
                        "success": False,
                        "message": f"自定义厂家 {display_name} 未配置 API 基础 URL"
                    }

                return await asyncio.get_event_loop().run_in_executor(
                    None, self._test_openai_compatible_api, api_key, display_name, base_url, provider_name, test_model,
                    llm_temperature, llm_max_tokens, llm_timeout
                )
        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} 连接测试失败: {str(e)}"
            }

    def _test_google_api(self, api_key: str, display_name: str, base_url: str = None, test_model: str = None) -> dict:
        """测试Google AI API"""
        try:
            import requests

            # 🔥 优先使用用户填写的测试模型，如果没有则使用默认模型
            if test_model and test_model.strip():
                model_name = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {model_name}")
            else:
                model_name = "gemini-2.0-flash-exp"
                logger.info(f"⚠️ 未指定模型，使用默认模型: {model_name}")

            logger.info(f"🔍 [Google AI 测试] 开始测试")
            logger.info(f"   display_name: {display_name}")
            logger.info(f"   model_name: {model_name}")
            logger.info(f"   base_url (原始): {base_url}")
            logger.info(f"   api_key 长度: {len(api_key) if api_key else 0}")

            # 使用配置的 base_url 或默认值
            if not base_url:
                base_url = "https://generativelanguage.googleapis.com/v1beta"
                logger.info(f"   ⚠️ base_url 为空，使用默认值: {base_url}")

            # 移除末尾的斜杠
            base_url = base_url.rstrip('/')
            logger.info(f"   base_url (去除斜杠): {base_url}")

            # 如果 base_url 以 /v1 结尾，替换为 /v1beta（Google AI 的正确端点）
            if base_url.endswith('/v1'):
                base_url = base_url[:-3] + '/v1beta'
                logger.info(f"   ✅ 将 /v1 替换为 /v1beta: {base_url}")

            # 构建完整的 API 端点（使用用户配置的模型）
            url = f"{base_url}/models/{model_name}:generateContent?key={api_key}"

            logger.info(f"🔗 [Google AI 测试] 最终请求 URL: {url.replace(api_key, '***')}")

            headers = {
                "Content-Type": "application/json"
            }

            # 🔧 增加 token 限制到 2000，避免思考模式消耗导致无输出
            data = {
                "contents": [{
                    "parts": [{
                        "text": "Hello, please respond with 'OK' if you can read this."
                    }]
                }],
                "generationConfig": {
                    "maxOutputTokens": 2000,
                    "temperature": 0.1
                }
            }

            response = requests.post(url, json=data, headers=headers, timeout=15)

            print(f"📥 [Google AI 测试] 响应状态码: {response.status_code}")

            if response.status_code == 200:
                # 打印完整的响应内容用于调试
                print(f"📥 [Google AI 测试] 响应内容（前1000字符）: {response.text[:1000]}")

                result = response.json()
                print(f"📥 [Google AI 测试] 解析后的 JSON 结构:")
                print(f"   - 顶层键: {list(result.keys())}")
                print(f"   - 是否包含 'candidates': {'candidates' in result}")
                if "candidates" in result:
                    print(f"   - candidates 长度: {len(result['candidates'])}")
                    if len(result['candidates']) > 0:
                        print(f"   - candidates[0] 的键: {list(result['candidates'][0].keys())}")

                if "candidates" in result and len(result["candidates"]) > 0:
                    candidate = result["candidates"][0]
                    print(f"📥 [Google AI 测试] candidate 结构: {candidate}")

                    # 检查 finishReason
                    finish_reason = candidate.get("finishReason", "")
                    print(f"📥 [Google AI 测试] finishReason: {finish_reason}")

                    if "content" in candidate:
                        content = candidate["content"]

                        # 检查是否有 parts
                        if "parts" in content and len(content["parts"]) > 0:
                            text = content["parts"][0].get("text", "")
                            print(f"📥 [Google AI 测试] 提取的文本: {text}")

                            if text and len(text.strip()) > 0:
                                return {
                                    "success": True,
                                    "message": f"{display_name} API连接测试成功"
                                }
                            else:
                                print(f"❌ [Google AI 测试] 文本为空")
                                return {
                                    "success": False,
                                    "message": f"{display_name} API响应内容为空"
                                }
                        else:
                            # content 中没有 parts，可能是因为 MAX_TOKENS 或其他原因
                            print(f"❌ [Google AI 测试] content 中没有 parts")
                            print(f"   content 的键: {list(content.keys())}")

                            if finish_reason == "MAX_TOKENS":
                                return {
                                    "success": False,
                                    "message": f"{display_name} API响应被截断（MAX_TOKENS），请增加 maxOutputTokens 配置"
                                }
                            else:
                                return {
                                    "success": False,
                                    "message": f"{display_name} API响应格式异常（缺少 parts，finishReason: {finish_reason}）"
                                }
                    else:
                        print(f"❌ [Google AI 测试] candidate 中缺少 'content'")
                        print(f"   candidate 的键: {list(candidate.keys())}")
                        return {
                            "success": False,
                            "message": f"{display_name} API响应格式异常（缺少 content）"
                        }
                else:
                    print(f"❌ [Google AI 测试] 缺少 candidates 或 candidates 为空")
                    return {
                        "success": False,
                        "message": f"{display_name} API无有效候选响应"
                    }
            elif response.status_code == 400:
                print(f"❌ [Google AI 测试] 400 错误，响应内容: {response.text[:500]}")
                try:
                    error_detail = response.json()
                    error_msg = error_detail.get("error", {}).get("message", "未知错误")
                    return {
                        "success": False,
                        "message": f"{display_name} API请求错误: {error_msg}"
                    }
                except:
                    return {
                        "success": False,
                        "message": f"{display_name} API请求格式错误"
                    }
            elif response.status_code == 403:
                print(f"❌ [Google AI 测试] 403 错误，响应内容: {response.text[:500]}")
                return {
                    "success": False,
                    "message": f"{display_name} 认证或权限异常，请检查账号、API Key 和模型访问权限"
                }
            elif response.status_code == 402:
                print(f"❌ [Google AI 测试] 402 错误，响应内容: {response.text[:500]}")
                return {
                    "success": False,
                    "message": f"{display_name} 账号额度不足，请检查余额、套餐或模型付费权限"
                }
            elif response.status_code == 503:
                print(f"❌ [Google AI 测试] 503 错误，响应内容: {response.text[:500]}")
                try:
                    error_detail = response.json()
                    error_code = error_detail.get("code", "")
                    error_msg = error_detail.get("message", "服务暂时不可用")

                    if error_code == "NO_KEYS_AVAILABLE":
                        return {
                            "success": False,
                            "message": f"{display_name} 中转服务暂时无可用密钥，请稍后重试或联系中转服务提供商"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"{display_name} 服务暂时不可用: {error_msg}"
                        }
                except:
                    return {
                        "success": False,
                        "message": f"{display_name} 服务暂时不可用 (HTTP 503)"
                    }
            else:
                print(f"❌ [Google AI 测试] {response.status_code} 错误，响应内容: {response.text[:500]}")
                return {
                    "success": False,
                    "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }

    def _test_deepseek_api(
        self,
        api_key: str,
        display_name: str,
        test_model: str = None,
        temperature: float = 0.1,
        max_tokens: int = 200,
        timeout: int = 15,
    ) -> dict:
        """测试DeepSeek API"""
        try:
            import requests

            def _normalize_message_text(value: Any) -> str:
                if value is None:
                    return ""
                if isinstance(value, str):
                    return value.strip()
                if isinstance(value, list):
                    text_parts = []
                    for item in value:
                        if isinstance(item, dict):
                            text = item.get("text") or item.get("content") or item.get("reasoning_content")
                            if text:
                                text_parts.append(str(text))
                        elif item:
                            text_parts.append(str(item))
                    return "".join(text_parts).strip()
                return str(value).strip()

            # 🔥 优先使用用户填写的测试模型，如果没有则使用默认模型
            if test_model and test_model.strip():
                model_name = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {model_name}")
            else:
                model_name = "deepseek-chat"
                logger.info(f"⚠️ 未指定模型，使用默认模型: {model_name}")

            logger.info(f"🔍 [DeepSeek 测试] 使用模型: {model_name}")

            url = "https://api.deepseek.com/chat/completions"
            effective_temperature = float(temperature if temperature is not None else 0.1)
            effective_timeout = max(int(timeout if timeout is not None else 15), 1)
            requested_max_tokens = int(max_tokens if max_tokens is not None else 200)
            effective_max_tokens = max(requested_max_tokens, 256)

            logger.info(
                f"🔧 [DeepSeek 测试] 使用测试参数: "
                f"temperature={effective_temperature}, max_tokens={effective_max_tokens}, timeout={effective_timeout}"
            )

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            data = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": "你好，请简单介绍一下你自己。"}
                ],
                "max_tokens": effective_max_tokens,
                "temperature": effective_temperature
            }

            response = requests.post(url, json=data, headers=headers, timeout=effective_timeout)

            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    choice = result["choices"][0] or {}
                    message = choice.get("message") or {}
                    content = _normalize_message_text(message.get("content"))
                    reasoning_content = _normalize_message_text(message.get("reasoning_content"))
                    finish_reason = str(choice.get("finish_reason") or "").lower()

                    if content and len(content.strip()) > 0:
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    elif reasoning_content:
                        logger.info(
                            f"✅ [{display_name}] DeepSeek 测试返回 reasoning_content，视为连接成功: "
                            f"finish_reason={finish_reason or 'N/A'}"
                        )
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    elif finish_reason in {"length", "max_tokens"}:
                        logger.info(
                            f"✅ [{display_name}] DeepSeek 测试响应因 token 限制截断，视为连接成功: "
                            f"finish_reason={finish_reason}, max_tokens={effective_max_tokens}"
                        )
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功（响应因 token 限制截断）"
                        }
                    else:
                        logger.warning(
                            f"⚠️ [{display_name}] DeepSeek 测试返回空内容: "
                            f"finish_reason={finish_reason or 'N/A'}, has_reasoning={bool(reasoning_content)}, "
                            f"response_keys={list(result.keys())}"
                        )
                        return {
                            "success": False,
                            "message": f"{display_name} API响应为空"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} API响应格式异常"
                    }
            elif response.status_code == 401:
                return {
                    "success": False,
                    "message": f"{display_name} 账号认证失败，请检查 API Key、账号状态和模型访问权限"
                }
            elif response.status_code == 402:
                return {
                    "success": False,
                    "message": f"{display_name} 账号额度不足，请检查余额、套餐配额或模型付费权限"
                }
            elif response.status_code == 403:
                return {
                    "success": False,
                    "message": f"{display_name} 账号权限不足，或当前模型配额已用完，请检查套餐和模型授权"
                }
            else:
                return {
                    "success": False,
                    "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }

    def _test_dashscope_api(self, api_key: str, display_name: str, test_model: str = None) -> dict:
        """测试阿里云百炼API"""
        try:
            import requests

            # 🔥 优先使用用户填写的测试模型，如果没有则使用默认模型
            if test_model and test_model.strip():
                model_name = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {model_name}")
            else:
                model_name = "qwen-turbo"
                logger.info(f"⚠️ 未指定模型，使用默认模型: {model_name}")

            logger.info(f"🔍 [DashScope 测试] 使用模型: {model_name}")
            #logger.info(f"🔍 [DashScope 测试] API Key: 已配置（脱敏）")
            logger.info(f"🔍 [DashScope 测试] API Key 长度: {len(api_key) if api_key else 0}")

            # 使用阿里云百炼的OpenAI兼容接口
            url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            data = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": "你好，请简单介绍一下你自己。"}
                ],
                "max_tokens": 50,
                "temperature": 0.1
            }

            response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0]["message"]["content"]
                    if content and len(content.strip()) > 0:
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"{display_name} API响应为空"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} API响应格式异常"
                    }
            else:
                return {
                    "success": False,
                    "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }

    def _test_openrouter_api(self, api_key: str, display_name: str, test_model: str = None) -> dict:
        """测试OpenRouter API"""
        try:
            import requests

            # 🔥 优先使用用户填写的测试模型，如果没有则使用默认模型
            if test_model and test_model.strip():
                model_name = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {model_name}")
            else:
                model_name = "meta-llama/llama-3.2-3b-instruct:free"  # 使用免费模型
                logger.info(f"⚠️ 未指定模型，使用默认模型: {model_name}")

            url = "https://openrouter.ai/api/v1/chat/completions"

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://tradingagents.cn",  # OpenRouter要求
                "X-Title": "TradingAgents-CN"
            }

            data = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": "你好，请简单介绍一下你自己。"}
                ],
                "max_tokens": 50,
                "temperature": 0.1
            }

            response = requests.post(url, json=data, headers=headers, timeout=15)

            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0]["message"]["content"]
                    if content and len(content.strip()) > 0:
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"{display_name} API响应为空"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} API响应格式异常"
                    }
            else:
                return {
                    "success": False,
                    "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }

    def _test_openai_api(self, api_key: str, display_name: str, test_model: str = None) -> dict:
        """测试OpenAI API"""
        try:
            import requests

            # 🔥 优先使用用户填写的测试模型，如果没有则使用默认模型
            if test_model and test_model.strip():
                model_name = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {model_name}")
            else:
                model_name = "gpt-3.5-turbo"
                logger.info(f"⚠️ 未指定模型，使用默认模型: {model_name}")

            url = "https://api.openai.com/v1/chat/completions"

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            data = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": "你好，请简单介绍一下你自己。"}
                ],
                "max_tokens": 50,
                "temperature": 0.1
            }

            response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0]["message"]["content"]
                    if content and len(content.strip()) > 0:
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"{display_name} API响应为空"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} API响应格式异常"
                    }
            else:
                return {
                    "success": False,
                    "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }

    def _test_anthropic_api(self, api_key: str, display_name: str, test_model: str = None) -> dict:
        """测试Anthropic API"""
        try:
            import requests

            # 🔥 优先使用用户填写的测试模型，如果没有则使用默认模型
            if test_model and test_model.strip():
                model_name = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {model_name}")
            else:
                model_name = "claude-3-haiku-20240307"
                logger.info(f"⚠️ 未指定模型，使用默认模型: {model_name}")

            url = "https://api.anthropic.com/v1/messages"

            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01"
            }

            data = {
                "model": model_name,
                "max_tokens": 50,
                "messages": [
                    {"role": "user", "content": "你好，请简单介绍一下你自己。"}
                ]
            }

            response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                result = response.json()
                if "content" in result and len(result["content"]) > 0:
                    content = result["content"][0]["text"]
                    if content and len(content.strip()) > 0:
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"{display_name} API响应为空"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} API响应格式异常"
                    }
            else:
                return {
                    "success": False,
                    "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }

    def _test_qianfan_api(self, api_key: str, display_name: str, test_model: str = None) -> dict:
        """测试百度千帆API"""
        try:
            import requests

            # 🔥 优先使用用户填写的测试模型，如果没有则使用默认模型
            if test_model and test_model.strip():
                model_name = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {model_name}")
            else:
                model_name = "ernie-3.5-8k"
                logger.info(f"⚠️ 未指定模型，使用默认模型: {model_name}")

            # 千帆新一代API使用OpenAI兼容接口
            url = "https://qianfan.baidubce.com/v2/chat/completions"

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            data = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": "你好，请简单介绍一下你自己。"}
                ],
                "max_tokens": 50,
                "temperature": 0.1
            }

            response = requests.post(url, json=data, headers=headers, timeout=15)

            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0]["message"]["content"]
                    if content and len(content.strip()) > 0:
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"{display_name} API响应为空"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} API响应格式异常"
                    }
            elif response.status_code == 401:
                return {
                    "success": False,
                    "message": f"{display_name} 账号认证失败，请检查 API Key、账号状态和模型访问权限"
                }
            elif response.status_code == 402:
                return {
                    "success": False,
                    "message": f"{display_name} 账号额度不足，请检查余额、套餐配额或模型付费权限"
                }
            elif response.status_code == 403:
                return {
                    "success": False,
                    "message": f"{display_name} 账号权限不足，或当前模型配额已用完，请检查套餐和模型授权"
                }
            else:
                try:
                    error_detail = response.json()
                    error_msg = error_detail.get("error", {}).get("message", f"HTTP {response.status_code}")
                    return {
                        "success": False,
                        "message": f"{display_name} API测试失败: {error_msg}"
                    }
                except:
                    return {
                        "success": False,
                        "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                    }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }

    async def fetch_provider_models(self, provider_id: str, filters: Optional[Dict[str, Any]] = None) -> dict:
        """从厂家 API 获取模型列表

        Args:
            provider_id: 厂家 ID
            filters: 可选的筛选参数（前端拉取后在客户端筛选，服务端仅做兼容接收）
        """
        try:
            filters = filters or {}
            print(f"🔍 获取厂家模型列表 - provider_id: {provider_id}, filters: {filters}")

            db = await self._get_db()
            providers_collection = db.llm_providers

            # 兼容处理：尝试 ObjectId 和字符串两种类型
            from bson import ObjectId
            provider_data = None
            try:
                provider_data = await providers_collection.find_one({"_id": ObjectId(provider_id)})
            except Exception:
                pass

            if not provider_data:
                provider_data = await providers_collection.find_one({"_id": provider_id})

            if not provider_data:
                return {
                    "success": False,
                    "message": f"厂家不存在 (ID: {provider_id})"
                }

            provider_name = provider_data.get("name")
            api_key = provider_data.get("api_key")
            base_url = provider_data.get("default_base_url")
            display_name = provider_data.get("display_name", provider_name)

            # 火山方舟编程：直接返回硬编码的 11 个模型，不调 API
            if provider_name == "volcengine_coding":
                coding_plan_models = [
                    {"id": "doubao-seed-2.0-code", "name": "Doubao-Seed-2.0-Code"},
                    {"id": "doubao-seed-2.0-pro", "name": "Doubao-Seed-2.0-pro"},
                    {"id": "doubao-seed-2.0-lite", "name": "Doubao-Seed-2.0-lite"},
                    {"id": "doubao-seed-code", "name": "Doubao-Seed-Code"},
                    {"id": "glm-5.2", "name": "GLM-5.2"},
                    {"id": "kimi-k2.7-code", "name": "Kimi-K2.7-Code"},
                    {"id": "minimax-m3", "name": "MiniMax-M3"},
                    {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash"},
                    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro"},
                    {"id": "minimax-m2.7", "name": "MiniMax-M2.7"},
                    {"id": "kimi-k2.6", "name": "Kimi-K2.6"},
                ]
                print(f"✅ [火山方舟编程] 直接返回 {len(coding_plan_models)} 个预设模型")
                return {
                    "success": True,
                    "models": coding_plan_models,
                    "message": f"成功获取 {len(coding_plan_models)} 个预设模型"
                }

            # 🔥 判断数据库中的 API Key 是否有效
            if not self._is_valid_api_key(api_key):
                # 数据库中的 Key 无效，尝试从环境变量读取
                env_api_key = self._get_env_api_key(provider_name)
                if env_api_key:
                    api_key = env_api_key
                    print(f"✅ 数据库配置无效，从环境变量读取到 {display_name} 的 API Key")
                else:
                    # 某些聚合平台（如 OpenRouter）的 /models 端点不需要 API Key
                    print(f"⚠️ {display_name} 未配置有效的API密钥，尝试无认证访问")
            else:
                print(f"✅ 使用数据库配置的 {display_name} API密钥")

            if not base_url:
                return {
                    "success": False,
                    "message": f"{display_name} 未配置 API 基础地址 (default_base_url)"
                }

            # 调用 OpenAI 兼容的 /v1/models 端点
            import asyncio
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_models_from_api, api_key, base_url, display_name, provider_name
            )

            return result

        except Exception as e:
            print(f"获取模型列表失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "message": f"获取模型列表失败: {str(e)}"
            }

    def _fetch_models_from_api(self, api_key: str, base_url: str, display_name: str, provider_name: str = None) -> dict:
        """从 API 获取模型列表"""
        try:
            import requests

            # 🔧 智能版本号处理：只有在没有版本号的情况下才添加 /v1
            # 避免对已有版本号的URL（如智谱AI的 /v4）重复添加 /v1
            import re
            base_url = base_url.rstrip("/")
            if not re.search(r'/v\d+$', base_url):
                # URL末尾没有版本号，添加 /v1（OpenAI标准）
                base_url = base_url + "/v1"
                logger.info(f"   [获取模型列表] 添加 /v1 版本号: {base_url}")
            else:
                # URL已包含版本号（如 /v4），不添加
                logger.info(f"   [获取模型列表] 检测到已有版本号，保持原样: {base_url}")

            url = f"{base_url}/models"

            # 构建请求头
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
                print(f"🔍 请求 URL: {url} (with API Key)")
            else:
                print(f"🔍 请求 URL: {url} (without API Key)")

            response = requests.get(url, headers=headers, timeout=15)

            print(f"📊 响应状态码: {response.status_code}")
            print(f"📊 响应内容: {response.text[:500]}...")

            if response.status_code == 200:
                result = response.json()
                print(f"📊 响应 JSON 结构: {list(result.keys())}")

                if "data" in result and isinstance(result["data"], list):
                    all_models = result["data"]
                    print(f"📊 API 返回 {len(all_models)} 个模型")

                    # 打印前几个模型的完整结构（用于调试价格字段）
                    if all_models:
                        print(f"🔍 第一个模型的完整结构:")
                        import json
                        print(json.dumps(all_models[0], indent=2, ensure_ascii=False))

                    # 打印所有 Anthropic 模型（用于调试）
                    anthropic_models = [m for m in all_models if "anthropic" in m.get("id", "").lower()]
                    if anthropic_models:
                        print(f"🔍 Anthropic 模型列表 ({len(anthropic_models)} 个):")
                        for m in anthropic_models[:20]:  # 只打印前 20 个
                            print(f"   - {m.get('id')}")

                    # 过滤：只保留主流大厂的常用模型
                    filtered_models = self._filter_popular_models(all_models, provider_name)
                    print(f"✅ 过滤后保留 {len(filtered_models)} 个常用模型")

                    # 转换模型格式，包含价格信息
                    formatted_models = self._format_models_with_pricing(filtered_models)

                    return {
                        "success": True,
                        "models": formatted_models,
                        "message": f"成功获取 {len(formatted_models)} 个常用模型（已过滤）"
                    }
                else:
                    print(f"❌ 响应格式异常，期望 'data' 字段为列表")
                    return {
                        "success": False,
                        "message": f"{display_name} API 响应格式异常（缺少 data 字段或格式不正确）"
                    }
            elif response.status_code == 401:
                return {
                    "success": False,
                    "message": f"{display_name} 账号认证失败，请检查 API Key、账号状态和模型访问权限"
                }
            elif response.status_code == 402:
                return {
                    "success": False,
                    "message": f"{display_name} 账号额度不足，请检查余额、套餐配额或模型付费权限"
                }
            elif response.status_code == 403:
                return {
                    "success": False,
                    "message": f"{display_name} 账号权限不足，请检查套餐和模型授权"
                }
            else:
                try:
                    error_detail = response.json()
                    error_msg = error_detail.get("error", {}).get("message", f"HTTP {response.status_code}")
                    print(f"❌ API 错误: {error_msg}")
                    return {
                        "success": False,
                        "message": f"{display_name} API请求失败: {error_msg}"
                    }
                except:
                    print(f"❌ HTTP 错误: {response.status_code}")
                    return {
                        "success": False,
                        "message": f"{display_name} API请求失败: HTTP {response.status_code}, 响应: {response.text[:200]}"
                    }

        except Exception as e:
            print(f"❌ 异常: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "message": f"{display_name} API请求异常: {str(e)}"
            }

    def _format_models_with_pricing(self, models: list) -> list:
        """
        格式化模型列表，包含价格信息

        支持多种价格格式：
        1. OpenRouter: pricing.prompt/completion (USD per token)
        2. 302.ai: price.prompt/completion 或 price.input/output
        3. 其他: 可能没有价格信息
        """
        formatted = []
        for model in models:
            model_id = model.get("id", "")
            model_name = model.get("name", model_id)

            # 尝试从多个字段获取价格信息
            input_price_per_1k = None
            output_price_per_1k = None

            # 方式1：OpenRouter 格式 (pricing.prompt/completion)
            pricing = model.get("pricing", {})
            if pricing:
                prompt_price = pricing.get("prompt", "0")  # USD per token
                completion_price = pricing.get("completion", "0")  # USD per token

                try:
                    if prompt_price and float(prompt_price) > 0:
                        input_price_per_1k = float(prompt_price) * 1000
                    if completion_price and float(completion_price) > 0:
                        output_price_per_1k = float(completion_price) * 1000
                except (ValueError, TypeError):
                    pass

            # 方式2：302.ai 格式 (price.prompt/completion 或 price.input/output)
            if not input_price_per_1k and not output_price_per_1k:
                price = model.get("price", {})
                if price and isinstance(price, dict):
                    # 尝试 prompt/completion 字段
                    prompt_price = price.get("prompt") or price.get("input")
                    completion_price = price.get("completion") or price.get("output")

                    try:
                        if prompt_price and float(prompt_price) > 0:
                            # 假设是 per token，转换为 per 1K tokens
                            input_price_per_1k = float(prompt_price) * 1000
                        if completion_price and float(completion_price) > 0:
                            output_price_per_1k = float(completion_price) * 1000
                    except (ValueError, TypeError):
                        pass

            # 获取上下文长度
            context_length = model.get("context_length")
            if not context_length:
                # 尝试从 top_provider 获取
                top_provider = model.get("top_provider", {})
                context_length = top_provider.get("context_length")

            # 如果还是没有，尝试从 max_completion_tokens 推断
            if not context_length:
                max_tokens = model.get("max_completion_tokens")
                if max_tokens and max_tokens > 0:
                    # 通常上下文长度是最大输出的 4-8 倍
                    context_length = max_tokens * 4

            formatted_model = {
                "id": model_id,
                "name": model_name,
                "context_length": context_length,
                "input_price_per_1k": input_price_per_1k,
                "output_price_per_1k": output_price_per_1k,
            }

            formatted.append(formatted_model)

            # 打印价格信息（用于调试）
            if input_price_per_1k or output_price_per_1k:
                print(f"💰 {model_id}: 输入=${input_price_per_1k:.6f}/1K, 输出=${output_price_per_1k:.6f}/1K")

        return formatted

    def _filter_popular_models(self, models: list, provider_name: str = None) -> list:
        """过滤模型列表，只保留主流大厂的常用模型"""
        import re

        # 火山方舟编程：不过滤，返回所有模型（API 已按套餐权限返回对应的模型）
        # 火山方舟（非编程）：不过滤，返回所有模型
        if provider_name in ("volcengine_coding", "volcengine"):
            print(f"✅ [{provider_name}] 返回全部 {len(models)} 个模型（不过滤）")
            return models

        # 只保留三大厂：OpenAI、Anthropic、Google
        popular_providers = [
            "openai",      # OpenAI
            "anthropic",   # Anthropic
            "google",      # Google
        ]

        # 常见模型名称前缀（用于识别不带厂商前缀的模型）
        model_prefixes = {
            "gpt-": "openai",           # gpt-3.5-turbo, gpt-4, gpt-4o
            "o1-": "openai",            # o1-preview, o1-mini
            "claude-": "anthropic",     # claude-3-opus, claude-3-sonnet
            "gemini-": "google",        # gemini-pro, gemini-1.5-pro
            "gemini": "google",         # gemini (不带连字符)
            "doubao-": "volcengine",    # 火山方舟豆包模型
            "ep-": "volcengine",        # 火山方舟推理接入点 (Endpoint)
        }

        # 排除的关键词
        exclude_keywords = [
            "preview",
            "experimental",
            "alpha",
            "beta",
            "free",
            "extended",
            "nitro",
            ":free",
            ":extended",
            "online",  # 排除带在线搜索的版本
            "instruct",  # 排除 instruct 版本
        ]

        # 日期格式正则表达式（匹配 2024-05-13 这种格式）
        date_pattern = re.compile(r'\d{4}-\d{2}-\d{2}')

        filtered = []
        for model in models:
            model_id = model.get("id", "").lower()
            model_name = model.get("name", "").lower()

            # 检查是否属于三大厂
            # 方式1：模型ID中包含厂商名称（如 openai/gpt-4）
            is_popular_provider = any(provider in model_id for provider in popular_providers)

            # 方式2：模型ID以常见前缀开头（如 gpt-4, claude-3-sonnet）
            if not is_popular_provider:
                for prefix, provider in model_prefixes.items():
                    if model_id.startswith(prefix):
                        is_popular_provider = True
                        print(f"🔍 识别模型前缀: {model_id} -> {provider}")
                        break

            if not is_popular_provider:
                continue

            # 检查是否包含日期（排除带日期的旧版本）
            if date_pattern.search(model_id):
                print(f"⏭️ 跳过带日期的旧版本: {model_id}")
                continue

            # 检查是否包含排除关键词
            has_exclude_keyword = any(keyword in model_id or keyword in model_name for keyword in exclude_keywords)

            if has_exclude_keyword:
                print(f"⏭️ 跳过排除关键词: {model_id}")
                continue

            # 保留该模型
            print(f"✅ 保留模型: {model_id}")
            filtered.append(model)

        return filtered

    def _test_openai_compatible_api(
        self,
        api_key: str,
        display_name: str,
        base_url: str = None,
        provider_name: str = None,
        test_model: str = None,
        temperature: float = 0.1,
        max_tokens: int = 200,
        timeout: int = 15,
    ) -> dict:
        """测试 OpenAI 兼容 API（用于聚合渠道和自定义厂家）"""
        try:
            import requests

            # 如果没有提供 base_url，使用默认值
            if not base_url:
                return {
                    "success": False,
                    "message": f"{display_name} 未配置 API 基础地址 (default_base_url)"
                }

            # 🔧 智能版本号处理：只有在没有版本号的情况下才添加 /v1
            # 避免对已有版本号的URL（如智谱AI的 /v4）重复添加 /v1
            import re
            logger.info(f"   [测试API] 原始 base_url: {base_url}")
            base_url = base_url.rstrip("/")
            logger.info(f"   [测试API] 去除斜杠后: {base_url}")

            if not re.search(r'/v\d+$', base_url):
                # URL末尾没有版本号，添加 /v1（OpenAI标准）
                base_url = base_url + "/v1"
                logger.info(f"   [测试API] 添加 /v1 版本号: {base_url}")
            else:
                # URL已包含版本号（如 /v4），不添加
                logger.info(f"   [测试API] 检测到已有版本号，保持原样: {base_url}")

            url = f"{base_url}/chat/completions"
            logger.info(f"   [测试API] 最终请求URL: {url}")

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            # 🔥 优先使用用户填写的测试模型，如果没有则根据厂家类型选择默认模型
            if test_model and test_model.strip():
                # 用户填写了测试模型，优先使用
                final_test_model = test_model.strip()
                logger.info(f"🔍 使用用户指定的测试模型: {final_test_model}")
            else:
                # 用户未填写，使用系统默认值
                final_test_model = "gpt-3.5-turbo"  # 默认模型
                if provider_name == "siliconflow":
                    # 硅基流动使用免费的 Qwen 模型进行测试
                    final_test_model = "Qwen/Qwen2.5-7B-Instruct"
                    logger.info(f"🔍 硅基流动使用测试模型: {final_test_model}")
                elif provider_name == "zhipu":
                    # 智谱AI使用 glm-4 模型进行测试
                    final_test_model = "glm-4.5-flash"
                    logger.info(f"🔍 智谱AI使用测试模型: {final_test_model}")
                else:
                    logger.info(f"🔍 使用默认测试模型: {final_test_model}")

            # 使用一个通用的模型名称进行测试
            # 聚合渠道通常支持多种模型，这里使用用户指定的或默认的测试模型
            data = {
                "model": final_test_model,
                "messages": [
                    {"role": "user", "content": "Hello, please respond with 'OK' if you can read this."}
                ],
                "max_tokens": max_tokens,
                "temperature": temperature
            }

            response = requests.post(url, json=data, headers=headers, timeout=timeout)

            if response.status_code == 200:
                result = response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0]["message"]["content"]
                    if content and len(content.strip()) > 0:
                        return {
                            "success": True,
                            "message": f"{display_name} API连接测试成功"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"{display_name} API响应为空"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"{display_name} API响应格式异常"
                    }
            elif response.status_code == 401:
                return {
                    "success": False,
                    "message": f"{display_name} 账号认证失败，请检查 API Key、账号状态和模型访问权限"
                }
            elif response.status_code == 402:
                return {
                    "success": False,
                    "message": f"{display_name} 账号额度不足，请检查余额、套餐配额或模型付费权限"
                }
            elif response.status_code == 403:
                return {
                    "success": False,
                    "message": f"{display_name} 账号权限不足，或当前模型配额已用完，请检查套餐和模型授权"
                }
            else:
                try:
                    error_detail = response.json()
                    error_msg = error_detail.get("error", {}).get("message", f"HTTP {response.status_code}")
                    logger.error(f"❌ [{display_name}] API测试失败")
                    logger.error(f"   请求URL: {url}")
                    logger.error(f"   状态码: {response.status_code}")
                    logger.error(f"   错误详情: {error_detail}")
                    return {
                        "success": False,
                        "message": f"{display_name} API测试失败: {error_msg}"
                    }
                except:
                    logger.error(f"❌ [{display_name}] API测试失败")
                    logger.error(f"   请求URL: {url}")
                    logger.error(f"   状态码: {response.status_code}")
                    logger.error(f"   响应内容: {response.text[:500]}")
                    return {
                        "success": False,
                        "message": f"{display_name} API测试失败: HTTP {response.status_code}"
                    }

        except Exception as e:
            return {
                "success": False,
                "message": f"{display_name} API测试异常: {str(e)}"
            }
