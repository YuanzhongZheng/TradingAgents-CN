from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from core.market_runtime.process_worker import ProviderProcess, ProviderUnavailable

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "market_sdk_worker.py"


def worker(**kwargs):
    return ProviderProcess("fixture", command=[sys.executable, str(FIXTURE)], **kwargs)


def call(instance, op="echo", timeout=2, **arguments):
    return instance.call(op, arguments, config={}, timeout=timeout)


def wait_until(predicate, timeout=2):
    end = monotonic() + timeout
    while monotonic() < end:
        if predicate():
            return
        sleep(0.005)
    assert predicate()


def test_repeated_permanent_hangs_are_reaped_and_normal_requests_recover():
    isolated = worker(failure_limit=100)
    other = worker()
    pids = set()
    try:
        for _ in range(3):
            first = call(isolated)
            pids.add(first["pid"])
            process = isolated._process
            directory = Path(isolated._directory.name)
            start = monotonic()
            with pytest.raises(TimeoutError):
                call(isolated, "hang", timeout=0.5)
            assert monotonic() - start < 1.2
            assert process.poll() is not None  # SIGTERM was ignored; kill/reap is required.
            assert isolated.pid is None and not directory.exists()
            assert call(other)["pid"] == other.pid
            recovered = call(isolated)
            assert recovered["pid"] not in pids
        assert len(pids) == 3
    finally:
        isolated.close()
        other.close()


def test_crash_stale_response_and_configuration_change_replace_process():
    isolated = worker(failure_limit=100)
    try:
        first = call(isolated)["pid"]
        for operation in ("crash", "stale"):
            process = isolated._process
            with pytest.raises(ProviderUnavailable):
                call(isolated, operation)
            assert process.poll() is not None
            assert call(isolated)["pid"] != process.pid
        old = isolated._process
        changed = isolated.call("echo", {}, config={"revision": 2}, timeout=2)
        assert changed["pid"] != old.pid and old.poll() is not None
    finally:
        isolated.close()


def test_circuit_opens_then_allows_recovery():
    isolated = worker(cooldown=0.1, failure_limit=1)
    try:
        with pytest.raises(ProviderUnavailable):
            call(isolated, "crash")
        with pytest.raises(ProviderUnavailable, match="暂停"):
            call(isolated)
        assert isolated.pid is None
        sleep(0.12)
        assert call(isolated)["pid"] == isolated.pid
        assert isolated._failures == 0
    finally:
        isolated.close()


def test_queue_is_bounded_and_queue_timeout_does_not_kill_active_job():
    isolated = worker(queue_size=1)
    try:
        call(isolated)
        with ThreadPoolExecutor(2) as pool:
            running = pool.submit(call, isolated, "slow", 2, seconds=0.4)
            wait_until(lambda: not (Path(isolated._directory.name) / "request.json").exists() and isolated._lock.locked())
            queued = pool.submit(call, isolated, "echo", 0.1)
            wait_until(lambda: isolated._admission._value == 0)
            with pytest.raises(ProviderUnavailable, match="队列已满"):
                call(isolated)
            with pytest.raises(TimeoutError, match="排队"):
                queued.result()
            assert running.result()["pid"] == isolated.pid
            assert call(isolated)["pid"] == isolated.pid
    finally:
        isolated.close()


def test_shutdown_reaps_running_process():
    isolated = worker()
    call(isolated)
    process = isolated._process
    with ThreadPoolExecutor(1) as pool:
        hung = pool.submit(call, isolated, "hang", 10)
        wait_until(lambda: (Path(isolated._directory.name) / "hung").exists())
        isolated.close()
        with pytest.raises(TimeoutError):
            hung.result(timeout=2)
    assert process.poll() is not None
    with pytest.raises(ProviderUnavailable, match="关闭"):
        call(isolated)


def test_unreaped_process_is_quarantined_instead_of_spawning_more(monkeypatch):
    isolated = worker(failure_limit=100)
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, kill=lambda: None)
    def stuck_wait(**kwargs):
        raise subprocess.TimeoutExpired("fixture", 0.4)
    process.wait = stuck_wait
    isolated._process = process
    isolated._broken = True
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn a replacement"))
    try:
        with pytest.raises(ProviderUnavailable, match="尚未退出"):
            call(isolated)
        assert isolated._process is process
    finally:
        isolated._process = None
        isolated.close()


def test_manager_skips_parent_probe_and_falls_back_after_process_timeout(monkeypatch):
    from app.services.data_sources.manager import DataSourceManager
    from core.market_runtime import isolated_providers as module
    calls = []
    def fail(provider, arguments):
        calls.append(provider)
        raise TimeoutError("fixture")
    monkeypatch.setattr(module, "get_isolated_kline", fail)
    manager = DataSourceManager.__new__(DataSourceManager)
    manager._configured_enabled_map = {}
    manager.adapters = [SimpleNamespace(name="qmt", is_available=lambda: pytest.fail("parent probe")),
        SimpleNamespace(name="akshare", is_available=lambda: True, get_kline=lambda **kw: [{"time": "2024-01-02"}])]
    rows, source = manager.get_kline_with_fallback("000001", end_date="2024-01-03")
    assert calls == ["qmt"] and source == "akshare" and rows


def test_worker_qmt_reads_without_download_and_baostock_sets_child_socket_timeout(monkeypatch):
    from core.market_runtime.provider_worker import execute
    import socket
    captured = []
    adapter = SimpleNamespace(is_available=lambda: True, get_kline=lambda **args: captured.append(args) or [])
    request = {"operation": "kline", "arguments": {"code": "000001", "period": "day"}, "deadline": monotonic() + 2}
    assert execute(adapter, "qmt", request) == {"rows": []}
    assert captured[-1]["download_history"] is False
    timeouts = []
    monkeypatch.setattr(socket, "setdefaulttimeout", timeouts.append)
    execute(adapter, "baostock", request)
    assert 0 < timeouts[0] <= 2


def test_qmt_download_has_separate_worker_and_bounded_deduplicated_queue(monkeypatch):
    from core.market_runtime import isolated_providers as module
    started, release = Event(), Event()
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        started.set()
        assert release.wait(3)
    fake = SimpleNamespace(call=download, close=lambda: release.set())
    monkeypatch.setitem(module._workers, "qmt_download", fake)
    queue = module.HistoryRefreshQueue()
    try:
        assert queue.schedule({"code": "000001", "period": "day"}, {})
        assert started.wait(2)
        assert not queue.schedule({"code": "000001", "period": "day"}, {})
        assert all(queue.schedule({"code": str(i), "period": "day"}, {}) for i in range(8))
        assert not queue.schedule({"code": "overflow", "period": "day"}, {})
        assert len(queue._pending) <= 9
        # The read worker is a different process supervisor.
        assert module._workers["qmt"] is not fake
        release.set()
        wait_until(lambda: not queue._pending)
        assert not queue.schedule({"code": "000001", "period": "day"}, {})
    finally:
        queue.close()


def test_reject_invalid_rows_before_parent_caches():
    from core.market_runtime.isolated_providers import validate_rows
    with pytest.raises(ProviderUnavailable):
        validate_rows([{"date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": float("nan")}])
    with pytest.raises(ProviderUnavailable):
        validate_rows({"close": 10})


def test_actual_worker_entry_point_with_fake_baostock_sdk(tmp_path, monkeypatch):
    # Exercise child import, login/query/logout, serialization and process reuse
    # without live network or the parent's monkeypatched adapter objects.
    (tmp_path / "baostock.py").write_text('''
class Result:
    error_code = '0'
    def __init__(self): self.position = 0
    def next(self):
        self.position += 1
        return self.position == 1
    def get_row_data(self): return ['2024-01-02','sz.000001','10','12','9','11','100','1100']
def login(): return Result()
def logout(): return Result()
def query_history_k_data_plus(**kwargs): return Result()
''')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    isolated = ProviderProcess("baostock")
    args = {"code": "000001", "period": "day", "limit": 100, "adj": "qfq",
            "start_date": "2024-01-01", "end_date": "2024-01-03"}
    try:
        result = isolated.call("kline", args, config={}, timeout=12)
        pid = isolated.pid
        assert result["rows"][0]["close"] == 11
        assert isolated.call("kline", args, config={}, timeout=2) == result
        assert isolated.pid == pid
    finally:
        isolated.close()


def test_qmt_read_adapter_does_not_download(monkeypatch):
    import pandas as pd
    from app.services.data_sources import qmt_adapter as module
    adapter = module.QMTAdapter()
    monkeypatch.setattr(adapter, "is_available", lambda: True)
    monkeypatch.setattr(adapter, "_download_history_if_needed", lambda *a: pytest.fail("inline history download"))
    frame = pd.DataFrame([{"open": 10, "high": 12, "low": 9, "close": 11, "volume": 100}],
                         index=pd.to_datetime(["2024-01-02"]))
    monkeypatch.setattr(module, "_get_xtdata", lambda: SimpleNamespace(get_market_data_ex=lambda **kw: {"000001.SZ": frame}))
    assert adapter.get_kline("000001", download_history=False)[0]["close"] == 11


def test_lifecycle_restart_recreates_closed_workers(monkeypatch):
    from core.market_runtime import isolated_providers as module
    instances = {name: worker() for name in ("qmt", "baostock", "qmt_download")}
    monkeypatch.setattr(module, "_workers", instances)
    # close_workers resolves the process module's registry; replace it for this test.
    monkeypatch.setattr(module, "close_workers", lambda: [w.close() for w in instances.values()])
    refresh = module.HistoryRefreshQueue()
    monkeypatch.setattr(module, "_refresh", refresh)
    module.stop_isolated_providers()
    assert all(w._closed.is_set() for w in instances.values())
    module.start_isolated_providers()
    assert all(not w._closed.is_set() for w in instances.values())
    assert module._refresh is not refresh
    module.stop_isolated_providers()
