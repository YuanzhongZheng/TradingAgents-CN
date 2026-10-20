"""Concurrency, snapshot consistency and deadline contracts; no live providers."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pandas as pd
import pytest

from core.market_runtime.cache import BoundedExecutor, BusyError, SingleFlightCache
from core.market_runtime.context import analysis_snapshot, current_snapshot, request_deadline


def test_singleflight_coalesces_before_worker_allocation_and_cleans_up():
    cache, pool = SingleFlightCache(maxsize=2), BoundedExecutor(workers=1)
    release = Event()
    calls = []

    def load():
        calls.append(1)
        assert release.wait(2)
        return {"prices": [10]}

    try:
        futures = [cache.submit(pool, "same", load) for _ in range(30)]
        assert all(f is futures[0] for f in futures)
        with pytest.raises(TimeoutError):
            futures[0].result(timeout=0.01)
        assert not futures[0].cancel()
        with pytest.raises(BusyError):
            pool.submit(lambda: None)
        release.set()
        assert futures[0].result(timeout=2) == {"prices": [10]}
        first = cache.get_or_load("same", load)
        first["prices"].append(20)
        assert cache.get_or_load("same", load) == {"prices": [10]}
        assert len(calls) == 1
        # Ensure the completed worker released its slot and inflight record.
        pool._pool.shutdown(wait=True)
        assert cache._inflight == {}
    finally:
        release.set()
        pool._pool.shutdown(wait=True)


def test_ttl_capacity_failures_and_clear_generation(monkeypatch):
    import core.market_runtime.cache as module
    now = [10.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    cache = SingleFlightCache(maxsize=3, ttl=2)
    for key in range(1000):
        cache.get_or_load(key, lambda: [1])
    assert len(cache._values) == 3
    assert not cache._inflight
    now[0] += 3
    assert cache.get_or_load(999, lambda: [2]) == [2]
    assert len(cache._values) == 1
    with pytest.raises(ValueError):
        cache.get_or_load("error", lambda: (_ for _ in ()).throw(ValueError("failed")))
    assert not cache._inflight
    assert cache.get_or_load("error", lambda: 3) == 3
    cache.get_or_load("negative", lambda: None, cacheable=lambda value: value is not None)
    assert "negative" not in cache._values

    pool, started, release = BoundedExecutor(1), Event(), Event()
    def old_load():
        started.set()
        assert release.wait(2)
        return "old"
    try:
        old = cache.submit(pool, "generation", old_load)
        assert started.wait(2)
        cache.clear()
        assert cache.get_or_load("generation", lambda: "new") == "new"
        release.set()
        assert old.result(timeout=2) == "old"
        assert cache.get_or_load("generation", lambda: "wrong") == "new"
    finally:
        release.set()
        pool._pool.shutdown(wait=True)


def test_snapshot_shares_daily_bars_across_structure_and_report(monkeypatch):
    from app.services.data_sources import manager as manager_module
    from core.tools.implementations.technical.price_structure import load_structure_bars
    from tradingagents.dataflows.data_source_manager import DataSourceManager as ReportManager

    manager = manager_module.DataSourceManager.__new__(manager_module.DataSourceManager)
    calls = []
    frame = pd.DataFrame([
        {"date": "2024-01-02", "open": 9, "high": 11, "low": 8, "close": 10, "vol": 100},
        {"date": "2024-01-03", "open": 10, "high": 12, "low": 9, "close": 11, "vol": 200},
    ])
    def load(symbol, start, end, period, reader):
        calls.append((symbol, start, end, period))
        return frame.copy(), "fixture"
    manager._read_historical_bars_uncached = load
    monkeypatch.setattr(manager_module, "get_app_data_source_manager", lambda: manager)
    report_manager = ReportManager.__new__(ReportManager)
    with analysis_snapshot("2024-01-03") as snapshot:
        bars, source, status = load_structure_bars("000001", "day", "2024-01-03")
        frame.loc[1, "close"] = 999  # upstream update must not alter this analysis
        reported, report_source = report_manager._read_shared_history_frame(
            "000001.SZ", "2024-01-02", "2024-01-03", "daily")
        assert source == report_source == "fixture"
        assert status == "ok"
        assert bars[-1].close == reported.iloc[-1]["close"] == 11
        assert snapshot.latest_daily_date("000001.SZ") == "2024-01-03"
        assert len(calls) == 1
    assert current_snapshot() is None
    with analysis_snapshot("2024-01-03"):
        fresh, _, _ = load_structure_bars("000001", "day", "2024-01-03")
        assert fresh[-1].close == 999


def test_partial_results_deadline_and_period_subset_reuse(monkeypatch):
    from core.tools.implementations.technical import price_structure as module
    from core.tools.implementations.technical.structure_bars import StructureBar
    pool, release, started = BoundedExecutor(2), Event(), Event()
    calls = []
    def load(symbol, period, end):
        calls.append((period, current_snapshot()))
        if period == "5m":
            started.set()
            assert release.wait(2)
        return [StructureBar("2024-01-02", 10, 11, 9, 10)], "fixture", "ok"
    monkeypatch.setattr(module, "_STRUCTURE_POOL", pool)
    monkeypatch.setattr(module, "load_structure_bars", load)
    monkeypatch.setenv("MARKET_STRUCTURE_TIMEOUT_SECONDS", "0.1")
    module.clear_structure_cache()
    try:
        with analysis_snapshot("2024-01-03") as snapshot:
            start = monotonic()
            result = module.analyze_price_structure("000001.SZ", "20240103", ("day", "5m"))
            assert monotonic() - start < 0.7
            assert started.is_set()
            assert result["day"]["source"] == "fixture" and not result["day"]["error"]
            assert "超过" in result["5m"]["error"]
            result["day"]["source"] = "mutated"
            again = module.analyze_price_structure("000001", "2024-01-03", ("day",))
            assert again["day"]["source"] == "fixture"
            assert [p for p, _ in calls].count("day") == 1
            assert all(ctx is snapshot for _, ctx in calls)
    finally:
        release.set()
        pool._pool.shutdown(wait=True)
        module.clear_structure_cache()


def test_source_config_and_adjustment_are_in_cache_namespace(monkeypatch):
    from core.market_runtime.context import configuration_key
    from app.core.unified_config import UnifiedConfigManager
    from core.tools.context import set_current_analysis_data_source, reset_current_analysis_data_source
    monkeypatch.setattr(UnifiedConfigManager, "_ds_config_revision", 0)
    monkeypatch.setattr(UnifiedConfigManager, "_ds_config_cache", [])
    monkeypatch.setattr(UnifiedConfigManager, "_ds_config_cache_time", 0)
    before = configuration_key()
    token = set_current_analysis_data_source("qmt")
    try:
        assert configuration_key() != before
        assert configuration_key()[-1] == "qfq"
    finally:
        reset_current_analysis_data_source(token)
    UnifiedConfigManager.invalidate_data_source_cache()
    assert configuration_key() != before


def test_http_budget_reaches_transport_without_global_mutation(monkeypatch):
    from core.market_runtime.transport import call_with_http_budget, BudgetTushareApi
    recorded = []
    requests = SimpleNamespace(get=lambda *args, **kwargs: recorded.append(kwargs["timeout"]))
    scope = {"requests": requests, "__name__": "fake_sdk"}
    exec("def daily():\n    requests.get('url')\ndef minute():\n    daily()\n    requests.get('url', timeout=30)\n", scope)
    monkeypatch.setenv("MARKET_PROVIDER_TIMEOUT_SECONDS", "0.5")
    with request_deadline(0.2):
        call_with_http_budget(scope["minute"])
    assert len(recorded) == 2 and all(0 < t <= 0.2 for t in recorded)
    assert scope["requests"] is requests
    assert scope["minute"].__globals__ is scope

    class Api:
        def __init__(self):
            self._DataApi__timeout = 30
        def daily(self):
            return self._DataApi__timeout
    api = Api()
    with request_deadline(0.2):
        assert 0 < BudgetTushareApi(api).daily() <= 0.2
    assert api._DataApi__timeout == 30

    exec("def get_tx_start_year():\n    requests.get('url', timeout=30)\n    return '2024-01-01'\ndef tencent():\n    return get_tx_start_year()\n", scope)
    with request_deadline(0.2):
        assert call_with_http_budget(scope["tencent"]) == "2024-01-01"
    assert 0 < recorded[-1] <= 0.2
    external_scope = {"requests": requests, "__name__": "external_sdk"}
    exec("def get_tx_start_year():\n    requests.get('url', timeout=30)\n    return '2023-01-01'\n", external_scope)
    scope["get_tx_start_year"] = external_scope["get_tx_start_year"]
    with request_deadline(0.2):
        assert call_with_http_budget(scope["tencent"]) == "2023-01-01"
    assert 0 < recorded[-1] <= 0.2


def test_lazy_probe_stops_after_success():
    from app.services.data_sources.manager import DataSourceManager
    manager = DataSourceManager.__new__(DataSourceManager)
    probes = []
    def adapter(name):
        return SimpleNamespace(name=name,
            is_available=lambda: probes.append(name) or True,
            get_kline=lambda **kwargs: [{"time": "2024-01-02", "close": 10}])
    manager.adapters = [adapter("first"), adapter("unused")]
    manager._configured_enabled_map = {}
    assert manager.get_kline_with_fallback("000001")[1] == "first"
    assert probes == ["first"]


def test_baostock_does_not_return_daily_bars_as_minutes():
    from app.services.data_sources.baostock_adapter import BaoStockAdapter
    adapter = BaoStockAdapter.__new__(BaoStockAdapter)
    adapter.is_available = lambda: (_ for _ in ()).throw(AssertionError("should not probe"))
    assert adapter.get_kline("000001", "5m", start_date="2024-01-01", end_date="2024-01-31") is None


def test_stale_cache_refreshes_once_before_snapshot_is_frozen(monkeypatch):
    from app.services.data_sources import manager as module
    manager = module.DataSourceManager.__new__(module.DataSourceManager)
    old = pd.DataFrame([{"date": "2024-01-02", "open": 9, "high": 11, "low": 8, "close": 10}])
    monkeypatch.setattr(module, "_read_mongo_history", lambda *args: old.copy())
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return [{"time": "2024-01-03", "open": 10, "high": 12, "low": 9, "close": 11}], "fixture"
    manager.get_kline_with_fallback = fetch
    manager._write_historical_bars = lambda *args: None
    with analysis_snapshot("2024-01-03"):
        first, source = manager.read_historical_bars("000001", "2024-01-01", "2024-01-03")
        second, _ = manager.read_historical_bars("000001.SZ", "2024-01-02", "2024-01-03")
    assert len(calls) == 1 and source == "fixture"
    assert first.iloc[-1]["close"] == second.iloc[-1]["close"] == 11
    # A successful but older provider response must not replace newer cached bars.
    manager.get_kline_with_fallback = lambda **kwargs: ([{
        "time": "2024-01-01", "open": 8, "high": 10, "low": 7, "close": 9}], "older")
    with analysis_snapshot("2024-01-03"):
        stale, source = manager.read_historical_bars("000001", "2024-01-01", "2024-01-03")
        assert source == "mongodb" and stale.iloc[-1]["close"] == 10
    manager.get_kline_with_fallback = lambda **kwargs: (None, None)
    with analysis_snapshot("2024-01-03"):
        stale, source = manager.read_historical_bars("000001", "2024-01-01", "2024-01-03")
        assert source == "mongodb" and stale.iloc[-1]["close"] == 10


def test_market_tool_timeout_does_not_create_duplicate_workers(monkeypatch):
    from core.tools.implementations.market import stock_market_data as module
    pool, release, started = BoundedExecutor(1), Event(), Event()
    calls = []
    def load(*args):
        calls.append(1)
        started.set()
        assert release.wait(2)
        return "ready"
    monkeypatch.setattr(module, "MARKET_IO_POOL", pool)
    monkeypatch.setattr(module, "_load_unified_market_data", load)
    monkeypatch.setenv("MARKET_STRUCTURE_TIMEOUT_SECONDS", "0.1")
    module._MARKET_RESULT_CACHE.clear()
    args = {"ticker": "000001", "start_date": "2024-01-03", "end_date": "2024-01-03"}
    try:
        with analysis_snapshot("2024-01-03"):
            assert "超过" in module.get_stock_market_data_unified.invoke(args)
            assert started.is_set()
            assert "超过" in module.get_stock_market_data_unified.invoke(args)
            assert len(calls) == 1
            release.set()
            pool._pool.shutdown(wait=True)
            assert module.get_stock_market_data_unified.invoke(args) == "ready"
    finally:
        release.set()
        pool._pool.shutdown(wait=True)
        module._MARKET_RESULT_CACHE.clear()


def test_analyst_scope_resets_even_when_execution_fails(monkeypatch):
    from core.agents.adapters.market_analyst_v2 import MarketAnalystV2
    agent = MarketAnalystV2()
    agent._extract_common_params = lambda state: ("000001", "2024-01-03")
    def fail(state):
        assert current_snapshot().end_date == "2024-01-03"
        raise RuntimeError("fixture")
    agent._execute_with_snapshot = fail
    with pytest.raises(RuntimeError, match="fixture"):
        agent.execute({})
    assert current_snapshot() is None


def test_manager_concurrent_initialization_and_config_refresh(monkeypatch):
    from app.services.data_sources import manager as module
    key, created = ["config-1"], []
    def create():
        item = object()
        created.append(item)
        return item
    monkeypatch.setattr(module, "_app_data_source_manager", None)
    monkeypatch.setattr(module, "_app_manager_key", None)
    monkeypatch.setattr(module, "_app_manager_created", 0)
    monkeypatch.setattr(module, "configuration_key", lambda: key[0])
    monkeypatch.setattr(module, "DataSourceManager", create)
    with ThreadPoolExecutor(max_workers=8) as pool:
        managers = list(pool.map(lambda _: module.get_app_data_source_manager(), range(20)))
    assert len(created) == 1 and all(m is created[0] for m in managers)
    with analysis_snapshot("2024-01-03") as snapshot:
        old = snapshot.get_manager()
        key[0] = "config-2"
        assert module.get_app_data_source_manager() is not old
        assert snapshot.get_manager() is old
    assert len(created) == 2


def test_actual_tushare_client_transport_receives_budget(monkeypatch):
    from tushare.pro.client import DataApi
    from tushare.pro import client as module
    from core.market_runtime.transport import BudgetTushareApi
    captured = []
    def post(url, **kwargs):
        captured.append(kwargs["timeout"])
        return SimpleNamespace(text='{"code": 0, "data": {"fields": ["close"], "items": [[10]]}}')
    monkeypatch.setattr(module.requests, "post", post)
    api = DataApi("fixture", timeout=30)
    with request_deadline(0.2):
        frame = BudgetTushareApi(api).daily(ts_code="000001.SZ")
    assert frame.iloc[0]["close"] == 10
    assert 0 < captured[0] <= 0.2
    assert api._DataApi__timeout == 30
