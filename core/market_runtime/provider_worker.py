"""Private SDK worker entry point. No cache/database writes or trading operations."""
import os
from pathlib import Path
import socket
import sys
from time import monotonic, sleep

from .process_worker import read_message, write_message


def create_adapter(provider, config):
    if provider.startswith("qmt"):
        from app.services.data_sources.qmt_adapter import QMTAdapter
        adapter = QMTAdapter()
        # Only attach to the existing terminal; never start a QMT service here.
        values = {**config, "QMT_START_LOCAL_SERVICE": False}
        from app.core.config import settings
        settings.QMT_UNIFIED_ENABLED = adapter._parse_bool(values.get("QMT_UNIFIED_ENABLED", False))
        adapter._get_runtime_setting = lambda name, default=None: values.get(name, default)
        return adapter
    if provider == "baostock":
        from app.services.data_sources.baostock_adapter import BaoStockAdapter
        return BaoStockAdapter()
    raise ValueError("Unsupported market provider")


def execute(adapter, provider, request):
    remaining = request["deadline"] - monotonic()
    if remaining <= 0:
        raise TimeoutError("行情请求已过期")
    if provider == "baostock":
        # Safe here: this process is dedicated to one serial BaoStock session.
        socket.setdefaulttimeout(min(8, remaining))
    if not adapter.is_available():
        raise RuntimeError(f"{provider} 不可用")
    operation, args = request["operation"], request["arguments"]
    if provider == "qmt_download" and operation == "download":
        from app.services.data_sources.qmt_adapter import _normalize_code
        period = {"day": "1d", "week": "1w", "month": "1mon", "5m": "5m", "30m": "30m", "15m": "15m", "60m": "1h"}[args["period"]]
        if not adapter._download_history_if_needed(_normalize_code(args["code"]), period):
            raise RuntimeError("QMT 历史下载未完成")
        return {"downloaded": True}
    if operation != "kline" or provider == "qmt_download":
        raise ValueError("Unsupported market operation")
    if provider == "qmt":
        args = {**args, "download_history": False}
    return {"rows": adapter.get_kline(**args)}


def serve(provider, directory):
    config = read_message(directory / "config.json")
    adapter = create_adapter(provider, config)
    parent_pid = os.getppid()
    idle_since = monotonic()
    while os.getppid() == parent_pid and monotonic() - idle_since < 120:
        path = directory / "request.json"
        if not directory.exists():
            return
        if not path.exists():
            sleep(0.02)
            continue
        request = read_message(path)
        path.unlink()
        try:
            result = execute(adapter, provider, request)
            response = {"id": request["id"], "ok": True, "result": result}
        except Exception as exc:
            response = {"id": request["id"], "ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
        # A late result cannot become a successful response or write parent caches.
        if monotonic() >= request["deadline"]:
            response = {"id": request["id"], "ok": False, "error": "行情请求已过期"}
        write_message(directory / "response.json", response)
        idle_since = monotonic()


if __name__ == "__main__":
    serve(sys.argv[1], Path(sys.argv[2]))
