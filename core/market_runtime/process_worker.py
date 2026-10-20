"""Supervise one serial SDK process with bounded admission and replaceable IPC.

Atomic JSON files avoid blocking on a half-written pipe frame after a native SDK
hang. Each process generation owns a private directory; it is discarded on reap.
"""
import atexit
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore, Event, Lock
from time import monotonic
from uuid import uuid4

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class ProviderUnavailable(RuntimeError):
    pass


def write_message(path, message):
    data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("行情进程消息超过大小上限")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def read_message(path):
    if path.stat().st_size > MAX_MESSAGE_BYTES:
        raise ValueError("行情进程消息超过大小上限")
    return json.loads(path.read_text(encoding="utf-8"))


class ProviderProcess:
    def __init__(self, provider, *, queue_size=4, cooldown=30, failure_limit=2, command=None):
        self.provider = provider
        self._admission = BoundedSemaphore(queue_size + 1)
        self._lock = Lock()
        self._closed = Event()
        self._process = None
        self._directory = None
        self._config = None
        self._broken = False
        self._failures = 0
        self._retry_at = 0.0
        self._cooldown, self._failure_limit = cooldown, failure_limit
        self._command = command

    @property
    def pid(self):
        return self._process.pid if self._process and self._process.poll() is None else None

    def _reap(self):
        self._broken = True
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=0.4)
                    except subprocess.TimeoutExpired:
                        # Retain the handle and refuse replacements until exit is verified.
                        raise ProviderUnavailable("行情子进程尚未退出，暂不创建替代进程")
            else:
                process.wait()
        self._process = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
        self._config = None
        self._broken = False

    def _start(self, config):
        encoded = json.dumps(config, sort_keys=True, default=str)
        if self._broken:
            self._reap()
        if self._process is not None and (self._process.poll() is not None or encoded != self._config):
            self._reap()
        if self._process is not None:
            return
        self._directory = tempfile.TemporaryDirectory(prefix="market-sdk-")
        directory = Path(self._directory.name)
        write_message(directory / "config.json", config)
        command = self._command or [sys.executable, "-m", "core.market_runtime.provider_worker"]
        try:
            self._process = subprocess.Popen(
                [*command, self.provider, str(directory)],
                cwd=str(Path(__file__).resolve().parents[2]),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._config = encoded
        except BaseException:
            self._reap()
            raise

    def call(self, operation, arguments, *, config, timeout):
        # Queue time, cold SDK import and execution all consume the same budget.
        deadline = monotonic() + timeout
        if self._closed.is_set():
            raise ProviderUnavailable("行情工作进程已关闭")
        if not self._admission.acquire(blocking=False):
            raise ProviderUnavailable(f"{self.provider} 等待队列已满")
        acquired = False
        try:
            acquired = self._lock.acquire(timeout=max(0, deadline - monotonic()))
            if not acquired:
                raise TimeoutError(f"{self.provider} 排队超时")
            if self._closed.is_set():
                raise ProviderUnavailable("行情工作进程已关闭")
            if monotonic() < self._retry_at:
                raise ProviderUnavailable(f"{self.provider} 暂停调用，等待恢复探测")
            # Reserve up to half a second for terminating/reaping the process.
            execution_deadline = deadline - min(0.5, timeout / 4)
            if monotonic() >= execution_deadline:
                raise TimeoutError(f"{self.provider} 排队耗尽时间预算")
            try:
                self._start(config)
                directory = Path(self._directory.name)
                response_path = directory / "response.json"
                response_path.unlink(missing_ok=True)
                request_id = uuid4().hex
                write_message(directory / "request.json", {
                    "id": request_id, "operation": operation, "arguments": arguments,
                    "deadline": execution_deadline,
                })
                while monotonic() < execution_deadline and not self._closed.is_set():
                    if response_path.exists():
                        response = read_message(response_path)
                        response_path.unlink()
                        if response.get("id") != request_id:
                            raise ProviderUnavailable("行情子进程返回了过期响应")
                        if not response.get("ok"):
                            raise ProviderUnavailable(response.get("error") or "行情子进程调用失败")
                        if monotonic() >= execution_deadline:
                            raise TimeoutError("行情结果超过截止时间")
                        self._failures = 0
                        return response["result"]
                    if self._process.poll() is not None:
                        raise ProviderUnavailable(f"{self.provider} 工作进程异常退出")
                    self._closed.wait(min(0.02, max(0, execution_deadline - monotonic())))
                raise TimeoutError(f"{self.provider} 调用超时，已请求回收工作进程")
            except BaseException:
                self._failures += 1
                if self._failures >= self._failure_limit:
                    self._retry_at = monotonic() + self._cooldown
                if not self._broken:
                    self._reap()
                raise
        finally:
            if acquired:
                self._lock.release()
            self._admission.release()

    def close(self):
        self._closed.set()
        if self._lock.acquire(timeout=1):
            try:
                self._reap()
            finally:
                self._lock.release()


_workers = {name: ProviderProcess(name) for name in ("qmt", "baostock", "qmt_download")}


def close_workers():
    for worker in _workers.values():
        try:
            worker.close()
        except Exception as exc:
            logging.getLogger(__name__).warning("行情工作进程清理失败: %s", exc)


atexit.register(close_workers)
