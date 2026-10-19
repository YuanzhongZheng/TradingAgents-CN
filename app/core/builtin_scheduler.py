"""内置数据同步定时任务。"""
import asyncio
import logging
from datetime import datetime

from app.core.config import settings
from app.services.multi_source_basics_sync_service import MultiSourceBasicsSyncService
from app.services.quotes_ingestion_service import QuotesIngestionService
from app.services.scheduler_service import set_scheduler_instance
from app.worker.tushare_sync_service import (
    run_tushare_realtime_quotes_hourly,
    run_tushare_basic_info_sync,
    run_tushare_historical_sync,
    run_tushare_financial_sync,
    run_tushare_status_check,
)
from app.worker.akshare_sync_service import (
    run_akshare_basic_info_sync,
    run_akshare_quotes_sync,
    run_akshare_historical_sync,
    run_akshare_financial_sync,
    run_akshare_status_check,
)
from app.worker.baostock_sync_service import (
    run_baostock_basic_info_sync,
    run_baostock_daily_quotes_sync,
    run_baostock_historical_sync,
    run_baostock_status_check,
)
from app.worker.etf_sync_service import run_etf_sync
from app.worker.favorites_sync_task import run_favorites_data_sync
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


async def start_builtin_scheduler(logger: logging.Logger) -> AsyncIOScheduler:
    scheduler: AsyncIOScheduler | None = None
    try:
        from croniter import croniter
    except Exception:
        croniter = None  # 可选依赖
    try:
        # 🔥 配置 APScheduler：同一任务不允许并发执行
        # max_instances=1 确保同一任务同时只能有一个实例在执行
        # 如果任务正在执行，新的触发会被跳过
        from apscheduler.executors.asyncio import AsyncIOExecutor
    
        # 创建异步执行器
        executors = {
            'default': AsyncIOExecutor()
        }
    
        # 配置 job_defaults：同一任务不允许并发执行
        job_defaults = {
            'coalesce': True,  # 合并错过的任务（如果任务正在执行，跳过新的触发）
            'max_instances': 1,  # 同一任务最多1个实例（不允许并发）
            'misfire_grace_time': 30  # 错过执行时间后30秒内仍可执行
        }
    
        scheduler = AsyncIOScheduler(
            timezone=settings.TIMEZONE,
            executors=executors,
            job_defaults=job_defaults
        )

        # 使用多数据源同步服务（支持自动切换）
        multi_source_service = MultiSourceBasicsSyncService()

        # 🔥 从数据库读取数据源优先级配置（而非硬编码）
        # 排除 local/mongodb 等缓存层，只使用真实 API 数据源
        async def get_preferred_sources_from_db():
            """从数据库读取启用的 API 数据源优先级"""
            try:
                from app.core.data_source_priority import get_enabled_data_sources_async
                all_sources = await get_enabled_data_sources_async("a_shares")
                # 排除缓存层，只保留真实 API 数据源
                api_sources = [s for s in all_sources if s not in ("local", "mongodb", "local_file")]
                # 启动期股票基础信息同步不使用 QMT 作为首选源，避免 QMT 全市场列表拖慢启动。
                filtered_sources = [s for s in api_sources if s != "qmt"]
                if api_sources:
                    logger.info(f"📊 股票基础信息同步原始数据源优先级（来自数据库）: {' > '.join(api_sources)}")
                    if filtered_sources != api_sources:
                        logger.info(f"🧹 股票基础信息同步已排除 QMT 首选优先级，实际顺序: {' > '.join(filtered_sources)}")
                    return filtered_sources or None
                else:
                    logger.warning("⚠️ 数据库中没有启用的 API 数据源，使用默认配置")
                    return None
            except Exception as e:
                logger.error(f"❌ 读取数据源优先级失败: {e}，使用默认配置")
                return None

        # 立即在启动后尝试一次（不阻塞）
        async def run_sync_with_sources():
            preferred_sources = await get_preferred_sources_from_db()
            await multi_source_service.run_full_sync(force=False, preferred_sources=preferred_sources)

        asyncio.create_task(run_sync_with_sources())

        # 配置调度：优先使用 CRON，其次使用 HH:MM
        # 🔥 定时任务也需要从数据库读取最新的数据源优先级
        async def scheduled_sync_with_db_priority():
            """定时任务：每次执行时从数据库读取最新的数据源优先级"""
            preferred_sources = await get_preferred_sources_from_db()
            await multi_source_service.run_full_sync(force=False, preferred_sources=preferred_sources)

        if settings.SYNC_STOCK_BASICS_ENABLED:
            if settings.SYNC_STOCK_BASICS_CRON:
                # 如果提供了cron表达式
                scheduler.add_job(
                    scheduled_sync_with_db_priority,
                    CronTrigger.from_crontab(settings.SYNC_STOCK_BASICS_CRON, timezone=settings.TIMEZONE),
                    id="basics_sync_service",
                    name="股票基础信息同步（多数据源）"
                )
                logger.info(f"📅 Stock basics sync scheduled by CRON: {settings.SYNC_STOCK_BASICS_CRON} ({settings.TIMEZONE})")
            else:
                hh, mm = (settings.SYNC_STOCK_BASICS_TIME or "06:30").split(":")
                scheduler.add_job(
                    scheduled_sync_with_db_priority,
                    CronTrigger(hour=int(hh), minute=int(mm), timezone=settings.TIMEZONE),
                    id="basics_sync_service",
                    name="股票基础信息同步（多数据源）"
                )
                logger.info(f"📅 Stock basics sync scheduled daily at {settings.SYNC_STOCK_BASICS_TIME} ({settings.TIMEZONE})")

        # 实时行情入库任务（每N秒），内部自判交易时段
        if settings.QUOTES_INGEST_ENABLED:
            quotes_ingestion = QuotesIngestionService()
            await quotes_ingestion.ensure_indexes()
            scheduler.add_job(
                quotes_ingestion.run_once,  # coroutine function; AsyncIOScheduler will await it
                IntervalTrigger(seconds=settings.QUOTES_INGEST_INTERVAL_SECONDS, timezone=settings.TIMEZONE),
                id="quotes_ingestion_service",
                name="实时行情入库服务"
            )
            logger.info(f"⏱ 实时行情入库任务已启动: 每 {settings.QUOTES_INGEST_INTERVAL_SECONDS}s")

        # Tushare统一数据同步任务配置
        logger.info("🔄 配置Tushare统一数据同步任务...")

        # 基础信息同步任务
        scheduler.add_job(
            run_tushare_basic_info_sync,
            CronTrigger.from_crontab(settings.TUSHARE_BASIC_INFO_SYNC_CRON, timezone=settings.TIMEZONE),
            id="tushare_basic_info_sync",
            name="股票基础信息同步（Tushare）",
            kwargs={"force_update": False}
        )
        if not (settings.TUSHARE_UNIFIED_ENABLED and settings.TUSHARE_BASIC_INFO_SYNC_ENABLED):
            scheduler.pause_job("tushare_basic_info_sync")
            logger.info(f"⏸️ Tushare基础信息同步已添加但暂停: {settings.TUSHARE_BASIC_INFO_SYNC_CRON}")
        else:
            logger.info(f"📅 Tushare基础信息同步已配置: {settings.TUSHARE_BASIC_INFO_SYNC_CRON}")

        # 🔥 已禁用旧的实时行情同步任务，统一由 run_tushare_realtime_quotes_hourly 管理
        # 避免与每小时31分的定时任务冲突（免费用户每小时只能调用一次）
        # scheduler.add_job(
        #     run_tushare_quotes_sync,
        #     CronTrigger.from_crontab(settings.TUSHARE_QUOTES_SYNC_CRON, timezone=settings.TIMEZONE),
        #     id="tushare_quotes_sync",
        #     name="实时行情同步（Tushare）"
        # )
        logger.info(f"⏸️ Tushare实时行情同步任务已禁用，统一由每小时31分的定时任务管理")

        # Tushare实时行情每小时同步任务（免费用户每小时只能调用一次）
        scheduler.add_job(
            run_tushare_realtime_quotes_hourly,
            CronTrigger.from_crontab(settings.TUSHARE_REALTIME_QUOTES_HOURLY_CRON, timezone=settings.TIMEZONE),
            id="tushare_realtime_quotes_hourly",
            name="实时行情每小时同步（Tushare，免费用户每小时31分）"
        )
        if not (settings.TUSHARE_UNIFIED_ENABLED and settings.TUSHARE_REALTIME_QUOTES_HOURLY_ENABLED):
            scheduler.pause_job("tushare_realtime_quotes_hourly")
            logger.info(f"⏸️ Tushare实时行情每小时同步已添加但暂停: {settings.TUSHARE_REALTIME_QUOTES_HOURLY_CRON}")
        else:
            logger.info(f"📈 Tushare实时行情每小时同步已配置: {settings.TUSHARE_REALTIME_QUOTES_HOURLY_CRON} (每小时31分执行，15:00后自动保存到历史数据)")

        # 历史数据同步任务
        scheduler.add_job(
            run_tushare_historical_sync,
            CronTrigger.from_crontab(settings.TUSHARE_HISTORICAL_SYNC_CRON, timezone=settings.TIMEZONE),
            id="tushare_historical_sync",
            name="历史数据同步（Tushare）",
            kwargs={"incremental": True}
        )
        if not (settings.TUSHARE_UNIFIED_ENABLED and settings.TUSHARE_HISTORICAL_SYNC_ENABLED):
            scheduler.pause_job("tushare_historical_sync")
            logger.info(f"⏸️ Tushare历史数据同步已添加但暂停: {settings.TUSHARE_HISTORICAL_SYNC_CRON}")
        else:
            logger.info(f"📊 Tushare历史数据同步已配置: {settings.TUSHARE_HISTORICAL_SYNC_CRON}")

        # 财务数据同步任务
        scheduler.add_job(
            run_tushare_financial_sync,
            CronTrigger.from_crontab(settings.TUSHARE_FINANCIAL_SYNC_CRON, timezone=settings.TIMEZONE),
            id="tushare_financial_sync",
            name="财务数据同步（Tushare）"
        )
        if not (settings.TUSHARE_UNIFIED_ENABLED and settings.TUSHARE_FINANCIAL_SYNC_ENABLED):
            scheduler.pause_job("tushare_financial_sync")
            logger.info(f"⏸️ Tushare财务数据同步已添加但暂停: {settings.TUSHARE_FINANCIAL_SYNC_CRON}")
        else:
            logger.info(f"💰 Tushare财务数据同步已配置: {settings.TUSHARE_FINANCIAL_SYNC_CRON}")

        # 状态检查任务
        scheduler.add_job(
            run_tushare_status_check,
            CronTrigger.from_crontab(settings.TUSHARE_STATUS_CHECK_CRON, timezone=settings.TIMEZONE),
            id="tushare_status_check",
            name="数据源状态检查（Tushare）"
        )
        if not (settings.TUSHARE_UNIFIED_ENABLED and settings.TUSHARE_STATUS_CHECK_ENABLED):
            scheduler.pause_job("tushare_status_check")
            logger.info(f"⏸️ Tushare状态检查已添加但暂停: {settings.TUSHARE_STATUS_CHECK_CRON}")
        else:
            logger.info(f"🔍 Tushare状态检查已配置: {settings.TUSHARE_STATUS_CHECK_CRON}")

        # AKShare统一数据同步任务配置
        logger.info("🔄 配置AKShare统一数据同步任务...")

        # 基础信息同步任务
        scheduler.add_job(
            run_akshare_basic_info_sync,
            CronTrigger.from_crontab(settings.AKSHARE_BASIC_INFO_SYNC_CRON, timezone=settings.TIMEZONE),
            id="akshare_basic_info_sync",
            name="股票基础信息同步（AKShare）",
            kwargs={"force_update": False}
        )
        if not (settings.AKSHARE_UNIFIED_ENABLED and settings.AKSHARE_BASIC_INFO_SYNC_ENABLED):
            scheduler.pause_job("akshare_basic_info_sync")
            logger.info(f"⏸️ AKShare基础信息同步已添加但暂停: {settings.AKSHARE_BASIC_INFO_SYNC_CRON}")
        else:
            logger.info(f"📅 AKShare基础信息同步已配置: {settings.AKSHARE_BASIC_INFO_SYNC_CRON}")

        # 实时行情同步任务
        scheduler.add_job(
            run_akshare_quotes_sync,
            CronTrigger.from_crontab(settings.AKSHARE_QUOTES_SYNC_CRON, timezone=settings.TIMEZONE),
            id="akshare_quotes_sync",
            name="实时行情同步（AKShare）"
        )
        if not (settings.AKSHARE_UNIFIED_ENABLED and settings.AKSHARE_QUOTES_SYNC_ENABLED):
            scheduler.pause_job("akshare_quotes_sync")
            logger.info(f"⏸️ AKShare行情同步已添加但暂停: {settings.AKSHARE_QUOTES_SYNC_CRON}")
        else:
            logger.info(f"📈 AKShare行情同步已配置: {settings.AKSHARE_QUOTES_SYNC_CRON}")

        # 历史数据同步任务
        scheduler.add_job(
            run_akshare_historical_sync,
            CronTrigger.from_crontab(settings.AKSHARE_HISTORICAL_SYNC_CRON, timezone=settings.TIMEZONE),
            id="akshare_historical_sync",
            name="历史数据同步（AKShare）",
            kwargs={"incremental": True}
        )
        if not (settings.AKSHARE_UNIFIED_ENABLED and settings.AKSHARE_HISTORICAL_SYNC_ENABLED):
            scheduler.pause_job("akshare_historical_sync")
            logger.info(f"⏸️ AKShare历史数据同步已添加但暂停: {settings.AKSHARE_HISTORICAL_SYNC_CRON}")
        else:
            logger.info(f"📊 AKShare历史数据同步已配置: {settings.AKSHARE_HISTORICAL_SYNC_CRON}")

        # 财务数据同步任务
        scheduler.add_job(
            run_akshare_financial_sync,
            CronTrigger.from_crontab(settings.AKSHARE_FINANCIAL_SYNC_CRON, timezone=settings.TIMEZONE),
            id="akshare_financial_sync",
            name="财务数据同步（AKShare）"
        )
        if not (settings.AKSHARE_UNIFIED_ENABLED and settings.AKSHARE_FINANCIAL_SYNC_ENABLED):
            scheduler.pause_job("akshare_financial_sync")
            logger.info(f"⏸️ AKShare财务数据同步已添加但暂停: {settings.AKSHARE_FINANCIAL_SYNC_CRON}")
        else:
            logger.info(f"💰 AKShare财务数据同步已配置: {settings.AKSHARE_FINANCIAL_SYNC_CRON}")

        # 状态检查任务
        scheduler.add_job(
            run_akshare_status_check,
            CronTrigger.from_crontab(settings.AKSHARE_STATUS_CHECK_CRON, timezone=settings.TIMEZONE),
            id="akshare_status_check",
            name="数据源状态检查（AKShare）"
        )
        if not (settings.AKSHARE_UNIFIED_ENABLED and settings.AKSHARE_STATUS_CHECK_ENABLED):
            scheduler.pause_job("akshare_status_check")
            logger.info(f"⏸️ AKShare状态检查已添加但暂停: {settings.AKSHARE_STATUS_CHECK_CRON}")
        else:
            logger.info(f"🔍 AKShare状态检查已配置: {settings.AKSHARE_STATUS_CHECK_CRON}")

        # BaoStock统一数据同步任务配置
        logger.info("🔄 配置BaoStock统一数据同步任务...")

        # 基础信息同步任务
        scheduler.add_job(
            run_baostock_basic_info_sync,
            CronTrigger.from_crontab(settings.BAOSTOCK_BASIC_INFO_SYNC_CRON, timezone=settings.TIMEZONE),
            id="baostock_basic_info_sync",
            name="股票基础信息同步（BaoStock）"
        )
        if not (settings.BAOSTOCK_UNIFIED_ENABLED and settings.BAOSTOCK_BASIC_INFO_SYNC_ENABLED):
            scheduler.pause_job("baostock_basic_info_sync")
            logger.info(f"⏸️ BaoStock基础信息同步已添加但暂停: {settings.BAOSTOCK_BASIC_INFO_SYNC_CRON}")
        else:
            logger.info(f"📋 BaoStock基础信息同步已配置: {settings.BAOSTOCK_BASIC_INFO_SYNC_CRON}")

        # 日K线同步任务（注意：BaoStock不支持实时行情）
        scheduler.add_job(
            run_baostock_daily_quotes_sync,
            CronTrigger.from_crontab(settings.BAOSTOCK_DAILY_QUOTES_SYNC_CRON, timezone=settings.TIMEZONE),
            id="baostock_daily_quotes_sync",
            name="日K线数据同步（BaoStock）"
        )
        if not (settings.BAOSTOCK_UNIFIED_ENABLED and settings.BAOSTOCK_DAILY_QUOTES_SYNC_ENABLED):
            scheduler.pause_job("baostock_daily_quotes_sync")
            logger.info(f"⏸️ BaoStock日K线同步已添加但暂停: {settings.BAOSTOCK_DAILY_QUOTES_SYNC_CRON}")
        else:
            logger.info(f"📈 BaoStock日K线同步已配置: {settings.BAOSTOCK_DAILY_QUOTES_SYNC_CRON} (注意：BaoStock不支持实时行情)")

        # 历史数据同步任务
        scheduler.add_job(
            run_baostock_historical_sync,
            CronTrigger.from_crontab(settings.BAOSTOCK_HISTORICAL_SYNC_CRON, timezone=settings.TIMEZONE),
            id="baostock_historical_sync",
            name="历史数据同步（BaoStock）"
        )
        if not (settings.BAOSTOCK_UNIFIED_ENABLED and settings.BAOSTOCK_HISTORICAL_SYNC_ENABLED):
            scheduler.pause_job("baostock_historical_sync")
            logger.info(f"⏸️ BaoStock历史数据同步已添加但暂停: {settings.BAOSTOCK_HISTORICAL_SYNC_CRON}")
        else:
            logger.info(f"📊 BaoStock历史数据同步已配置: {settings.BAOSTOCK_HISTORICAL_SYNC_CRON}")

        # 状态检查任务
        scheduler.add_job(
            run_baostock_status_check,
            CronTrigger.from_crontab(settings.BAOSTOCK_STATUS_CHECK_CRON, timezone=settings.TIMEZONE),
            id="baostock_status_check",
            name="数据源状态检查（BaoStock）"
        )
        if not (settings.BAOSTOCK_UNIFIED_ENABLED and settings.BAOSTOCK_STATUS_CHECK_ENABLED):
            scheduler.pause_job("baostock_status_check")
            logger.info(f"⏸️ BaoStock状态检查已添加但暂停: {settings.BAOSTOCK_STATUS_CHECK_CRON}")
        else:
            logger.info(f"🔍 BaoStock状态检查已配置: {settings.BAOSTOCK_STATUS_CHECK_CRON}")

        # ETF 数据同步任务配置
        logger.info("🔄 配置ETF数据同步任务...")

        scheduler.add_job(
            run_etf_sync,
            CronTrigger.from_crontab(settings.ETF_SYNC_CRON, timezone=settings.TIMEZONE),
            id="etf_sync",
            name="ETF 数据同步",
            kwargs={"force_update": False, "days": settings.ETF_SYNC_DAYS, "job_id": "etf_sync"}
        )
        if not settings.ETF_SYNC_ENABLED:
            scheduler.pause_job("etf_sync")
            logger.info(f"⏸️ ETF 数据同步已添加但暂停: {settings.ETF_SYNC_CRON}")
        else:
            logger.info(
                f"📅 ETF 数据同步已配置: {settings.ETF_SYNC_CRON}，"
                f"首次全量补齐、后续按最新入库日期增量同步；"
                f"强制回溯窗口为 {settings.ETF_SYNC_DAYS} 天"
            )

        # 股票关注列表A股数据同步任务配置
        logger.info("🔄 配置股票关注列表A股数据同步任务...")
    
        scheduler.add_job(
            run_favorites_data_sync,
            CronTrigger.from_crontab(settings.FAVORITES_DATA_SYNC_CRON, timezone=settings.TIMEZONE),
            id="favorites_data_sync",
            name="股票关注列表A股数据同步（历史数据+财务数据）"
        )
        if not settings.FAVORITES_DATA_SYNC_ENABLED:
            scheduler.pause_job("favorites_data_sync")
            logger.info(f"⏸️ 股票关注列表A股数据同步已添加但暂停: {settings.FAVORITES_DATA_SYNC_CRON}")
        else:
            logger.info(f"⭐ 股票关注列表A股数据同步已配置: {settings.FAVORITES_DATA_SYNC_CRON} (历史数据+财务数据)")

        # 新闻数据同步任务配置（使用AKShare同步所有股票新闻）
        logger.info("🔄 配置新闻数据同步任务...")

        from app.worker.akshare_sync_service import get_akshare_sync_service

        async def run_news_sync():
            """运行新闻同步任务 - 使用AKShare同步股票关注列表新闻"""
            try:
                logger.info("📰 开始新闻数据同步（AKShare - 仅股票关注列表）...")
                service = await get_akshare_sync_service()
                # 🔥 设置正确的 job_id，确保进度更新和状态标记使用正确的任务ID
                service._current_job_id = "news_sync"
                result = await service.sync_news_data(
                    symbols=None,  # None + favorites_only=True 表示只同步股票关注列表
                    max_news_per_stock=settings.NEWS_SYNC_MAX_PER_SOURCE,
                    favorites_only=True  # 只同步股票关注列表
                )
                logger.info(
                    f"✅ 新闻同步完成: "
                    f"处理{result['total_processed']}只股票关注列表, "
                    f"成功{result['success_count']}只, "
                    f"失败{result['error_count']}只, "
                    f"新闻总数{result['news_count']}条, "
                    f"耗时{(datetime.utcnow() - result['start_time']).total_seconds():.2f}秒"
                )
            except Exception as e:
                logger.error(f"❌ 新闻同步失败: {e}", exc_info=True)

        # ==================== 港股/美股数据配置 ====================
        # 港股和美股采用按需获取+缓存模式，不再配置定时同步任务
        logger.info("🇭🇰 港股数据采用按需获取+缓存模式")
        logger.info("🇺🇸 美股数据采用按需获取+缓存模式")

        scheduler.add_job(
            run_news_sync,
            CronTrigger.from_crontab(settings.NEWS_SYNC_CRON, timezone=settings.TIMEZONE),
            id="news_sync",
            name="新闻数据同步（AKShare - 仅股票关注列表）"
        )
        if not settings.NEWS_SYNC_ENABLED:
            scheduler.pause_job("news_sync")
            logger.info(f"⏸️ 新闻数据同步已添加但暂停: {settings.NEWS_SYNC_CRON}")
        else:
            logger.info(f"📰 新闻数据同步已配置（仅股票关注列表）: {settings.NEWS_SYNC_CRON}")

        # ==================== 股票关注列表定时分析配置 ====================
        from app.worker.watchlist_analysis_task import run_watchlist_analysis
        scheduler.add_job(
            run_watchlist_analysis,
            CronTrigger.from_crontab(settings.WATCHLIST_ANALYSIS_CRON, timezone=settings.TIMEZONE),
            id="watchlist_analysis",
            name="股票关注列表定时分析"
        )
        if not settings.WATCHLIST_ANALYSIS_ENABLED:
            scheduler.pause_job("watchlist_analysis")
            logger.info(f"⏸️ 股票关注列表定时分析已添加但暂停: {settings.WATCHLIST_ANALYSIS_CRON}")
        else:
            logger.info(f"📊 股票关注列表定时分析已配置: {settings.WATCHLIST_ANALYSIS_CRON}")

        # ==================== 交易日历缓存刷新任务 ====================
        # 每日 18:30 刷新一次：Tushare daily_basic 数据一般在收盘后（15:00）
        # 约 1~3 小时才稳定入库，18:30 之后获取当日数据才可靠。
        # 启动时已由 warm_up() 完成初始化，本任务负责当日收盘后的更新。
        async def _refresh_trading_calendar():
            try:
                from app.services.trading_calendar_service import get_trading_calendar_service
                await get_trading_calendar_service().warm_up()
                logger.info("🗓️ [TradingCalendar] 交易日历缓存已刷新（18:30 定时）")
            except Exception as _e:
                logger.warning(f"⚠️ [TradingCalendar] 定时刷新失败（不影响运行）: {_e}")

        scheduler.add_job(
            _refresh_trading_calendar,
            CronTrigger(hour=18, minute=30, timezone=settings.TIMEZONE),
            id="trading_calendar_refresh",
            name="交易日历缓存刷新（每日18:30）"
        )
        logger.info("🗓️ 交易日历缓存刷新任务已配置（每日 18:30 执行）")

        # v3.5.0 项4 / v3.6.0 项6：HITL 待确认操作过期清理（每 30 分钟执行一次）
        async def _expire_stale_hitl_operations():
            try:
                from app.core.database import get_mongo_db
                from core.hitl.checkpoint_store import HITLCheckpointService
                db = get_mongo_db()
                if db is not None:
                    svc = HITLCheckpointService(db)
                    cleaned = await svc.expire_stale()
                    if cleaned:
                        logger.info(f"[HITL·定时] 已清理 {cleaned} 条过期待确认操作")
            except Exception as _e:
                logger.warning(f"[HITL·定时] 过期清理失败: {_e}")

        scheduler.add_job(
            _expire_stale_hitl_operations,
            IntervalTrigger(minutes=30, timezone=settings.TIMEZONE),
            id="hitl_expire_stale",
            name="HITL 待确认操作过期清理（每30分钟）"
        )
        logger.info("🔄 HITL 待确认操作过期清理已配置（每 30 分钟）")

        # v3.6.0：Skill 审核过期清理（每 30 分钟执行一次）
        async def _expire_stale_skill_reviews():
            try:
                from app.core.database import get_mongo_db
                from app.pro.services.skill_review_service import SkillReviewService
                db = get_mongo_db()
                if db is not None:
                    svc = SkillReviewService(db=db)
                    cleaned = await svc.expire_stale_reviews()
                    if cleaned:
                        logger.info(f"[SkillReview·定时] 已过期 {cleaned} 条待审核 Skill 记录")
            except Exception as _e:
                logger.warning(f"[SkillReview·定时] 过期清理失败: {_e}")

        scheduler.add_job(
            _expire_stale_skill_reviews,
            IntervalTrigger(minutes=30, timezone=settings.TIMEZONE),
            id="skill_review_expire_stale",
            name="Skill 审核过期清理（每30分钟）"
        )
        logger.info("🔄 Skill 审核过期清理已配置（每 30 分钟）")

        scheduler.start()

        # 设置调度器实例到服务中，以便API可以管理任务
        set_scheduler_instance(scheduler)
        logger.info("✅ 调度器服务已初始化")
    
        # 🔥 检测服务重启导致的挂起任务
        try:
            from app.services.scheduler_service import get_scheduler_service
            scheduler_service = get_scheduler_service()  # 同步函数，不需要await
            await scheduler_service.check_suspended_tasks_on_startup()
        except Exception as e:
            logger.warning(f"⚠️ 检测挂起任务失败（不影响启动）: {e}")
    except Exception as e:
        logger.error(f"❌ 调度器启动失败: {e}", exc_info=True)
        raise  # 抛出异常，阻止应用启动
    return scheduler
