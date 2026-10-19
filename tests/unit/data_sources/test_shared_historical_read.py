import pandas as pd

from app.services.data_sources.manager import DataSourceManager


def test_cached_history_is_returned_without_calling_adapters() -> None:
    manager = DataSourceManager.__new__(DataSourceManager)
    manager.adapter_calls = 0

    def reject_live_fetch(*args, **kwargs):
        manager.adapter_calls += 1
        return None, None

    manager.get_kline_with_fallback = reject_live_fetch
    cached = pd.DataFrame([{"trade_date": "2024-01-02", "close": 10.0}])

    frame, source = manager.read_historical_bars(
        "000001",
        "2024-01-01",
        "2024-01-31",
        cache_reader=lambda *args, **kwargs: cached,
    )

    assert source == "mongodb"
    assert manager.adapter_calls == 0
    assert frame.iloc[0]["close"] == 10.0


def test_cache_miss_uses_adapter_priority_and_keeps_rows() -> None:
    manager = DataSourceManager.__new__(DataSourceManager)
    seen = {}

    def live_fetch(code, period, limit):
        seen.update({"code": code, "period": period, "limit": limit})
        return [{
            "time": "2024-01-02",
            "open": 9.0,
            "high": 11.0,
            "low": 8.5,
            "close": 10.5,
            "volume": 100,
            "amount": 1000,
        }], "akshare"

    manager.get_kline_with_fallback = live_fetch
    manager.written = None

    def remember_write(symbol, frame, source, period):
        manager.written = (symbol, source, period, len(frame))

    manager._write_historical_bars = remember_write

    frame, source = manager.read_historical_bars(
        "000001.SZ",
        "2024-01-01",
        "2024-01-31",
        cache_reader=lambda *args, **kwargs: None,
    )

    assert source == "akshare"
    assert seen["code"] == "000001"
    assert seen["period"] == "day"
    assert frame.iloc[0]["close"] == 10.5
    assert manager.written[1] == "akshare"


def test_tushare_bars_are_converted_to_sync_units() -> None:
    manager = DataSourceManager.__new__(DataSourceManager)

    def live_fetch(code, period, limit):
        return [{
            "time": "2024-01-02",
            "open": 9.0,
            "high": 11.0,
            "low": 8.5,
            "close": 10.5,
            "volume": 100,
            "amount": 2,
        }], "tushare"

    manager.get_kline_with_fallback = live_fetch
    manager._write_historical_bars = lambda *args, **kwargs: None

    frame, source = manager.read_historical_bars(
        "000001",
        cache_reader=lambda *args, **kwargs: None,
    )

    assert source == "tushare"
    assert frame.iloc[0]["vol"] == 10000
    assert frame.iloc[0]["amount"] == 2000
