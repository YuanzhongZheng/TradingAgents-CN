"""ConfigService.LlmTestingMixin。"""
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



class LlmTestingMixin:

    async def test_llm_config(self, llm_config: LLMConfig) -> Dict[str, Any]:
        """测试大模型配置 - 真实调用API进行验证"""
        start_time = time.time()
        try:
            import requests

            # 获取 provider 字符串值（兼容枚举和字符串）
            provider_str = llm_config.provider.value if hasattr(llm_config.provider, 'value') else str(llm_config.provider)

            logger.info(f"🧪 测试大模型配置: {provider_str} - {llm_config.model_name}")
            if provider_str == "codex":
                from core.llm.codex_runtime import acomplete
                result = await acomplete(
                    [{"role": "user", "content": "请只回复 ASTRA_OK。"}],
                    model=llm_config.model_name, timeout=llm_config.timeout,
                    reasoning_effort=llm_config.reasoning_effort or "medium",
                )
                ok = result["content"].strip() == "ASTRA_OK"
                return {"success": ok, "message": "本机 Codex / Astra 连接成功（ChatGPT 登录）" if ok else "Astra 返回了非预期测试结果",
                        "response_time": time.time() - start_time,
                        "details": {"model": llm_config.model_name, "auth": "chatgpt"}}
            logger.info(f"📍 API基础URL (模型配置): {llm_config.api_base}")

            # 获取厂家配置（用于获取 API Key 和 default_base_url）
            db = await self._get_db()
            providers_collection = db.llm_providers
            provider_data = await providers_collection.find_one({"name": provider_str})

            # 1. 确定 API 基础 URL
            api_base = llm_config.api_base
            if not api_base:
                # 如果模型配置没有 api_base，从厂家配置获取 default_base_url
                if provider_data and provider_data.get("default_base_url"):
                    api_base = provider_data["default_base_url"]
                    logger.info(f"✅ 从厂家配置获取 API 基础 URL: {api_base}")
                else:
                    return {
                        "success": False,
                        "message": f"模型配置和厂家配置都未设置 API 基础 URL",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

            # 🔥 本地模型列表（不需要 API Key）
            local_models = ["ollama", "localai", "codex"]
            is_local_model = provider_str.lower() in local_models

            # 2. 验证 API Key（优先级：厂家配置 > 模型配置 > 环境变量）
            # 🔥 本地模型不需要 API Key，跳过检查
            api_key = None
            api_key_source = None

            if is_local_model:
                # 本地模型不需要 API Key，使用占位符
                api_key = "ollama"  # 占位符，实际不会使用
                api_key_source = "本地模型（不需要 API Key）"
                logger.info(f"✅ 本地模型 {provider_str} 不需要 API Key")
            else:
                # 云端模型需要 API Key
                # 优先从厂家配置获取 API Key
                if provider_data and provider_data.get("api_key") and self._is_valid_api_key(provider_data.get("api_key")):
                    api_key = provider_data["api_key"]
                    api_key_source = "厂家配置（数据库）"
                    logger.info(f"✅ 从厂家配置获取到API密钥")
                # 其次从模型配置获取
                elif llm_config.api_key and self._is_valid_api_key(llm_config.api_key):
                    api_key = llm_config.api_key
                    api_key_source = "模型配置"
                    logger.info(f"✅ 从模型配置获取到API密钥")
                # 最后尝试从环境变量获取
                else:
                    api_key = self._get_env_api_key(provider_str)
                    if api_key:
                        api_key_source = "环境变量"
                        logger.info(f"✅ 从环境变量获取到API密钥")

                # 云端模型必须配置有效的 API Key
                if not api_key or not self._is_valid_api_key(api_key):
                    return {
                        "success": False,
                        "message": f"{provider_str} 未配置有效的API密钥（已检查：厂家配置、模型配置、环境变量）",
                        "response_time": time.time() - start_time,
                        "details": {
                            "provider": provider_str,
                            "checked_sources": ["厂家配置（数据库）", "模型配置", "环境变量"],
                            "has_provider_data": provider_data is not None,
                            "has_model_api_key": bool(llm_config.api_key)
                        }
                    }

            logger.info(f"🔑 使用API密钥来源: {api_key_source}")

            # 3. 根据厂家类型选择测试方法
            if provider_str == "google":
                # Google AI 使用专门的测试方法
                logger.info(f"🔍 使用 Google AI 专用测试方法")
                result = self._test_google_api(api_key, f"{provider_str} {llm_config.model_name}", api_base, llm_config.model_name)
                result["response_time"] = time.time() - start_time
                return result
            elif provider_str == "deepseek":
                # DeepSeek 使用专门的测试方法
                logger.info(f"🔍 使用 DeepSeek 专用测试方法")
                result = self._test_deepseek_api(
                    api_key,
                    f"{provider_str} {llm_config.model_name}",
                    llm_config.model_name,
                    llm_config.temperature,
                    llm_config.max_tokens,
                    llm_config.timeout,
                )
                result["response_time"] = time.time() - start_time
                return result
            elif provider_str == "dashscope":
                # DashScope 使用专门的测试方法
                logger.info(f"🔍 使用 DashScope 专用测试方法")
                result = self._test_dashscope_api(api_key, f"{provider_str} {llm_config.model_name}", llm_config.model_name)
                result["response_time"] = time.time() - start_time
                return result
            else:
                # 其他厂家使用 OpenAI 兼容的测试方法
                logger.info(f"🔍 使用 OpenAI 兼容测试方法")

                # 构建测试请求
                api_base_normalized = api_base.rstrip("/")

                # 🔥 Ollama 特殊处理：Ollama 的 OpenAI 兼容端点需要 /v1 前缀
                # 如果 base_url 是 http://localhost:11434，需要添加 /v1
                # 如果 base_url 已经是 http://localhost:11434/v1，则保持不变
                if is_local_model and provider_str.lower() == "ollama":
                    # Ollama 的 OpenAI 兼容模式需要 /v1 前缀
                    if not api_base_normalized.endswith("/v1"):
                        api_base_normalized = api_base_normalized + "/v1"
                        logger.info(f"   Ollama: 添加 /v1 版本号: {api_base_normalized}")
                    else:
                        logger.info(f"   Ollama: 检测到已有版本号，保持原样: {api_base_normalized}")
                else:
                    # 🔧 智能版本号处理：只有在没有版本号的情况下才添加 /v1
                    # 避免对已有版本号的URL（如智谱AI的 /v4）重复添加 /v1
                    import re
                    if not re.search(r'/v\d+$', api_base_normalized):
                        # URL末尾没有版本号，添加 /v1（OpenAI标准）
                        api_base_normalized = api_base_normalized + "/v1"
                        logger.info(f"   添加 /v1 版本号: {api_base_normalized}")
                    else:
                        # URL已包含版本号（如 /v4），不添加
                        logger.info(f"   检测到已有版本号，保持原样: {api_base_normalized}")

                url = f"{api_base_normalized}/chat/completions"

                # 🔥 本地模型可以不设置 Authorization header，或者使用占位符
                headers = {
                    "Content-Type": "application/json",
                }
                # 只有非本地模型才添加 Authorization header
                if not is_local_model:
                    headers["Authorization"] = f"Bearer {api_key}"
                else:
                    # 某些本地服务（如 Ollama）可能需要 Authorization header，但可以使用任意值
                    headers["Authorization"] = f"Bearer {api_key}"

                # 🔥 Ollama 模型名称格式转换：横线转冒号
                # Ollama 中模型名称使用冒号（如 qwen2.5:7b），但配置中可能使用横线（qwen2.5-7b）
                model_name_for_request = (llm_config.model_name or "").strip()
                if is_local_model and provider_str.lower() == "ollama":
                    # 将横线转换为冒号（Ollama 格式）
                    if "-" in model_name_for_request and ":" not in model_name_for_request:
                        model_name_for_request = model_name_for_request.replace("-", ":", 1)
                        logger.info(f"   Ollama: 模型名称格式转换: {llm_config.model_name} -> {model_name_for_request}")
                
                data = {
                    "model": model_name_for_request,
                    "messages": [
                        {"role": "user", "content": "Hello, please respond with 'OK' if you can read this."}
                    ],
                    "max_tokens": llm_config.max_tokens,
                    "temperature": llm_config.temperature
                }

                logger.info(f"🌐 发送测试请求到: {url}")
                logger.info(f"📦 使用模型: {llm_config.model_name}")
                logger.info(
                    f"📦 使用测试参数: max_tokens={llm_config.max_tokens}, "
                    f"temperature={llm_config.temperature}, timeout={llm_config.timeout}"
                )
                logger.info(f"📦 请求数据: {data}")

                # 发送测试请求
                response = requests.post(url, json=data, headers=headers, timeout=llm_config.timeout)
                response_time = time.time() - start_time

                logger.info(f"📡 收到响应: HTTP {response.status_code}")

                # 处理响应（仅用于 OpenAI 兼容的厂家）
                if response.status_code == 200:
                    try:
                        result = response.json()
                        logger.info(f"📦 响应JSON: {result}")

                        if "choices" in result and len(result["choices"]) > 0:
                            content = result["choices"][0]["message"]["content"]
                            logger.info(f"📝 响应内容: {content}")

                            if content and len(content.strip()) > 0:
                                logger.info(f"✅ 测试成功: {content[:50]}")
                                return {
                                    "success": True,
                                    "message": f"成功连接到 {provider_str} {llm_config.model_name}",
                                    "response_time": response_time,
                                    "details": {
                                        "provider": provider_str,
                                        "model": llm_config.model_name,
                                        "api_base": api_base,
                                        "response_preview": content[:100]
                                    }
                                }
                            else:
                                logger.warning(f"⚠️ API响应内容为空")
                                return {
                                    "success": False,
                                    "message": "API响应内容为空",
                                    "response_time": response_time,
                                    "details": None
                                }
                        else:
                            logger.warning(f"⚠️ API响应格式异常，缺少 choices 字段")
                            logger.warning(f"   响应内容: {result}")
                            return {
                                "success": False,
                                "message": "API响应格式异常",
                                "response_time": response_time,
                                "details": None
                            }
                    except Exception as e:
                        logger.error(f"❌ 解析响应失败: {e}")
                        logger.error(f"   响应文本: {response.text[:500]}")
                        return {
                            "success": False,
                            "message": f"解析响应失败: {str(e)}",
                            "response_time": response_time,
                            "details": None
                        }
                elif response.status_code == 401:
                    return {
                        "success": False,
                        "message": "API密钥无效或已过期",
                        "message": "账号认证失败，请检查 API Key、账号状态和模型访问权限",
                        "response_time": response_time,
                        "details": None
                    }
                elif response.status_code == 402:
                    return {
                        "success": False,
                        "message": "账号额度不足，请检查余额、套餐配额或模型付费权限",
                        "response_time": response_time,
                        "details": None
                    }
                elif response.status_code == 403:
                    return {
                        "success": False,
                        "message": "账号权限不足，或当前模型配额已用完，请检查套餐和模型授权",
                        "response_time": response_time,
                        "details": None
                    }
                elif response.status_code == 404:
                    # 🔥 本地模型（Ollama）404 错误的特殊提示
                    if is_local_model:
                        error_msg = (
                            f"Ollama 服务返回 404，可能的原因：\n"
                            f"1. Ollama 服务未启动（请运行 `ollama serve` 或启动 Ollama 应用）\n"
                            f"2. 模型 '{llm_config.model_name}' 未下载（请运行 `ollama pull {llm_config.model_name}`）\n"
                            f"3. API 端点不正确（当前: {url}）\n"
                            f"4. Ollama 版本过低，不支持 OpenAI 兼容模式（需要 Ollama 0.1.0+）"
                        )
                    else:
                        error_msg = f"API端点不存在，请检查API基础URL是否正确: {url}"
                    
                    return {
                        "success": False,
                        "message": error_msg,
                        "response_time": response_time,
                        "details": {
                            "url": url,
                            "is_local_model": is_local_model,
                            "provider": provider_str,
                            "model": llm_config.model_name
                        }
                    }
                else:
                    try:
                        error_detail = response.json()
                        error_msg = error_detail.get("error", {}).get("message", f"HTTP {response.status_code}")
                        return {
                            "success": False,
                            "message": f"API测试失败: {error_msg}",
                            "response_time": response_time,
                            "details": None
                        }
                    except:
                        return {
                        "success": False,
                        "message": f"API测试失败: HTTP {response.status_code}",
                        "response_time": response_time,
                        "details": None
                    }

        except requests.exceptions.Timeout:
            response_time = time.time() - start_time
            return {
                "success": False,
                "message": "连接超时，请检查API基础URL是否正确或网络是否可达",
                "response_time": response_time,
                "details": None
            }
        except requests.exceptions.ConnectionError as e:
            response_time = time.time() - start_time
            return {
                "success": False,
                "message": f"连接失败，请检查API基础URL是否正确: {str(e)}",
                "response_time": response_time,
                "details": None
            }
        except Exception as e:
            response_time = time.time() - start_time
            logger.error(f"❌ 测试大模型配置失败: {e}")
            return {
                "success": False,
                "message": f"连接失败: {str(e)}",
                "response_time": response_time,
                "details": None
            }
    
    def _truncate_api_key(self, api_key: str, prefix_len: int = 6, suffix_len: int = 6) -> str:
        """
        截断 API Key 用于显示

        Args:
            api_key: 完整的 API Key
            prefix_len: 保留前缀长度
            suffix_len: 保留后缀长度

        Returns:
            截断后的 API Key，例如：0f229a...c550ec
        """
        if not api_key or len(api_key) <= prefix_len + suffix_len:
            return api_key

        return f"{api_key[:prefix_len]}...{api_key[-suffix_len:]}"
