"""ConfigService.DataSourceTestingMixin。"""
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



class DataSourceTestingMixin:

    async def test_data_source_config(self, ds_config: DataSourceConfig) -> Dict[str, Any]:
        """测试数据源配置 - 真实调用API进行验证"""
        start_time = time.time()
        try:
            import requests
            import os

            ds_type = ds_config.type.value if hasattr(ds_config.type, 'value') else str(ds_config.type)

            logger.info(f"🧪 [TEST] Testing data source config: {ds_config.name} ({ds_type})")

            # 🔥 优先使用配置中的 API Key，如果没有或被截断，则从数据库获取
            api_key = ds_config.api_key
            used_db_credentials = False
            used_env_credentials = False

            logger.info(f"🔍 [TEST] Received API Key from config: {repr(api_key)} (type: {type(api_key).__name__}, length: {len(api_key) if api_key else 0})")

            # 根据不同的数据源类型进行测试
            if ds_type == "tushare":
                # 🔥 如果配置中的 API Key 包含 "..."（截断标记），需要验证是否是未修改的原值
                if api_key and "..." in api_key:
                    logger.info(f"🔍 [TEST] API Key contains '...' (truncated), checking if it matches database value")

                    # 从数据库中获取完整的 API Key
                    system_config = await self.get_system_config()
                    db_config = None
                    if system_config:
                        for ds in system_config.data_source_configs:
                            if ds.name == ds_config.name:
                                db_config = ds
                                break

                    if db_config and db_config.api_key:
                        # 对数据库中的完整 API Key 进行相同的截断处理
                        truncated_db_key = self._truncate_api_key(db_config.api_key)
                        logger.info(f"🔍 [TEST] Database API Key truncated: {truncated_db_key}")
                        logger.info(f"🔍 [TEST] Received API Key: {api_key}")

                        # 比较截断后的值
                        if api_key == truncated_db_key:
                            # 相同，说明用户没有修改，使用数据库中的完整值
                            api_key = db_config.api_key
                            used_db_credentials = True
                            logger.info(f"✅ [TEST] Truncated values match, using complete API Key from database (length: {len(api_key)})")
                        else:
                            # 不同，说明用户修改了但修改得不完整
                            logger.error(f"❌ [TEST] Truncated API Key doesn't match database value, user may have modified it incorrectly")
                            return {
                                "success": False,
                                "message": "API Key 格式错误：检测到截断标记但与数据库中的值不匹配，请输入完整的 API Key",
                                "response_time": time.time() - start_time,
                                "details": {
                                    "error": "truncated_key_mismatch",
                                    "received": api_key,
                                    "expected": truncated_db_key
                                }
                            }
                    else:
                        # 数据库中没有有效的 API Key，尝试从环境变量获取
                        logger.info(f"⚠️  [TEST] No valid API Key in database, trying environment variable")
                        env_token = os.getenv('TUSHARE_TOKEN')
                        if env_token:
                            api_key = env_token.strip().strip('"').strip("'")
                            used_env_credentials = True
                            logger.info(f"🔑 [TEST] Using TUSHARE_TOKEN from environment (length: {len(api_key)})")
                        else:
                            logger.error(f"❌ [TEST] No valid API Key in database or environment")
                            return {
                                "success": False,
                                "message": "API Key 无效：数据库和环境变量中均未配置有效的 Token",
                                "response_time": time.time() - start_time,
                                "details": None
                            }

                # 如果 API Key 为空，尝试从数据库或环境变量获取
                elif not api_key:
                    logger.info(f"⚠️  [TEST] API Key is empty, trying to get from database")

                    # 从数据库中获取完整的 API Key
                    system_config = await self.get_system_config()
                    db_config = None
                    if system_config:
                        for ds in system_config.data_source_configs:
                            if ds.name == ds_config.name:
                                db_config = ds
                                break

                    if db_config and db_config.api_key and "..." not in db_config.api_key:
                        api_key = db_config.api_key
                        used_db_credentials = True
                        logger.info(f"🔑 [TEST] Using API Key from database (length: {len(api_key)})")
                    else:
                        # 如果数据库中也没有，尝试从环境变量获取
                        logger.info(f"⚠️  [TEST] No valid API Key in database, trying environment variable")
                        env_token = os.getenv('TUSHARE_TOKEN')
                        if env_token:
                            api_key = env_token.strip().strip('"').strip("'")
                            used_env_credentials = True
                            logger.info(f"🔑 [TEST] Using TUSHARE_TOKEN from environment (length: {len(api_key)})")
                        else:
                            logger.error(f"❌ [TEST] No valid API Key in config, database, or environment")
                            return {
                                "success": False,
                                "message": "API Key 无效：配置、数据库和环境变量中均未配置有效的 Token",
                                "response_time": time.time() - start_time,
                                "details": None
                            }
                else:
                    # API Key 是完整的，直接使用
                    logger.info(f"✅ [TEST] Using complete API Key from config (length: {len(api_key)})")

                # 测试 Tushare API
                try:
                    logger.info(f"🔌 [TEST] Calling Tushare API with token (length: {len(api_key)})")
                    import tushare as ts
                    import os

                    # 🔧 临时禁用代理（Tushare 是国内数据源，不需要代理）
                    # 保存原始代理设置
                    original_http_proxy = os.environ.get('HTTP_PROXY')
                    original_https_proxy = os.environ.get('HTTPS_PROXY')
                    original_http_proxy_lower = os.environ.get('http_proxy')
                    original_https_proxy_lower = os.environ.get('https_proxy')

                    try:
                        # 临时清除代理
                        if 'HTTP_PROXY' in os.environ:
                            del os.environ['HTTP_PROXY']
                        if 'HTTPS_PROXY' in os.environ:
                            del os.environ['HTTPS_PROXY']
                        if 'http_proxy' in os.environ:
                            del os.environ['http_proxy']
                        if 'https_proxy' in os.environ:
                            del os.environ['https_proxy']

                        logger.info(f"🌐 [TEST] Proxy disabled for Tushare API call (domestic data source)")

                        # 直接传入 token 给 pro_api，不调用 set_token 避免写 tk.csv 文件
                        pro = ts.pro_api(api_key)
                        # 获取交易日历（轻量级测试）
                        df = pro.trade_cal(exchange='SSE', start_date='20240101', end_date='20240101')
                    finally:
                        # 恢复原始代理设置
                        if original_http_proxy is not None:
                            os.environ['HTTP_PROXY'] = original_http_proxy
                        if original_https_proxy is not None:
                            os.environ['HTTPS_PROXY'] = original_https_proxy
                        if original_http_proxy_lower is not None:
                            os.environ['http_proxy'] = original_http_proxy_lower
                        if original_https_proxy_lower is not None:
                            os.environ['https_proxy'] = original_https_proxy_lower

                    if df is not None and len(df) > 0:
                        response_time = time.time() - start_time
                        logger.info(f"✅ [TEST] Tushare API call successful (response time: {response_time:.2f}s)")

                        # 构建消息，说明使用了哪个来源的凭证
                        credential_source = "配置"
                        if used_db_credentials:
                            credential_source = "数据库"
                        elif used_env_credentials:
                            credential_source = "环境变量"

                        return {
                            "success": True,
                            "message": f"成功连接到 Tushare 数据源（使用{credential_source}中的凭证）",
                            "response_time": response_time,
                            "details": {
                                "type": ds_type,
                                "test_result": "获取交易日历成功",
                                "credential_source": credential_source,
                                "used_db_credentials": used_db_credentials,
                                "used_env_credentials": used_env_credentials
                            }
                        }
                    else:
                        logger.error(f"❌ [TEST] Tushare API returned empty data")
                        return {
                            "success": False,
                            "message": "Tushare API 返回数据为空",
                            "response_time": time.time() - start_time,
                            "details": None
                        }
                except ImportError:
                    logger.error(f"❌ [TEST] Tushare library not installed")
                    return {
                        "success": False,
                        "message": "Tushare 库未安装，请运行: pip install tushare",
                        "response_time": time.time() - start_time,
                        "details": None
                    }
                except Exception as e:
                    logger.error(f"❌ [TEST] Tushare API call failed: {e}")
                    return {
                        "success": False,
                        "message": f"Tushare API 调用失败: {str(e)}",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

            elif ds_type == "akshare":
                # AKShare 不需要 API Key，直接测试
                try:
                    import akshare as ak
                    # 使用更轻量级的接口测试 - 获取交易日历
                    # 这个接口数据量小，响应快，更适合测试连接
                    df = ak.tool_trade_date_hist_sina()

                    if df is not None and len(df) > 0:
                        response_time = time.time() - start_time
                        return {
                            "success": True,
                            "message": f"成功连接到 AKShare 数据源",
                            "response_time": response_time,
                            "details": {
                                "type": ds_type,
                                "test_result": f"获取交易日历成功（{len(df)} 条记录）"
                            }
                        }
                    else:
                        return {
                            "success": False,
                            "message": "AKShare API 返回数据为空",
                            "response_time": time.time() - start_time,
                            "details": None
                        }
                except ImportError:
                    return {
                        "success": False,
                        "message": "AKShare 库未安装，请运行: pip install akshare",
                        "response_time": time.time() - start_time,
                        "details": None
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "message": f"AKShare API 调用失败: {str(e)}",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

            elif ds_type == "qmt":
                original_env: Dict[str, Optional[str]] = {}
                temp_env = {
                    "QMT_TOKEN": ds_config.api_key or "",
                }
                qmt_param_map = {
                    "data_dir": "QMT_DATA_DIR",
                    "listen_port": "QMT_LISTEN_PORT",
                    "listen_port_range": "QMT_LISTEN_PORT_RANGE",
                    "init_markets": "QMT_INIT_MARKETS",
                    "kline_mirror_markets": "QMT_KLINE_MIRROR_MARKETS",
                    "allow_optimize_addresses": "QMT_ALLOW_OPTIMIZE_ADDRESSES",
                    "history_start_time": "QMT_HISTORY_START_TIME",
                    "start_local_service": "QMT_START_LOCAL_SERVICE",
                    "kline_mirror_enabled": "QMT_KLINE_MIRROR_ENABLED",
                    "auto_download_history": "QMT_AUTO_DOWNLOAD_HISTORY",
                    "auto_download_financial": "QMT_AUTO_DOWNLOAD_FINANCIAL",
                    "auto_download_sector": "QMT_AUTO_DOWNLOAD_SECTOR",
                }

                for key, env_key in qmt_param_map.items():
                    value = (ds_config.config_params or {}).get(key)
                    if value is None:
                        continue
                    if isinstance(value, list):
                        temp_env[env_key] = ",".join(str(item) for item in value)
                    elif isinstance(value, bool):
                        temp_env[env_key] = str(value).lower()
                    else:
                        temp_env[env_key] = str(value)

                try:
                    for env_key, env_value in temp_env.items():
                        original_env[env_key] = os.getenv(env_key)
                        if env_value:
                            os.environ[env_key] = env_value

                    from app.services.data_sources.qmt_adapter import QMTAdapter

                    logger.info(
                        "QMT 测试连接开始: data_dir=%s, init_markets=%s, start_local_service=%s",
                        temp_env.get("QMT_DATA_DIR", ""),
                        temp_env.get("QMT_INIT_MARKETS", ""),
                        temp_env.get("QMT_START_LOCAL_SERVICE", ""),
                    )

                    adapter = QMTAdapter()
                    available = await asyncio.wait_for(
                        asyncio.to_thread(adapter.is_available),
                        timeout=15.0,
                    )
                    if not available:
                        logger.warning("QMT 测试连接失败: adapter.is_available() returned False")
                        return {
                            "success": False,
                            "message": "QMT 不可用：请确认 xtquant 已安装、QMT 已启动，且本地行情连接参数正确",
                            "response_time": time.time() - start_time,
                            "details": {
                                "type": ds_type,
                                "tested_params": sorted(list((ds_config.config_params or {}).keys())),
                            }
                        }

                    return {
                        "success": True,
                        "message": "成功连接到 QMT 数据源",
                        "response_time": time.time() - start_time,
                        "details": {
                            "type": ds_type,
                            "test_result": "QMT 可用，本地 QMT 连接检测通过",
                        }
                    }
                except asyncio.TimeoutError:
                    logger.error("QMT 测试连接超时: adapter.is_available() exceeded 15 seconds")
                    return {
                        "success": False,
                        "message": "QMT 测试超时：本地连接检测超过 15 秒，请检查 QMT 当前状态后重试",
                        "response_time": time.time() - start_time,
                        "details": {
                            "type": ds_type,
                            "tested_params": sorted(list((ds_config.config_params or {}).keys())),
                        }
                    }
                except ImportError:
                    return {
                        "success": False,
                        "message": "xtquant 库未安装，请先安装并确认运行环境支持 QMT",
                        "response_time": time.time() - start_time,
                        "details": None
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "message": f"QMT 测试失败: {str(e)}",
                        "response_time": time.time() - start_time,
                        "details": None
                    }
                finally:
                    for env_key, env_value in original_env.items():
                        if env_value is None:
                            os.environ.pop(env_key, None)
                        else:
                            os.environ[env_key] = env_value

            elif ds_type == "local":
                # 本地数据源 - 直接连接数据库检查是否有本地数据
                try:
                    from app.core.database import get_mongo_db
                    db = get_mongo_db()

                    # 检查是否有本地数据（data_source = "local"）
                    # 检查多个集合
                    collections_to_check = [
                        ("stock_basic_info", "source"),  # 股票基本信息
                        ("market_quotes", "data_source"),  # 实时行情
                        ("stock_financial_data", "data_source"),  # 财务数据
                        ("stock_news", "data_source")  # 新闻数据
                    ]

                    total_count = 0
                    collection_stats = []

                    for collection_name, field_name in collections_to_check:
                        collection = db[collection_name]
                        count = await collection.count_documents({field_name: "local"})
                        total_count += count
                        if count > 0:
                            collection_stats.append(f"{collection_name}: {count}条")

                    response_time = time.time() - start_time

                    if total_count > 0:
                        stats_msg = "、".join(collection_stats)
                        return {
                            "success": True,
                            "message": f"成功连接到本地数据源，共找到 {total_count} 条本地数据",
                            "response_time": response_time,
                            "details": {
                                "type": ds_type,
                                "test_result": f"数据库连接成功，{stats_msg}",
                                "total_records": total_count
                            }
                        }
                    else:
                        return {
                            "success": True,
                            "message": "成功连接到本地数据源，但暂无本地数据（可通过批量导入 API 接口导入数据）",
                            "response_time": response_time,
                            "details": {
                                "type": ds_type,
                                "test_result": "数据库连接成功，暂无本地数据",
                                "total_records": 0
                            }
                        }
                except Exception as e:
                    return {
                        "success": False,
                        "message": f"本地数据源连接失败: {str(e)}",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

            elif ds_type == "baostock":
                # BaoStock 不需要 API Key，直接测试登录
                try:
                    import baostock as bs
                    # 测试登录
                    lg = bs.login()

                    if lg.error_code == '0':
                        # 登录成功，测试获取数据
                        try:
                            # 获取交易日历（轻量级测试）
                            rs = bs.query_trade_dates(start_date="2024-01-01", end_date="2024-01-01")

                            if rs.error_code == '0':
                                response_time = time.time() - start_time
                                bs.logout()
                                return {
                                    "success": True,
                                    "message": f"成功连接到 BaoStock 数据源",
                                    "response_time": response_time,
                                    "details": {
                                        "type": ds_type,
                                        "test_result": "登录成功，获取交易日历成功"
                                    }
                                }
                            else:
                                bs.logout()
                                return {
                                    "success": False,
                                    "message": f"BaoStock 数据获取失败: {rs.error_msg}",
                                    "response_time": time.time() - start_time,
                                    "details": None
                                }
                        except Exception as e:
                            bs.logout()
                            return {
                                "success": False,
                                "message": f"BaoStock 数据获取异常: {str(e)}",
                                "response_time": time.time() - start_time,
                                "details": None
                            }
                    else:
                        return {
                            "success": False,
                            "message": f"BaoStock 登录失败: {lg.error_msg}",
                            "response_time": time.time() - start_time,
                            "details": None
                        }
                except ImportError:
                    return {
                        "success": False,
                        "message": "BaoStock 库未安装，请运行: pip install baostock",
                        "response_time": time.time() - start_time,
                        "details": None
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "message": f"BaoStock API 调用失败: {str(e)}",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

            elif ds_type == "yahoo_finance":
                # Yahoo Finance 测试 - 需要代理访问
                if not ds_config.endpoint:
                    ds_config.endpoint = "https://query1.finance.yahoo.com"

                try:
                    # 规范化代理地址，确保有协议前缀
                    def normalize_proxy(proxy: str) -> str:
                        if not proxy:
                            return proxy
                        proxy = proxy.strip()
                        if not proxy.startswith(('http://', 'https://', 'socks5://', 'socks4://')):
                            proxy = f"http://{proxy}"
                        return proxy

                    # 获取代理配置
                    system_config = await self.get_system_config()
                    proxies = {}
                    proxy_used = False
                    if system_config and system_config.system_settings:
                        http_proxy = system_config.system_settings.get('http_proxy', '')
                        https_proxy = system_config.system_settings.get('https_proxy', '')
                        if http_proxy:
                            proxies['http'] = normalize_proxy(http_proxy)
                            proxy_used = True
                        if https_proxy:
                            proxies['https'] = normalize_proxy(https_proxy)
                            proxy_used = True

                    # 也检查环境变量
                    if not proxies:
                        env_http = os.environ.get('HTTP_PROXY', '')
                        env_https = os.environ.get('HTTPS_PROXY', '')
                        if env_http:
                            proxies['http'] = normalize_proxy(env_http)
                            proxy_used = True
                        if env_https:
                            proxies['https'] = normalize_proxy(env_https)
                            proxy_used = True

                    if not proxy_used:
                        logger.warning("⚠️ [TEST] Yahoo Finance 测试未配置代理，可能无法访问")
                    else:
                        logger.info(f"🔍 [TEST] Yahoo Finance 使用代理: {proxies}")

                    url = f"{ds_config.endpoint}/v8/finance/chart/AAPL"
                    params = {"interval": "1d", "range": "1d"}

                    # 添加浏览器 User-Agent，避免被 Yahoo Finance 拒绝
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                    }

                    response = requests.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=15,
                        proxies=proxies if proxies else None
                    )

                    # 记录响应状态和头信息用于调试
                    logger.info(f"🔍 [TEST] Yahoo Finance 响应: status={response.status_code}, headers={dict(response.headers)}")

                    if response.status_code == 200:
                        data = response.json()
                        if "chart" in data and "result" in data["chart"]:
                            response_time = time.time() - start_time
                            proxy_msg = "（使用代理）" if proxy_used else "（直连）"
                            return {
                                "success": True,
                                "message": f"成功连接到 Yahoo Finance 数据源{proxy_msg}",
                                "response_time": response_time,
                                "details": {
                                    "type": ds_type,
                                    "endpoint": ds_config.endpoint,
                                    "test_result": "获取 AAPL 数据成功",
                                    "proxy_used": proxy_used
                                }
                            }
                        elif "chart" in data and "error" in data["chart"]:
                            error_info = data["chart"]["error"]
                            return {
                                "success": False,
                                "message": f"Yahoo Finance API 错误: {error_info.get('description', str(error_info))}",
                                "response_time": time.time() - start_time,
                                "details": None
                            }
                        else:
                            logger.warning(f"⚠️ [TEST] Yahoo Finance 返回未知格式: {data}")
                            return {
                                "success": False,
                                "message": "Yahoo Finance API 返回未知格式",
                                "response_time": time.time() - start_time,
                                "details": {"raw_response": str(data)[:500]}
                            }
                    elif response.status_code == 429:
                        # HTTP 429 说明代理连接成功，只是请求频率被限制
                        proxy_msg = "（使用代理）" if proxy_used else "（直连）"
                        return {
                            "success": True,
                            "message": f"代理连接正常{proxy_msg}，但 Yahoo Finance 请求频率受限（HTTP 429），请稍后再试",
                            "response_time": time.time() - start_time,
                            "details": {
                                "type": ds_type,
                                "endpoint": ds_config.endpoint,
                                "test_result": "代理连接成功，API 频率限制",
                                "proxy_used": proxy_used,
                                "note": "HTTP 429 表示请求过于频繁，稍后会自动恢复"
                            }
                        }
                    else:
                        # 记录响应体用于调试
                        try:
                            response_body = response.text[:500]
                        except Exception:
                            response_body = "无法读取响应体"
                        logger.warning(f"⚠️ [TEST] Yahoo Finance 错误响应: status={response.status_code}, body={response_body}")
                        return {
                            "success": False,
                            "message": f"Yahoo Finance API 返回错误: HTTP {response.status_code}",
                            "response_time": time.time() - start_time,
                            "details": {
                                "status_code": response.status_code,
                                "response_body": response_body,
                                "proxy_used": proxy_used
                            }
                        }
                except requests.exceptions.ProxyError as e:
                    return {
                        "success": False,
                        "message": "代理连接失败，请检查代理配置",
                        "response_time": time.time() - start_time,
                        "details": {"error": str(e)}
                    }
                except requests.exceptions.ConnectTimeout:
                    return {
                        "success": False,
                        "message": "连接超时，请检查网络或代理配置",
                        "response_time": time.time() - start_time,
                        "details": None
                    }
                except Exception as e:
                    error_msg = str(e)
                    if "ProxyError" in error_msg or "proxy" in error_msg.lower():
                        return {
                            "success": False,
                            "message": f"代理连接失败: {error_msg}",
                            "response_time": time.time() - start_time,
                            "details": None
                        }
                    return {
                        "success": False,
                        "message": f"Yahoo Finance API 调用失败: {error_msg}",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

            elif ds_type == "alpha_vantage":
                # 🔥 如果配置中的 API Key 包含 "..."（截断标记），需要验证是否是未修改的原值
                if api_key and "..." in api_key:
                    logger.info(f"🔍 [TEST] API Key contains '...' (truncated), checking if it matches database value")

                    # 从数据库中获取完整的 API Key
                    system_config = await self.get_system_config()
                    db_config = None
                    if system_config:
                        for ds in system_config.data_source_configs:
                            if ds.name == ds_config.name:
                                db_config = ds
                                break

                    if db_config and db_config.api_key:
                        # 对数据库中的完整 API Key 进行相同的截断处理
                        truncated_db_key = self._truncate_api_key(db_config.api_key)
                        logger.info(f"🔍 [TEST] Database API Key truncated: {truncated_db_key}")
                        logger.info(f"🔍 [TEST] Received API Key: {api_key}")

                        # 比较截断后的值
                        if api_key == truncated_db_key:
                            # 相同，说明用户没有修改，使用数据库中的完整值
                            api_key = db_config.api_key
                            used_db_credentials = True
                            logger.info(f"✅ [TEST] Truncated values match, using complete API Key from database (length: {len(api_key)})")
                        else:
                            # 不同，说明用户修改了但修改得不完整
                            logger.error(f"❌ [TEST] Truncated API Key doesn't match database value")
                            return {
                                "success": False,
                                "message": "API Key 格式错误：检测到截断标记但与数据库中的值不匹配，请输入完整的 API Key",
                                "response_time": time.time() - start_time,
                                "details": {
                                    "error": "truncated_key_mismatch",
                                    "received": api_key,
                                    "expected": truncated_db_key
                                }
                            }
                    else:
                        # 数据库中没有有效的 API Key，尝试从环境变量获取
                        logger.info(f"⚠️  [TEST] No valid API Key in database, trying environment variable")
                        env_key = os.getenv('ALPHA_VANTAGE_API_KEY')
                        if env_key:
                            api_key = env_key.strip().strip('"').strip("'")
                            used_env_credentials = True
                            logger.info(f"🔑 [TEST] Using ALPHA_VANTAGE_API_KEY from environment (length: {len(api_key)})")
                        else:
                            logger.error(f"❌ [TEST] No valid API Key in database or environment")
                            return {
                                "success": False,
                                "message": "API Key 无效：数据库和环境变量中均未配置有效的 API Key",
                                "response_time": time.time() - start_time,
                                "details": None
                            }

                # 如果 API Key 为空，尝试从数据库或环境变量获取
                elif not api_key:
                    logger.info(f"⚠️  [TEST] API Key is empty, trying to get from database")

                    # 从数据库中获取完整的 API Key
                    system_config = await self.get_system_config()
                    db_config = None
                    if system_config:
                        for ds in system_config.data_source_configs:
                            if ds.name == ds_config.name:
                                db_config = ds
                                break

                    if db_config and db_config.api_key and "..." not in db_config.api_key:
                        api_key = db_config.api_key
                        used_db_credentials = True
                        logger.info(f"🔑 [TEST] Using API Key from database (length: {len(api_key)})")
                    else:
                        # 如果数据库中也没有，尝试从环境变量获取
                        logger.info(f"⚠️  [TEST] No valid API Key in database, trying environment variable")
                        env_key = os.getenv('ALPHA_VANTAGE_API_KEY')
                        if env_key:
                            api_key = env_key.strip().strip('"').strip("'")
                            used_env_credentials = True
                            logger.info(f"🔑 [TEST] Using ALPHA_VANTAGE_API_KEY from environment (length: {len(api_key)})")
                        else:
                            logger.error(f"❌ [TEST] No valid API Key in config, database, or environment")
                            return {
                                "success": False,
                                "message": "API Key 无效：配置、数据库和环境变量中均未配置有效的 API Key",
                                "response_time": time.time() - start_time,
                                "details": None
                            }
                else:
                    # API Key 是完整的，直接使用
                    logger.info(f"✅ [TEST] Using complete API Key from config (length: {len(api_key)})")

                # 测试 Alpha Vantage API
                endpoint = ds_config.endpoint or "https://www.alphavantage.co"
                url = f"{endpoint}/query"
                params = {
                    "function": "TIME_SERIES_INTRADAY",
                    "symbol": "IBM",
                    "interval": "5min",
                    "apikey": api_key
                }

                try:
                    logger.info(f"🔌 [TEST] Calling Alpha Vantage API with key (length: {len(api_key)})")
                    response = requests.get(url, params=params, timeout=10)

                    if response.status_code == 200:
                        data = response.json()
                        logger.info(f"🔍 [TEST] Alpha Vantage API response keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")

                        if "Time Series (5min)" in data or "Meta Data" in data:
                            response_time = time.time() - start_time
                            logger.info(f"✅ [TEST] Alpha Vantage API call successful (response time: {response_time:.2f}s)")

                            # 构建消息，说明使用了哪个来源的凭证
                            credential_source = "配置"
                            if used_db_credentials:
                                credential_source = "数据库"
                            elif used_env_credentials:
                                credential_source = "环境变量"

                            return {
                                "success": True,
                                "message": f"成功连接到 Alpha Vantage 数据源（使用{credential_source}中的凭证）",
                                "response_time": response_time,
                                "details": {
                                    "type": ds_type,
                                    "endpoint": endpoint,
                                    "test_result": "API 密钥有效",
                                    "credential_source": credential_source,
                                    "used_db_credentials": used_db_credentials,
                                    "used_env_credentials": used_env_credentials
                                }
                            }
                        elif "Error Message" in data:
                            return {
                                "success": False,
                                "message": f"Alpha Vantage API 错误: {data['Error Message']}",
                                "response_time": time.time() - start_time,
                                "details": None
                            }
                        elif "Note" in data:
                            return {
                                "success": False,
                                "message": f"API 调用频率超限: {data['Note']}",
                                "response_time": time.time() - start_time,
                                "details": None
                            }
                        elif "Information" in data:
                            # Alpha Vantage 返回的 Information 字段通常包含 API 限制信息
                            info_msg = data["Information"]
                            if "premium" in info_msg.lower() or "limit" in info_msg.lower() or "call frequency" in info_msg.lower():
                                return {
                                    "success": False,
                                    "message": f"API 调用频率超限: {info_msg}",
                                    "response_time": time.time() - start_time,
                                    "details": None
                                }
                            else:
                                return {
                                    "success": False,
                                    "message": f"Alpha Vantage API 信息: {info_msg}",
                                    "response_time": time.time() - start_time,
                                    "details": None
                                }
                        else:
                            # 未知响应格式，记录详情
                            logger.warning(f"⚠️ [TEST] Alpha Vantage 返回未知格式: {data}")
                            return {
                                "success": False,
                                "message": f"Alpha Vantage API 返回未知格式: {list(data.keys()) if isinstance(data, dict) else str(data)[:100]}",
                                "response_time": time.time() - start_time,
                                "details": {"raw_response": str(data)[:500]}
                            }
                    else:
                        return {
                            "success": False,
                            "message": f"Alpha Vantage API 返回错误: HTTP {response.status_code}",
                            "response_time": time.time() - start_time,
                            "details": None
                        }
                except Exception as e:
                    return {
                        "success": False,
                        "message": f"Alpha Vantage API 调用失败: {str(e)}",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

            else:
                # 其他数据源类型 - 尝试从环境变量获取 API Key（如果需要）
                # 支持的环境变量映射
                env_key_map = {
                    "finnhub": "FINNHUB_API_KEY",
                    "polygon": "POLYGON_API_KEY",
                    "iex": "IEX_API_KEY",
                    "quandl": "QUANDL_API_KEY",
                }

                # 如果配置中没有 API Key，尝试从环境变量获取
                if ds_type in env_key_map and (not api_key or "..." in api_key):
                    env_var_name = env_key_map[ds_type]
                    env_key = os.getenv(env_var_name)
                    if env_key:
                        api_key = env_key.strip()
                        used_env_credentials = True
                        logger.info(f"🔑 使用环境变量中的 {ds_type.upper()} API Key ({env_var_name})")

                # 基本的端点测试
                if ds_config.endpoint:
                    try:
                        # 如果有 API Key，添加到请求中
                        headers = {}
                        params = {}

                        if api_key:
                            # 根据不同数据源的认证方式添加 API Key
                            if ds_type == "finnhub":
                                params["token"] = api_key
                            elif ds_type in ["polygon", "alpha_vantage"]:
                                params["apiKey"] = api_key
                            elif ds_type == "iex":
                                params["token"] = api_key
                            else:
                                # 默认使用 header 认证
                                headers["Authorization"] = f"Bearer {api_key}"

                        response = requests.get(ds_config.endpoint, params=params, headers=headers, timeout=10)
                        response_time = time.time() - start_time

                        if response.status_code < 500:
                            return {
                                "success": True,
                                "message": f"成功连接到数据源 {ds_config.name}",
                                "response_time": response_time,
                                "details": {
                                    "type": ds_type,
                                    "endpoint": ds_config.endpoint,
                                    "status_code": response.status_code,
                                    "used_env_credentials": used_env_credentials
                                }
                            }
                        else:
                            return {
                                "success": False,
                                "message": f"数据源返回服务器错误: HTTP {response.status_code}",
                                "response_time": response_time,
                                "details": None
                            }
                    except Exception as e:
                        return {
                            "success": False,
                            "message": f"连接失败: {str(e)}",
                            "response_time": time.time() - start_time,
                            "details": None
                        }
                else:
                    return {
                        "success": False,
                        "message": f"不支持的数据源类型: {ds_type}，且未配置端点",
                        "response_time": time.time() - start_time,
                        "details": None
                    }

        except Exception as e:
            response_time = time.time() - start_time
            logger.error(f"❌ 测试数据源配置失败: {e}")
            return {
                "success": False,
                "message": f"连接失败: {str(e)}",
                "response_time": response_time,
                "details": None
            }
