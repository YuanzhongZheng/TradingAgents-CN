"""K-line isolation and a separately bounded QMT history refresh queue."""
import atexit
from collections import OrderedDict
import json
import logging
import math
import os
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic

from .context import request_timeout
from .process_worker import ProviderProcess, ProviderUnavailable, _workers, close_workers

logger = logging.getLogger(__name__)


def provider_config(provider):
    from app.core.config import settings
    if provider != "qmt":
        return {}
    return {key: os.environ.get(key, value) for key, value in settings.model_dump(mode="json").items()
            if key.startswith("QMT_")}


def enabled(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def validate_rows(rows):
    if rows is None:
        return None
    if not isinstance(rows, list) or len(rows) > 100000:
        raise ProviderUnavailable("行情子进程返回格式不正确")
    for row in rows:
        if not isinstance(row, dict) or not (row.get("time") or row.get("date")):
            raise ProviderUnavailable("行情子进程返回缺少日期")
        for field in ("open", "high", "low", "close"):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ProviderUnavailable("行情子进程返回无效价格")
        if row["high"] < row["low"]:
            raise ProviderUnavailable("行情子进程价格范围不正确")
    return rows


class HistoryRefreshQueue:
    """One daemon supervisor, one SDK process, at most eight pending refreshes."""
    def __init__(self):
        self._queue = Queue(maxsize=8)
        self._guard = Lock()
        self._pending = set()
        self._recent = OrderedDict()
        self._stop = Event()
        self._thread = None

    def schedule(self, arguments, config):
        key = json.dumps([arguments, config], sort_keys=True, default=str)
        with self._guard:
            if self._stop.is_set() or key in self._pending:
                return False
            if monotonic() - self._recent.get(key, -float("inf")) < 300:
                return False
            self._pending.add(key)
            try:
                self._queue.put_nowait((key, arguments, config))
            except Full:
                self._pending.remove(key)
                return False
            if self._thread is None:
                self._thread = Thread(target=self._run, name="qmt-history-supervisor", daemon=True)
                self._thread.start()
            return True

    def _run(self):
        while not self._stop.is_set():
            try:
                key, arguments, config = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                from app.core.config import settings
                _workers["qmt_download"].call("download", arguments, config=config,
                    timeout=settings.MARKET_QMT_DOWNLOAD_TIMEOUT_SECONDS)
                with self._guard:
                    self._recent[key] = monotonic()
                    self._recent.move_to_end(key)
                    while len(self._recent) > 64:
                        self._recent.popitem(last=False)
            except Exception as exc:
                logger.warning("QMT 后台历史下载失败: %s", exc)
            finally:
                with self._guard:
                    self._pending.discard(key)
                self._queue.task_done()

    def close(self):
        self._stop.set()
        try:
            _workers["qmt_download"].close()
        finally:
            if self._thread is not None:
                self._thread.join(timeout=1)
            while True:
                try:
                    key, _, _ = self._queue.get_nowait()
                except Empty:
                    break
                with self._guard:
                    self._pending.discard(key)
                self._queue.task_done()


_refresh = HistoryRefreshQueue()


def start_isolated_providers():
    global _refresh
    for name, worker in list(_workers.items()):
        if worker._closed.is_set() and worker.pid is None:
            worker.close()
            _workers[name] = ProviderProcess(name)
    if _refresh._stop.is_set():
        _refresh = HistoryRefreshQueue()


def stop_isolated_providers():
    try:
        _refresh.close()
    finally:
        close_workers()


atexit.register(stop_isolated_providers)


def get_isolated_kline(provider, arguments):
    config = provider_config(provider)
    if provider == "qmt" and not enabled(config.get("QMT_UNIFIED_ENABLED", False)):
        return None
    if provider == "baostock" and arguments["period"] not in {"day", "week", "month"}:
        return None
    response = _workers[provider].call("kline", arguments, config=config, timeout=request_timeout())
    rows = validate_rows(response["rows"])
    if provider == "qmt" and enabled(config.get("QMT_AUTO_DOWNLOAD_HISTORY", True)):
        # Refresh is for subsequent analyses; never change the current frozen snapshot.
        _refresh.schedule({"code": arguments["code"], "period": arguments["period"]}, config)
    return rows
