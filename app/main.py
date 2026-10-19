"""
TradingAgents-CN v2.1.0 FastAPI Backend
主应用程序入口

Copyright (c) 2024-2026 hsliuping. All rights reserved.
版权所有 (c) 2024-2026 hsliuping。保留所有权利。

This software is proprietary and confidential. Unauthorized copying, distribution,
or use of this software, via any medium, is strictly prohibited.
本软件为专有和机密软件。严禁通过任何媒介未经授权复制、分发或使用本软件。

For commercial licensing, please contact: hsliup@163.com
商业许可咨询，请联系：hsliup@163.com
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
import uvicorn
import logging
import time
from datetime import datetime
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path

from app.core.config import settings
from app.core.database import init_db, close_db
from app.core.logging_config import setup_logging
from app.router_registry import register_routers
from app.core.builtin_scheduler import start_builtin_scheduler
# 港股和美股改为按需获取+缓存模式，不再需要定时同步任务
# from app.worker.hk_sync_service import ...
# from app.worker.us_sync_service import ...
from app.middleware.operation_log_middleware import OperationLogMiddleware
from app.services.quotes_ingestion_service import QuotesIngestionService
from app.core.startup_summary import _print_config_summary


def get_version() -> str:
    """从 VERSION 文件读取版本号"""
    try:
        version_file = Path(__file__).parent.parent / "VERSION"
        if version_file.exists():
            return version_file.read_text(encoding='utf-8').strip()
    except Exception:
        pass
    return "2.1.0"  # 默认版本号


def get_build_info() -> dict:
    """从 BUILD_INFO 文件读取构建信息"""
    try:
        build_info_file = Path(__file__).parent.parent / "BUILD_INFO"
        if build_info_file.exists():
            import json
            return json.loads(build_info_file.read_text(encoding='utf-8'))
    except Exception as e:
        logging.debug(f"Failed to read BUILD_INFO: {e}")
    return {}




@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化
    setup_logging()
    logger = logging.getLogger("app.main")

    # 验证启动配置
    try:
        from app.core.startup_validator import validate_startup_config
        validate_startup_config()
    except Exception as e:
        logger.error(f"配置验证失败: {e}")
        raise

    await init_db()

    # 执行数据库迁移
    try:
        from app.core.database import get_mongo_db as _get_db
        from migrations.runner import MigrationRunner

        _mig_db = _get_db()
        _runner = MigrationRunner(_mig_db)
        _results = await _runner.run_pending()
        if _results:
            _ok = sum(1 for r in _results if r["status"] == "success")
            _fail = sum(1 for r in _results if r["status"] == "failed")
            logger.info(f"📦 数据库迁移完成: {_ok} 成功, {_fail} 失败")
        else:
            logger.info("📦 数据库已是最新版本")
    except Exception as e:
        logger.error(f"❌ 数据库迁移失败: {e}")
        # 迁移失败不阻止启动，但记录错误
        # 如果需要强制要求迁移成功才启动，取消下一行注释：
        # raise

    # 首次启动引导：空库时依据 ADMIN_DEFAULT_PASSWORD 创建初始管理员（社区版无注册接口）
    try:
        from app.services.user_service import bootstrap_first_admin
        await bootstrap_first_admin()
    except Exception as e:
        logger.error(f"❌ 初始管理员引导异常: {e}")

    # 首次启动自举：导入社区版最小系统配置（幂等，集合非空则跳过）
    # 须在 ensure_system_prompt_templates 之前执行，否则 prompt_templates 被后者占位后本包跳过
    try:
        from app.services.community_bootstrap_service import import_bootstrap_config
        await import_bootstrap_config()
    except Exception as e:
        logger.error(f"❌ 启动配置导入失败（不影响服务启动）: {e}", exc_info=True)

    # 本机 Codex 模型：有 CLI 才注册，不覆盖用户原有模型与默认选择。
    try:
        from app.core.database import get_mongo_db
        from app.services.codex_config_service import ensure_codex_config
        await ensure_codex_config(get_mongo_db())
    except Exception:
        logger.exception("本机 Codex 模型注册失败（不影响服务启动）")

    # 初始化自定义工具
    try:
        from app.core.database import get_mongo_db
        from core.tools.custom_tool import CustomToolDefinition, register_custom_tool
        
        db = get_mongo_db()
        cursor = db.custom_tools.find()
        async for doc in cursor:
            try:
                # 移除 _id 字段以符合 Pydantic 模型
                if "_id" in doc:
                    del doc["_id"]
                definition = CustomToolDefinition(**doc)
                await register_custom_tool(definition)
                logger.info(f"✅ Loaded custom tool: {definition.id}")
            except Exception as e:
                logger.error(f"❌ Failed to load custom tool {doc.get('id')}: {e}")
                
        logger.info("Custom tools initialization completed")
    except Exception as e:
        logger.error(f"❌ Custom tools initialization failed: {e}")

    # 初始化 Skill：将 active/enabled 的 Skill 注册到 ToolRegistry，供 Agent 运行时调用
    try:
        from app.core.database import get_mongo_db
        from core.tools import get_tool_registry
        from core.tools.external_skill_loader import load_external_skills_to_registry
        from app.routers.skills import _register_skill_to_registry

        db = get_mongo_db()
        standard_count = 0
        async for doc in db.skills.find({"enabled": True}):
            await _register_skill_to_registry(doc)
            standard_count += 1
        external_count = await load_external_skills_to_registry(db, get_tool_registry())
        logger.info(f"✅ Skill 初始化完成，已加载 {standard_count} 个标准 Skill、{external_count} 个 AI 生成 Skill")

        # 初始化工具执行日志记录器
        from core.tools.execution_logger import get_tool_execution_logger
        get_tool_execution_logger().set_db(db)
        logger.info("✅ 工具执行日志记录器已初始化")
    except Exception as e:
        logger.error(f"❌ Skill 初始化失败: {e}", exc_info=True)

    # 恢复已启用的 MCP 服务器工具到 ToolRegistry
    # 后端重启后，MCP 工具不会自动注册，需要从 mcp_server_configs 集合恢复
    try:
        from app.pro.services.mcp_service import MCPService
        mcp_service = MCPService(db)
        await mcp_service.restore_enabled_servers()
        logger.info("✅ MCP 服务器工具恢复完成")
    except Exception as e:
        logger.error(f"❌ MCP 服务器工具恢复失败: {e}", exc_info=True)

    #  配置桥接：将统一配置写入环境变量，供 TradingAgents 核心库使用
    try:
        from app.core.config_bridge import bridge_config_to_env
        bridge_config_to_env()
    except Exception as e:
        logger.warning(f"⚠️  配置桥接失败: {e}")
        logger.warning("⚠️  TradingAgents 将使用 .env 文件中的配置")

    # 🔧 京东云合作版：首次启动时从环境变量初始化数据库配置
    try:
        from app.services.jdyun_init import init_jdyun_config
        init_jdyun_config()
    except Exception as e:
        logger.warning(f"⚠️  京东云配置初始化失败: {e}")

    # 启动期同步能力索引：工具元数据变更后，重启即可增量写入向量库。
    # 改为后台异步任务，避免 Embedding 模型加载阻塞项目启动
    async def _sync_capability_index_background():
        """后台异步同步能力索引，不阻塞启动"""
        try:
            # 延迟 5 秒，让 uvicorn 先完全启动并监听端口
            await asyncio.sleep(5)

            from app.core.database import get_mongo_db
            from app.services.capability_index_service import CapabilityIndexService

            result = await CapabilityIndexService(get_mongo_db()).ensure_index()
            logger.info(
                "✅ 能力索引同步完成: %s %d/%d backend=%s",
                result.sync_mode,
                result.indexed_documents,
                result.total_documents,
                result.vector_backend,
            )

            # 索引跳过时，向所有管理员发送系统通知
            if result.sync_mode.startswith("skipped_"):
                try:
                    from app.services.user_service import UserService
                    from app.services.notifications_service import get_notifications_service
                    from app.models.notification import NotificationCreate

                    user_svc = UserService()
                    users = await user_svc.list_users(limit=100)
                    notif_svc = get_notifications_service()

                    skip_reason = "Embedding API 不可用" if "embedding" in result.sync_mode else "未配置 Embedding"
                    for u in users:
                        await notif_svc.create_and_publish(NotificationCreate(
                            user_id=u.id,
                            type="system",
                            title="能力索引未构建",
                            content=f"系统启动时能力索引构建被跳过（{skip_reason}）。请先在系统设置中配置有效的 Embedding API Key，然后在「系统设置 -> 工具能力索引」中手动重建索引。",
                            source="capability_index",
                            severity="warning",
                        ))
                    logger.info("📢 已向 %d 位用户发送能力索引跳过通知", len(users))
                except Exception as ne:
                    logger.warning(f"⚠️ 发送能力索引通知失败: {ne}")
        except Exception as e:
            logger.error(f"❌ 能力索引同步失败: {e}", exc_info=True)

    # 创建后台任务，不阻塞启动
    asyncio.create_task(_sync_capability_index_background())
    logger.info("📋 能力索引同步已在后台启动")

    # 启动期同步用户手册向量索引：手册内容变更后重启即自动重建，
    # 让使用问答机器人能基于向量语义检索回答"怎么做分析"等自然语言问题
    async def _sync_user_manual_index_background():
        """后台异步构建用户手册向量索引，不阻塞启动"""
        try:
            from core.knowledge.user_manual_manager import UserManualManager
            from app.core.database import get_mongo_db

            manager = UserManualManager(db=get_mongo_db())
            if not manager.available:
                logger.warning(
                    "⚠️ [用户手册索引] 向量库/Embedding 不可用，跳过构建。"
                    "使用问答机器人将回退到关键字匹配模式。"
                )
                return

            # 延迟 3 秒，让 uvicorn 先完全启动并监听端口
            await asyncio.sleep(3)

            # 在独立线程中执行同步索引，避免阻塞事件循环
            # ensure_indexed 是同步方法（含大量 embedding 请求），
            # 直接在协程中调用会阻塞 uvicorn 监听端口
            result = await asyncio.to_thread(manager.ensure_indexed, force=False)
            if result["rebuilt"]:
                logger.info(
                    "✅ 用户手册索引构建完成: %d 个切片，来源 %d 份手册，签名=%s",
                    result["sections_count"],
                    result["manuals_count"],
                    result["signature"][:8],
                )
            elif result["skipped"]:
                logger.info(
                    "📋 用户手册索引跳过重建（签名一致），现有 %d 个切片",
                    result["sections_count"],
                )
            else:
                logger.warning(
                    "⚠️ 用户手册索引构建失败: %s",
                    result.get("error", "未知原因"),
                )
        except Exception as e:
            logger.error(f"❌ 用户手册索引同步失败: {e}", exc_info=True)

    asyncio.create_task(_sync_user_manual_index_background())
    logger.info("📖 用户手册索引同步已在后台启动")

    # 启动期同步系统默认提示词模板：将代码中的 fallback 提示词写入数据库统一管理
    try:
        from app.services.system_prompt_seed_service import ensure_system_prompt_templates
        await ensure_system_prompt_templates()
        logger.info("✅ 系统默认提示词模板同步完成")
    except Exception as e:
        logger.error(f"❌ 系统默认提示词模板同步失败: {e}", exc_info=True)

    # Apply dynamic settings (log_level, enable_monitoring) from ConfigProvider
    try:
        from app.services.config_provider import provider as config_provider  # local import to avoid early DB init issues
        eff = await config_provider.get_effective_system_settings()
        desired_level = str(eff.get("log_level", "INFO")).upper()
        setup_logging(log_level=desired_level)
        for name in ("webapi", "worker", "uvicorn", "fastapi"):
            logging.getLogger(name).setLevel(desired_level)
        try:
            from app.middleware.operation_log_middleware import set_operation_log_enabled
            set_operation_log_enabled(bool(eff.get("enable_monitoring", True)))
        except Exception:
            pass
    except Exception as e:
        logging.getLogger("webapi").warning(f"Failed to apply dynamic settings: {e}")

    # 初始化工作流注册表
    try:
        from app.services.workflow_registry import initialize_builtin_workflows
        initialize_builtin_workflows()
        logger.info("✅ 工作流注册表初始化完成")
    except Exception as e:
        logger.error(f"❌ 工作流注册表初始化失败: {e}")

    # 显示配置摘要
    logger.info("⏳ 开始显示配置摘要")
    await _print_config_summary(logger)
    logger.info("✅ 配置摘要显示完成")

    # 🔥 启动线程池 Worker（在 Backend 进程内处理队列任务）
    try:
        logger.info("⏳ 开始启动线程池 Worker")
        from app.services.thread_worker import start_thread_worker
        max_workers = getattr(settings, 'WORKER_MAX_CONCURRENT', 3)
        await asyncio.wait_for(start_thread_worker(max_workers=max_workers), timeout=20.0)
        logger.info(f"✅ 线程池 Worker 已启动 (max_workers={max_workers})")
        logger.info("✅ 线程池 Worker 启动阶段完成")
    except Exception as e:
        logger.error(f"❌ 线程池 Worker 启动失败: {e}", exc_info=True)
        # 不阻止 Backend 启动，只是队列任务不会被处理

    logger.info("TradingAgents FastAPI backend started")

    # 初始化交易日历缓存（确定今日/最近一个真实交易日，供 JIT 数据新鲜度判断使用）
    try:
        from app.services.trading_calendar_service import get_trading_calendar_service
        await get_trading_calendar_service().warm_up()
    except Exception as e:
        logger.warning(f"⚠️ 交易日历缓存初始化失败（不影响启动）: {e}")

    # 启动期：若需要在休市时补充上一交易日收盘快照
    if settings.QUOTES_BACKFILL_ON_STARTUP:
        try:
            qi = QuotesIngestionService()
            await qi.ensure_indexes()
            await qi.backfill_last_close_snapshot_if_needed()
        except Exception as e:
            logger.warning(f"Startup backfill failed (ignored): {e}")

    # 启动每日定时任务：可配置
    scheduler = await start_builtin_scheduler(logger)

    try:
        yield
    finally:
        # 关闭时清理

        # 🔥 关闭统一线程池同步服务
        try:
            from app.worker.unified_thread_pool_sync_service import get_unified_thread_pool_sync_service
            unified_sync_service = await get_unified_thread_pool_sync_service()
            unified_sync_service.shutdown(timeout=30.0)
            logger.info("🛑 统一线程池同步服务已停止")
        except Exception as e:
            logger.warning(f"统一线程池同步服务关闭错误: {e}")

        # 🔥 停止财务数据同步服务的线程池
        try:
            from app.worker.financial_data_sync_service import get_financial_sync_service
            financial_sync_service = await get_financial_sync_service()
            if financial_sync_service is not None:
                financial_sync_service.shutdown(timeout=30.0)
                logger.info("🛑 财务数据同步服务线程池已停止")
        except Exception as e:
            logger.warning(f"财务数据同步服务停止错误: {e}")

        # 🔥 停止线程池 Worker
        try:
            from app.services.thread_worker import stop_thread_worker
            await stop_thread_worker()
            logger.info("🛑 线程池 Worker 已停止")
        except Exception as e:
            logger.warning(f"线程池 Worker 停止错误: {e}")

        if scheduler:
            try:
                # 等待正在执行的任务收尾，避免 job 仍在访问 Motor 时数据库/事件循环已被关闭。
                scheduler.shutdown(wait=True)
                logger.info("🛑 Scheduler stopped")
            except Exception as e:
                logger.warning(f"Scheduler shutdown error: {e}")

        try:
            stop_gain_alert_service.stop()
        except Exception as e:
            logger.warning(f"StopGainAlertService cleanup error: {e}")

        # 关闭 UserService MongoDB 连接
        try:
            from app.services.user_service import user_service
            user_service.close()
        except Exception as e:
            logger.warning(f"UserService cleanup error: {e}")

        await close_db()
        logger.info("TradingAgents FastAPI backend stopped")


# 创建FastAPI应用
app = FastAPI(
    title="TradingAgents-CN API",
    description="股票分析与批量队列系统 API",
    version=get_version(),
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    lifespan=lifespan,
    redirect_slashes=False,
)

# 安全中间件
if not settings.DEBUG:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.ALLOWED_HOSTS
    )

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Session 中间件（用于 Web 前端的 Cookie 认证）
# 注意：必须在 CORS 中间件之后添加
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.JWT_SECRET,  # 使用相同的密钥
    session_cookie="tradingagents_session",  # Cookie 名称
    max_age=3600,  # 1 小时过期
    same_site="lax",  # CSRF 保护
    https_only=False if settings.DEBUG else True,  # 生产环境使用 HTTPS
)

# 操作日志中间件
app.add_middleware(OperationLogMiddleware)


# 请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()

    # 跳过健康检查和静态文件请求的日志
    if request.url.path in ["/health", "/favicon.ico"] or request.url.path.startswith("/static"):
        response = await call_next(request)
        return response

    # 使用webapi logger记录请求
    logger = logging.getLogger("webapi")
    
    # 对于 license/status 接口，记录更详细的信息
    if request.url.path == "/api/license/status":
        auth_header = request.headers.get("authorization", "无")
        # 🔥 安全地访问 session（可能不存在或 SessionMiddleware 未安装）
        session_id = "无"
        try:
            if "session" in request.scope:
                session_id = request.session.get("user_id", "无")
        except (AssertionError, AttributeError, KeyError):
            session_id = "无"
        logger.info(f"🔄 {request.method} {request.url.path} - 开始处理 (Authorization: {auth_header[:20] if len(auth_header) > 20 else auth_header}..., Session: {session_id})")
    else:
        logger.info(f"🔄 {request.method} {request.url.path} - 开始处理")

    response = await call_next(request)
    process_time = time.time() - start_time

    # 记录请求完成
    status_emoji = "✅" if response.status_code < 400 else "❌"
    if request.url.path == "/api/license/status":
        logger.info(f"{status_emoji} {request.method} {request.url.path} - 状态: {response.status_code} - 耗时: {process_time:.3f}s (如果是404，可能是路由未匹配或认证失败)")
    else:
        logger.info(f"{status_emoji} {request.method} {request.url.path} - 状态: {response.status_code} - 耗时: {process_time:.3f}s")

    return response


# 全局异常处理
# 请求ID/Trace-ID 中间件（需作为最外层，放在函数式中间件之后）
from app.middleware.request_id import RequestIDMiddleware
app.add_middleware(RequestIDMiddleware)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "Internal server error occurred",
                "request_id": getattr(request.state, "request_id", None)
            }
        }
    )


# 测试端点 - 验证中间件是否工作
@app.get("/api/test-log")
async def test_log():
    """测试日志中间件是否工作"""
    print("🧪 测试端点被调用 - 这条消息应该出现在控制台")
    return {"message": "测试成功", "timestamp": time.time()}


# =============================================================================
# 路由注册
# v3.6.0 社区版开源分离（M1 收敛改造）：全部路由注册收敛至 app/router_registry.py，
# COMMUNITY_ROUTERS 与 PRO_ROUTERS 分列，社区版发布时整体剔除 PRO_ROUTERS 段。
# 见 docs/05-design/v3.0/v3.6.0-community-edition-separation-plan.md §7.1
# =============================================================================
register_routers(app)

logger = logging.getLogger("app.main")


@app.get("/api/system/info", tags=["system"])
async def get_system_info():
    """获取系统版本信息"""
    version = get_version()
    build_info = get_build_info()

    # 如果BUILD_INFO存在，使用其中的信息
    if build_info:
        return {
            "version": build_info.get("version", version),
            "full_version": build_info.get("full_version", version),
            "build_timestamp": build_info.get("build_timestamp"),
            "build_date": build_info.get("build_date"),
            "build_unix": build_info.get("build_unix"),
            "git_commit": build_info.get("git_commit"),
            "build_type": build_info.get("build_type"),
        }

    # 否则只返回主版本号
    return {
        "version": version,
        "full_version": version,
        "build_timestamp": None,
        "build_date": None,
        "build_unix": None,
        "git_commit": None,
        "build_type": None,
    }


@app.get("/")
async def root():
    """根路径，返回API信息"""
    print("🏠 根路径被访问")
    return {
        "name": "TradingAgents-CN API",
        "version": get_version(),
        "status": "running",
        "docs_url": "/docs" if settings.DEBUG else None
    }


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
        reload_dirs=["app"] if settings.DEBUG else None,
        reload_excludes=[
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            ".git",
            ".pytest_cache",
            "*.log",
            "*.tmp"
        ] if settings.DEBUG else None,
        reload_includes=["*.py"] if settings.DEBUG else None
    )
