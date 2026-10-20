import pandas as pd
from pymongo import UpdateOne

from app.services.data_sources.manager import (
    ANALYSIS_BAR_ADJUST,
    DataSourceManager,
    _filter_frame_by_dates,
)
from app.services.historical_data_service import historical_adjust_query


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


def test_cache_miss_requests_forward_adjusted_date_range() -> None:
    manager = DataSourceManager.__new__(DataSourceManager)
    seen = {}

    def live_fetch(code, period="day", limit=120, adj=None, start_date=None, end_date=None):
        seen.update({
            "code": code,
            "period": period,
            "limit": limit,
            "adj": adj,
            "start_date": start_date,
            "end_date": end_date,
        })
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
    assert seen["adj"] == "qfq"
    assert seen["start_date"] == "2024-01-01"
    assert seen["end_date"] == "2024-01-31"
    assert frame.iloc[0]["close"] == 10.5
    assert manager.written[1] == "akshare"


def test_tushare_bars_are_converted_to_sync_units() -> None:
    manager = DataSourceManager.__new__(DataSourceManager)

    def live_fetch(code, period="day", limit=120, adj=None, start_date=None, end_date=None):
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


def test_bars_outside_requested_range_are_dropped_and_next_source_is_used() -> None:
    manager = DataSourceManager.__new__(DataSourceManager)
    calls = []

    class Adapter:
        def __init__(self, name, rows):
            self.name = name
            self.rows = rows

        def is_available(self):
            return True

        def get_kline(self, code, period="day", limit=120, adj=None, start_date=None, end_date=None):
            calls.append({
                "name": self.name,
                "adj": adj,
                "start_date": start_date,
                "end_date": end_date,
            })
            return self.rows

    future = Adapter("tushare", [{
        "time": "2026-10-16",
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 99,
        "volume": 1,
        "amount": 1,
    }])
    january = Adapter("akshare", [{
        "time": "2024-01-16",
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 10,
        "volume": 1,
        "amount": 1,
    }])
    manager.adapters = [future, january]
    manager._configured_enabled_map = {}
    manager._write_historical_bars = lambda *args, **kwargs: None

    frame, source = manager.read_historical_bars(
        "000001",
        "2024-01-01",
        "2024-01-31",
        cache_reader=lambda *args, **kwargs: None,
    )

    assert [call["name"] for call in calls] == ["tushare", "akshare"]
    assert all(call["adj"] == ANALYSIS_BAR_ADJUST for call in calls)
    assert all(call["start_date"] == "2024-01-01" and call["end_date"] == "2024-01-31" for call in calls)
    assert source == "akshare"
    assert frame.iloc[0]["close"] == 10
    assert "2026-10-16" not in set(frame["date"].astype(str))


def test_empty_requested_range_returns_no_rows_instead_of_latest_bars() -> None:
    manager = DataSourceManager.__new__(DataSourceManager)
    manager.written = False

    class Adapter:
        name = "tushare"

        def is_available(self):
            return True

        def get_kline(self, code, period="day", limit=120, adj=None, start_date=None, end_date=None):
            return [{
                "time": "2026-10-16",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 99,
                "volume": 1,
                "amount": 1,
            }]

    manager.adapters = [Adapter()]
    manager._configured_enabled_map = {}

    def remember_write(*args, **kwargs):
        manager.written = True

    manager._write_historical_bars = remember_write

    frame, source = manager.read_historical_bars(
        "000001",
        "2024-01-01",
        "2024-01-31",
        cache_reader=lambda *args, **kwargs: None,
    )

    assert frame is None
    assert source is None
    assert manager.written is False


def test_date_filter_does_not_fall_back_to_unfiltered_rows() -> None:
    frame = pd.DataFrame([{"date": "2026-10-16", "close": 99}])
    filtered = _filter_frame_by_dates(frame, "2024-01-01", "2024-01-31")
    assert filtered.empty


def test_forward_adjusted_cache_is_stored_separately(monkeypatch) -> None:
    manager = DataSourceManager.__new__(DataSourceManager)
    collection = type("Collection", (), {})()
    collection.operations = None

    def bulk_write(operations, ordered=False):
        collection.operations = operations

    collection.bulk_write = bulk_write

    class Service:
        def _standardize_record(self, code, row, source, market, period, index):
            return {
                "symbol": code,
                "trade_date": "2024-01-02",
                "data_source": source,
                "period": period,
                "close": float(row["close"]),
            }

    monkeypatch.setattr(
        "app.services.historical_data_service.HistoricalDataService",
        Service,
    )
    monkeypatch.setattr(
        "app.services.historical_data_service.ensure_stock_quote_identity_index",
        lambda target: None,
    )
    monkeypatch.setattr(
        "app.core.database.get_mongo_db_sync",
        lambda: type("DB", (), {"stock_daily_quotes": collection})(),
    )

    manager._write_historical_bars(
        "000001",
        pd.DataFrame([{"date": "2024-01-02", "close": 10.0, "vol": 100}]),
        "tushare",
        "daily",
    )

    operation = collection.operations[0]
    assert isinstance(operation, UpdateOne)
    assert operation._filter["adjust"] == "qfq"
    assert operation._doc["$setOnInsert"]["adjust"] == "qfq"
    assert historical_adjust_query() == {"adjust": None}
    assert historical_adjust_query("qfq") == {"adjust": "qfq"}
