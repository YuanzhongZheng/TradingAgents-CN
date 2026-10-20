"""Analysis-scoped frozen bar snapshots and cooperative request deadlines."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
import re
from threading import Lock
from time import monotonic
from uuid import uuid4

from .cache import SingleFlightCache

_snapshot = ContextVar("market_snapshot", default=None)
_deadline = ContextVar("market_request_deadline", default=None)


def normalize_symbol(symbol):
    return re.sub(r"\.(SH|SZ|BJ)$", "", str(symbol).strip().upper())


def normalize_date(value):
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) >= 8 else str(value)


def configuration_key():
    # Only cached config is inspected here: no database probes on cache hits.
    from app.core.unified_config import UnifiedConfigManager
    from app.core.config import settings
    from core.tools.context import get_current_analysis_data_source
    configs = UnifiedConfigManager._ds_config_cache or []
    data = [c.model_dump(mode="json") for c in configs]
    knobs = {k: getattr(settings, k, None) for k in (
        "ENABLE_TUSHARE", "ENABLE_BAOSTOCK", "BAOSTOCK_UNIFIED_ENABLED",
        "MARKET_ANALYST_LOOKBACK_DAYS", "QMT_UNIFIED_ENABLED", "QMT_DATA_DIR",
        "QMT_TOKEN", "QMT_LISTEN_PORT_RANGE", "TUSHARE_TOKEN", "TUSHARE_API_KEY",
    )}
    digest = sha256(json.dumps([UnifiedConfigManager._ds_config_revision, data, knobs], sort_keys=True, default=str).encode()).hexdigest()
    return digest, get_current_analysis_data_source(), "qfq"


def cache_namespace():
    snapshot = current_snapshot()
    return snapshot.identity if snapshot else configuration_key()


def current_snapshot():
    return _snapshot.get()


def market_wait_budget():
    from app.core.config import settings
    return max(0.1, float(os.getenv("MARKET_STRUCTURE_TIMEOUT_SECONDS", settings.MARKET_STRUCTURE_TIMEOUT_SECONDS)))


def request_timeout():
    from app.core.config import settings
    limit = max(0.1, float(os.getenv("MARKET_PROVIDER_TIMEOUT_SECONDS", settings.MARKET_PROVIDER_TIMEOUT_SECONDS)))
    deadline = _deadline.get()
    if deadline is not None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("行情取数时间预算已用尽")
        limit = min(limit, remaining)
    return limit


@contextmanager
def request_deadline(seconds):
    existing = _deadline.get()
    token = _deadline.set(min(existing or float("inf"), monotonic() + seconds))
    try:
        yield
    finally:
        _deadline.reset(token)


class MarketSnapshot:
    def __init__(self, end_date):
        self.identity = uuid4().hex
        self.end_date = normalize_date(end_date)
        self.cache = SingleFlightCache(maxsize=64, ttl=float("inf"))
        self.manager = None
        self._daily_dates = {}
        self._manager_lock = Lock()

    def get_manager(self):
        with self._manager_lock:
            if self.manager is None:
                from app.services.data_sources.manager import get_app_data_source_manager
                self.manager = get_app_data_source_manager()
            return self.manager

    def latest_daily_date(self, symbol):
        with self._manager_lock:
            return self._daily_dates.get(normalize_symbol(symbol))

    def history(self, symbol, start, end, period, loader):
        from app.services.data_sources.manager import _filter_frame_by_dates
        from app.core.config import settings
        start, end = normalize_date(start), normalize_date(end)
        canonical_end = self.end_date or end
        if end and canonical_end:
            end = min(end, canonical_end)
        query_start, query_end = start, end
        if period in ("day", "daily") and canonical_end:
            span = max(465, int(settings.MARKET_ANALYST_LOOKBACK_DAYS) + 10)
            baseline = (datetime.fromisoformat(canonical_end) - timedelta(days=span)).date().isoformat()
            # One daily snapshot, including the lookback used by technical indicators.
            if start and start < baseline:
                raise ValueError("请求超出本次分析行情快照的回溯区间")
            query_start, query_end, period = baseline, canonical_end, "daily"
        key = (normalize_symbol(symbol), query_start, query_end, period, "qfq")
        frame, source = self.cache.get_or_load(
            key, lambda: loader(query_start, query_end, period), timeout=request_timeout())
        if period == "daily" and frame is not None and not frame.empty:
            import pandas as pd
            column = next((c for c in ("date", "trade_date", "time") if c in frame.columns), None)
            if column:
                dates = pd.to_datetime(frame[column], errors="coerce").dropna()
                if not dates.empty:
                    with self._manager_lock:
                        self._daily_dates[normalize_symbol(symbol)] = dates.max().date().isoformat()
        return _filter_frame_by_dates(frame, start or None, end or None), source


@contextmanager
def analysis_snapshot(end_date):
    snapshot = MarketSnapshot(end_date)
    token = _snapshot.set(snapshot)
    try:
        yield snapshot
    finally:
        _snapshot.reset(token)
