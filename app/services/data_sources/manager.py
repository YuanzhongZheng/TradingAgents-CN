"""
Data source manager that orchestrates multiple adapters with priority and optional consistency checks
"""
from typing import List, Optional, Tuple, Dict
import logging
import time
import threading
from core.market_runtime.cache import SingleFlightCache
from core.market_runtime.context import configuration_key, current_snapshot, request_timeout, normalize_date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

from .base import DataSourceAdapter
from .local_adapter import LocalAdapter
from .tushare_adapter import TushareAdapter
from .akshare_adapter import AKShareAdapter
from .baostock_adapter import BaoStockAdapter
from .qmt_adapter import QMTAdapter

logger = logging.getLogger(__name__)

# 分析读取使用前复权，和原 Tushare/AKShare/BaoStock 历史接口一致。
ANALYSIS_BAR_ADJUST = "qfq"


class DataSourceManager:
    """
    数据源管理器
    - 管理多个适配器，基于优先级排序
    - 提供 fallback 获取能力
    - 可选：一致性检查（若依赖存在）
    """

    def __init__(self):
        self._history_cache = SingleFlightCache(maxsize=128)
        self._configured_enabled_map: Dict[str, bool] = {}
        self.adapters: List[DataSourceAdapter] = [
            LocalAdapter(),      # 本地数据源（用户通过API导入）
            TushareAdapter(),
            AKShareAdapter(),
            BaoStockAdapter(),
            QMTAdapter(),        # 迅投 QMT（需 QMT 运行，新闻用 AKShare）
        ]

        # 使用统一的数据源优先级模块加载配置
        self._load_priority_from_unified_config()

        # 按优先级排序（数字越大优先级越高，所以降序排列）
        self.adapters.sort(key=lambda x: x.priority, reverse=True)

        try:
            from .data_consistency_checker import DataConsistencyChecker  # type: ignore
            self.consistency_checker = DataConsistencyChecker()
        except Exception:
            logger.warning("⚠️ 数据一致性检查器不可用")
            self.consistency_checker = None

    def _load_priority_from_unified_config(self):
        """使用统一配置模块加载数据源优先级"""
        try:
            from app.core.unified_config import UnifiedConfigManager

            config = UnifiedConfigManager()
            data_source_configs = config.get_data_source_configs()

            # 创建名称到优先级的映射
            priority_map = {}
            for ds_config in data_source_configs:
                data_source_name = ds_config.type.lower()
                priority = ds_config.priority
                enabled = ds_config.enabled

                self._configured_enabled_map[data_source_name] = bool(enabled)

                if enabled:
                    priority_map[data_source_name] = priority

            # 🔥 强制 .env 开关覆盖（.env 优先级最高，无视数据库缓存）
            self._apply_dotenv_overrides()

            # 更新各个 Adapter 的优先级
            for adapter in self.adapters:
                if adapter.name in priority_map:
                    adapter._priority = priority_map[adapter.name]
                else:
                    adapter._priority = adapter._get_default_priority()

        except Exception as e:
            logger.warning(f"⚠️ 从统一配置加载优先级失败: {e}，使用默认优先级")
            import traceback
            logger.warning(f"堆栈跟踪:\n{traceback.format_exc()}")
            # 使用默认优先级
            for adapter in self.adapters:
                adapter._priority = adapter._get_default_priority()

    def _apply_dotenv_overrides(self):
        """强制应用 .env 中的数据源开关（最高优先级）

        通过 Pydantic BaseSettings 读取 .env 中显式设置的值。
        多个 key 按优先级排列，第一个被显式设置的值生效。
        """
        try:
            from app.core.config import settings as runtime_settings

            # 按优先级排列：用户实际在 .env 中设置的 key 放前面
            env_overrides = {
                "baostock": ["BAOSTOCK_UNIFIED_ENABLED", "ENABLE_BAOSTOCK"],
                "tushare": ["ENABLE_TUSHARE"],
            }

            for adapter_name, env_keys in env_overrides.items():
                effective_key = None
                env_val = None

                for key in env_keys:
                    val = getattr(runtime_settings, key, None)

                    # 只有当值为 False（禁用）时才覆盖。
                    # True 可能是 Pydantic 默认值，不要用默认 True 去覆盖数据库的 False。
                    if val is False:
                        env_val = False
                        effective_key = key
                        break

                if effective_key:
                    old_val = self._configured_enabled_map.get(adapter_name)
                    self._configured_enabled_map[adapter_name] = env_val
                    logger.info(f"🔧 [DataSourceManager] .env 禁用 ({effective_key}): {adapter_name} enabled=False (原={old_val})")
        except Exception as e:
            logger.warning(f"⚠️ [DataSourceManager] .env 覆盖失败: {e}")

    def get_available_adapters(self) -> List[DataSourceAdapter]:
        available: List[DataSourceAdapter] = []
        for adapter in self.adapters:
            if self._configured_enabled_map.get(adapter.name) is False:
                logger.info(f"Data source {adapter.name} is disabled in unified config, skipping availability probe")
                continue

            if adapter.is_available():
                available.append(adapter)
                logger.info(
                    f"Data source {adapter.name} is available (priority: {adapter.priority})"
                )
            else:
                log_message = f"Data source {adapter.name} is not available"
                if adapter.name == "qmt":
                    logger.info(log_message)
                else:
                    logger.warning(log_message)
        return available

    def get_stock_list_with_fallback(self, preferred_sources: Optional[List[str]] = None, exclude_local: bool = False) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """
        获取股票列表，支持指定优先数据源

        Args:
            preferred_sources: 优先使用的数据源列表，例如 ['akshare', 'baostock']
                             如果为 None，则按照默认优先级顺序
            exclude_local: 是否排除本地数据源（同步操作时应设为 True）

        Returns:
            (DataFrame, source_name) 或 (None, None)
        """
        available_adapters = self.get_available_adapters()

        # 排除本地数据源（用于同步操作）
        if exclude_local:
            available_adapters = [a for a in available_adapters if a.name != 'local']
            logger.info("🚫 排除本地数据源（local），仅使用外部数据源进行同步")

        # 如果指定了优先数据源，重新排序
        if preferred_sources:
            logger.info(f"Using preferred data sources: {preferred_sources}")
            # 创建优先级映射
            priority_map = {name: idx for idx, name in enumerate(preferred_sources)}
            # 将指定的数据源排在前面，其他的保持原顺序
            preferred = [a for a in available_adapters if a.name in priority_map]
            others = [a for a in available_adapters if a.name not in priority_map]
            # 按照 preferred_sources 的顺序排序
            preferred.sort(key=lambda a: priority_map.get(a.name, 999))
            available_adapters = preferred + others
            logger.info(f"Reordered adapters: {[a.name for a in available_adapters]}")

        for adapter in available_adapters:
            try:
                adapter_start = time.perf_counter()
                logger.info(f"Trying to fetch stock list from {adapter.name}")
                df = adapter.get_stock_list()
                if df is not None and not df.empty:
                    logger.info(
                        "Stock list fetch succeeded from %s: records=%s elapsed=%.2fs",
                        adapter.name,
                        len(df),
                        time.perf_counter() - adapter_start,
                    )
                    return df, adapter.name
                logger.warning(
                    "Stock list fetch returned empty from %s after %.2fs",
                    adapter.name,
                    time.perf_counter() - adapter_start,
                )
            except Exception as e:
                logger.error(
                    "Failed to fetch stock list from %s after %.2fs: %s",
                    adapter.name,
                    time.perf_counter() - adapter_start,
                    e,
                )
                continue
        return None, None

    def get_daily_basic_with_fallback(self, trade_date: str, preferred_sources: Optional[List[str]] = None) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """
        获取每日基础数据，支持指定优先数据源

        Args:
            trade_date: 交易日期
            preferred_sources: 优先使用的数据源列表

        Returns:
            (DataFrame, source_name) 或 (None, None)
        """
        available_adapters = self.get_available_adapters()

        # 如果指定了优先数据源，重新排序
        if preferred_sources:
            priority_map = {name: idx for idx, name in enumerate(preferred_sources)}
            preferred = [a for a in available_adapters if a.name in priority_map]
            others = [a for a in available_adapters if a.name not in priority_map]
            preferred.sort(key=lambda a: priority_map.get(a.name, 999))
            available_adapters = preferred + others

        for adapter in available_adapters:
            try:
                logger.info(f"Trying to fetch daily basic data from {adapter.name}")
                df = adapter.get_daily_basic(trade_date)
                if df is not None and not df.empty:
                    return df, adapter.name
            except Exception as e:
                logger.error(f"Failed to fetch daily basic data from {adapter.name}: {e}")
                continue
        return None, None

    def find_latest_trade_date_with_fallback(self, preferred_sources: Optional[List[str]] = None) -> Optional[str]:
        """
        查找最新交易日期，支持指定优先数据源

        Args:
            preferred_sources: 优先使用的数据源列表

        Returns:
            交易日期字符串（YYYYMMDD格式）或 None
        """
        available_adapters = self.get_available_adapters()

        # 如果指定了优先数据源，重新排序
        if preferred_sources:
            priority_map = {name: idx for idx, name in enumerate(preferred_sources)}
            preferred = [a for a in available_adapters if a.name in priority_map]
            others = [a for a in available_adapters if a.name not in priority_map]
            preferred.sort(key=lambda a: priority_map.get(a.name, 999))
            available_adapters = preferred + others

        for adapter in available_adapters:
            try:
                trade_date = adapter.find_latest_trade_date()
                if trade_date:
                    return trade_date
            except Exception as e:
                logger.error(f"Failed to find trade date from {adapter.name}: {e}")
                continue
        return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    def get_realtime_quotes_with_fallback(self) -> Tuple[Optional[Dict], Optional[str]]:
        """
        获取全市场实时快照，按适配器优先级依次尝试，返回首个成功结果
        Returns: (quotes_dict, source_name)
        quotes_dict 形如 { '000001': {'close': 10.0, 'pct_chg': 1.2, 'amount': 1.2e8}, ... }
        """
        available_adapters = self.get_available_adapters()
        for adapter in available_adapters:
            try:
                logger.info(f"Trying to fetch realtime quotes from {adapter.name}")
                data = adapter.get_realtime_quotes()
                if data:
                    return data, adapter.name
            except Exception as e:
                logger.error(f"Failed to fetch realtime quotes from {adapter.name}: {e}")
                continue
        return None, None


    def get_daily_basic_with_consistency_check(
        self, trade_date: str
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], Optional[Dict]]:
        """
        使用一致性检查获取每日基础数据

        Returns:
            Tuple[DataFrame, source_name, consistency_report]
        """
        available_adapters = self.get_available_adapters()
        if len(available_adapters) < 2:
            df, source = self.get_daily_basic_with_fallback(trade_date)
            return df, source, None
        primary_adapter = available_adapters[0]
        secondary_adapter = available_adapters[1]
        try:
            logger.info(
                f"🔍 获取数据进行一致性检查: {primary_adapter.name} vs {secondary_adapter.name}"
            )
            primary_data = primary_adapter.get_daily_basic(trade_date)
            secondary_data = secondary_adapter.get_daily_basic(trade_date)
            if primary_data is None or primary_data.empty:
                logger.warning(f"⚠️ 主数据源{primary_adapter.name}失败，使用fallback")
                df, source = self.get_daily_basic_with_fallback(trade_date)
                return df, source, None
            if secondary_data is None or secondary_data.empty:
                logger.warning(f"⚠️ 次数据源{secondary_adapter.name}失败，使用主数据源")
                return primary_data, primary_adapter.name, None
            if self.consistency_checker:
                consistency_result = self.consistency_checker.check_daily_basic_consistency(
                    primary_data,
                    secondary_data,
                    primary_adapter.name,
                    secondary_adapter.name,
                )
                final_data, resolution_strategy = self.consistency_checker.resolve_data_conflicts(
                    primary_data, secondary_data, consistency_result
                )
                consistency_report = {
                    'is_consistent': consistency_result.is_consistent,
                    'confidence_score': consistency_result.confidence_score,
                    'recommended_action': consistency_result.recommended_action,
                    'resolution_strategy': resolution_strategy,
                    'differences': consistency_result.differences,
                    'primary_source': primary_adapter.name,
                    'secondary_source': secondary_adapter.name,
                }
                logger.info(
                    f"📊 数据一致性检查完成: 置信度={consistency_result.confidence_score:.2f}, 策略={consistency_result.recommended_action}"
                )
                return final_data, primary_adapter.name, consistency_report
            else:
                logger.warning("⚠️ 一致性检查器不可用，使用主数据源")
                return primary_data, primary_adapter.name, None
        except Exception as e:
            logger.error(f"❌ 一致性检查失败: {e}")
            df, source = self.get_daily_basic_with_fallback(trade_date)
            return df, source, None



    def get_kline_with_fallback(
        self,
        code: str,
        period: str = "day",
        limit: int = 120,
        adj: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Tuple[Optional[List[Dict]], Optional[str]]:
        """按优先级尝试获取K线，返回(items, source)。

        给出日期区间时，只接受区间内的 K 线；区间外的数据会换下一个数据源。
        """
        # Probe only the adapter being attempted; do not wait for every provider.
        for adapter in self.adapters:
            request_timeout()
            if self._configured_enabled_map.get(adapter.name) is False:
                continue
            try:
                arguments = dict(code=code, period=period, limit=limit, adj=adj,
                                 start_date=start_date, end_date=end_date)
                logger.info(f"Trying to fetch kline from {adapter.name}")
                if adapter.name in {"qmt", "baostock"}:
                    # Availability probes can hang too: run them inside the worker.
                    from core.market_runtime.isolated_providers import get_isolated_kline
                    items = get_isolated_kline(adapter.name, arguments)
                else:
                    if not adapter.is_available():
                        continue
                    request_timeout()
                    items = adapter.get_kline(**arguments)
                if start_date or end_date:
                    items = _items_within_dates(items, start_date, end_date)
                if items:
                    return items, adapter.name
                if start_date or end_date:
                    logger.info(f"{adapter.name} 在 {start_date}..{end_date} 没有K线，尝试下一个数据源")
            except Exception as e:
                logger.error(f"Failed to fetch kline from {adapter.name}: {e}")
                continue
        return None, None

    def get_news_with_fallback(self, code: str, days: int = 2, limit: int = 50, include_announcements: bool = True) -> Tuple[Optional[List[Dict]], Optional[str]]:
        """按优先级尝试获取新闻与公告，返回(items, source)"""
        available_adapters = self.get_available_adapters()
        for adapter in available_adapters:
            try:
                logger.info(f"Trying to fetch news from {adapter.name}")
                items = adapter.get_news(code=code, days=days, limit=limit, include_announcements=include_announcements)
                if items:
                    return items, adapter.name
            except Exception as e:
                logger.error(f"Failed to fetch news from {adapter.name}: {e}")
                continue
        return None, None

    def get_daily_ohlc_with_fallback(self, code: str, trade_date: str, adj: Optional[str] = None) -> Tuple[Optional[Dict], Optional[str]]:
        """
        按优先级尝试获取指定日期的日线 OHLC 数据。

        Args:
            code: 6位股票代码
            trade_date: 交易日期，格式 YYYY-MM-DD 或 YYYYMMDD
            adj: 复权方式，None=不复权，"qfq"=前复权，"hfq"=后复权

        Returns:
            (ohlc_dict, source_name) 或 (None, None)
            ohlc_dict: {"date", "open", "high", "low", "close"}
        """
        available_adapters = self.get_available_adapters()
        for adapter in available_adapters:
            try:
                logger.info(f"Trying to fetch daily OHLC from {adapter.name} (adj={adj})")
                ohlc = adapter.get_daily_ohlc(code=code, trade_date=trade_date, adj=adj)
                if ohlc:
                    return ohlc, adapter.name
            except Exception as e:
                logger.error(f"Failed to fetch daily OHLC from {adapter.name}: {e}")
                continue
        return None, None

    def read_historical_bars(self, symbol, start_date=None, end_date=None, period="daily", cache_reader=None):
        start_date, end_date = normalize_date(start_date) or None, normalize_date(end_date) or None
        period = {"day": "daily", "week": "weekly", "month": "monthly"}.get(period, period)
        snapshot = current_snapshot()

        def read(start, end, selected_period):
            def load():
                return self._read_historical_bars_uncached(symbol, start, end, selected_period, cache_reader)
            cache = getattr(self, "_history_cache", None)
            if cache is None or cache_reader is not None:
                return load()
            return cache.get_or_load(
                (configuration_key(), _normalize_symbol(symbol), start, end, selected_period, ANALYSIS_BAR_ADJUST),
                load, cacheable=lambda value: value[0] is not None and not value[0].empty,
                timeout=request_timeout(),
            )
        if snapshot is not None:
            return snapshot.history(symbol, start_date, end_date, period, read)
        return read(start_date, end_date, period)

    def _read_historical_bars_uncached(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        cache_reader=None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """分析与同步共用的行情读路径。

        先读前复权缓存；没有覆盖请求区间时，按适配器优先级拉取同一复权口径并写回。
        日期会传给数据源，区间外的 K 线不会返回。
        """
        reader = cache_reader or _read_mongo_history
        try:
            cached = reader(symbol, start_date, end_date, period)
        except Exception as exc:
            logger.warning("读取行情缓存失败，改为按适配器拉取: %s", exc)
            cached = None
        cached = _filter_frame_by_dates(cached, start_date, end_date)
        has_cached = cached is not None and not cached.empty
        if has_cached and (cache_reader is not None or _history_reaches_end(cached, end_date, period)):
            return cached, "mongodb"

        try:
            items, source = self.get_kline_with_fallback(
                code=_normalize_symbol(symbol),
                period=_kline_period(period),
                limit=_history_bar_limit(start_date, end_date, period),
                adj=ANALYSIS_BAR_ADJUST,
                start_date=start_date,
                end_date=end_date,
            )
        except TimeoutError:
            if has_cached:
                return cached, "mongodb"
            raise
        if not items or not source:
            return (cached, "mongodb") if has_cached else (None, None)

        frame = _normalize_bar_units(
            _filter_frame_by_dates(_bars_to_frame(items), start_date, end_date),
            source,
        )
        if frame is None or frame.empty:
            return (cached, "mongodb") if has_cached else (None, None)
        if has_cached:
            cached_day, loaded_day = _latest_history_day(cached), _latest_history_day(frame)
            if cached_day and (loaded_day is None or loaded_day < cached_day):
                return cached, "mongodb"
        self._write_historical_bars(symbol, frame, source, period)
        return frame, source

    def _write_historical_bars(self, symbol: str, frame: pd.DataFrame, source: str, period: str) -> None:
        """把适配器拉到的行情按同步集合的键写回，不覆盖已有记录。"""
        if source in {"mongodb", "local"}:
            return
        try:
            from pymongo import UpdateOne
            from app.core.database import get_mongo_db_sync
            from app.services.historical_data_service import (
                HistoricalDataService,
                ensure_stock_quote_identity_index,
            )

            code = _normalize_symbol(symbol)
            prepared = frame.copy()
            if "date" in prepared.columns and "trade_date" not in prepared.columns:
                prepared["trade_date"] = prepared["date"]

            service = HistoricalDataService()
            operations = []
            for index, row in prepared.iterrows():
                document = service._standardize_record(code, row, source, "CN", period, index)
                document["adjust"] = ANALYSIS_BAR_ADJUST
                operations.append(UpdateOne(
                    {
                        "symbol": document["symbol"],
                        "trade_date": document["trade_date"],
                        "data_source": document["data_source"],
                        "period": document["period"],
                        "adjust": ANALYSIS_BAR_ADJUST,
                    },
                    {"$setOnInsert": document},
                    upsert=True,
                ))
            if operations:
                collection = get_mongo_db_sync().stock_daily_quotes
                ensure_stock_quote_identity_index(collection)
                collection.bulk_write(operations, ordered=False)
                logger.info("行情已写回缓存: %s source=%s rows=%s", code, source, len(operations))
        except Exception as exc:
            logger.warning("行情写回缓存失败，本次读取结果仍然返回: %s", exc)


def _normalize_symbol(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return str(symbol or "").strip()


def _history_bar_limit(start_date: Optional[str], end_date: Optional[str], period: str) -> int:
    defaults = {"daily": 250, "weekly": 120, "monthly": 60}
    default_limit = defaults.get(period, 250)
    if not start_date or not end_date:
        return default_limit
    try:
        days = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days
    except Exception:
        return default_limit
    days = max(int(days), 1)
    if period == "weekly":
        return min(max(days // 5 + 8, 20), 2000)
    if period == "monthly":
        return min(max(days // 28 + 3, 12), 600)
    return min(max(int(days * 0.75) + 10, 30), 4000)


def _history_reaches_end(frame, end_date, period):
    """Check daily freshness before freezing; calendar holidays may trigger a refresh.

    Failed refreshes retain dated cached data. No JIT job runs after a snapshot is
    frozen, so the report and structure cannot see different revisions.
    """
    if not end_date or period not in {"day", "daily"}:
        return True
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target = min(pd.Timestamp(end_date).date(), now.date())
    if target == now.date() and now.hour < 16:
        target -= timedelta(days=1)
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    latest = _latest_history_day(frame)
    return latest is not None and latest >= target


def _latest_history_day(frame):
    column = next((name for name in ("date", "trade_date", "time") if name in frame.columns), None)
    if column is None:
        return None
    dates = pd.to_datetime(frame[column], errors="coerce").dropna()
    return dates.max().date() if not dates.empty else None


def _kline_period(period: str) -> str:
    return {
        "daily": "day",
        "day": "day",
        "weekly": "week",
        "week": "week",
        "monthly": "month",
        "month": "month",
    }.get(period or "daily", "day")


def _bars_to_frame(items: List[Dict]) -> pd.DataFrame:
    frame = pd.DataFrame(items)
    return frame.rename(columns={"time": "date", "volume": "vol"})


def _normalize_bar_units(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    """把适配器原始单位换成同步入库使用的股和元。"""
    if frame is None or frame.empty or source != "tushare":
        return frame
    prepared = frame.copy()
    if "vol" in prepared.columns:
        prepared["vol"] = prepared["vol"] * 100
    if "volume" in prepared.columns:
        prepared["volume"] = prepared["volume"] * 100
    if "amount" in prepared.columns:
        prepared["amount"] = prepared["amount"] * 1000
    return prepared


def _filter_frame_by_dates(frame: pd.DataFrame, start_date: Optional[str], end_date: Optional[str]) -> pd.DataFrame:
    if frame is None or frame.empty or (not start_date and not end_date):
        return frame
    date_column = next((name for name in ("date", "trade_date", "time") if name in frame.columns), None)
    if date_column is None:
        return frame.iloc[0:0]
    dates = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    mask = dates.notna()
    if start_date:
        mask &= dates >= pd.to_datetime(start_date).normalize()
    if end_date:
        mask &= dates <= pd.to_datetime(end_date).normalize()
    return frame.loc[mask]


def _items_within_dates(items: Optional[List[Dict]], start_date: Optional[str], end_date: Optional[str]) -> List[Dict]:
    if not items:
        return []
    if not start_date and not end_date:
        return list(items)
    frame = _filter_frame_by_dates(pd.DataFrame(items), start_date, end_date)
    if frame is None or frame.empty:
        return []
    return frame.to_dict("records")


def _read_mongo_history(symbol: str, start_date: Optional[str], end_date: Optional[str], period: str):
    from tradingagents.dataflows.cache.mongodb_cache_adapter import get_mongodb_cache_adapter
    return get_mongodb_cache_adapter().get_historical_data(
        _normalize_symbol(symbol),
        start_date,
        end_date,
        period=period,
        adjust=ANALYSIS_BAR_ADJUST,
    )


_app_data_source_manager = None
_app_manager_lock = threading.Lock()
_app_manager_key = None
_app_manager_created = 0.0


def get_app_data_source_manager() -> DataSourceManager:
    """Reuse adapters; refresh after config changes or the config's five-minute TTL."""
    global _app_data_source_manager, _app_manager_key, _app_manager_created
    with _app_manager_lock:
        key = configuration_key()
        if (_app_data_source_manager is None or key != _app_manager_key
                or time.monotonic() - _app_manager_created >= 300):
            _app_data_source_manager = DataSourceManager()
            _app_manager_key = configuration_key()
            _app_manager_created = time.monotonic()
        return _app_data_source_manager
