"""Bounded TTL cache with single-flight loading and defensive result copies."""
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from threading import BoundedSemaphore, Lock
from time import monotonic


class BusyError(RuntimeError):
    pass


class SingleFlightCache:
    def __init__(self, maxsize=64, ttl=180, max_inflight=64):
        self.maxsize, self.ttl, self.max_inflight = maxsize, ttl, max_inflight
        self._values = OrderedDict()
        self._inflight = {}
        self._guard = Lock()
        self._generation = 0

    def clear(self):
        with self._guard:
            self._values.clear()
            self._generation += 1

    def _reserve(self, key):
        with self._guard:
            now = monotonic()
            for expired in [k for k, (deadline, _) in self._values.items() if deadline <= now]:
                del self._values[expired]
            if key in self._values:
                self._values.move_to_end(key)
                future = Future()
                future.set_result(deepcopy(self._values[key][1]))
                return future, None
            flight_key = (self._generation, key)
            if flight_key in self._inflight:
                return self._inflight[flight_key], None
            if len(self._inflight) >= self.max_inflight:
                raise BusyError("行情请求繁忙，请稍后重试")
            future = Future()
            future.set_running_or_notify_cancel()  # A waiter cannot cancel other waiters' work.
            self._inflight[flight_key] = future
            return future, flight_key

    def _load(self, future, flight_key, loader, cacheable):
        try:
            value = loader()
            saved = deepcopy(value)
            with self._guard:
                generation, key = flight_key
                if generation == self._generation and cacheable(value):
                    self._values[key] = (monotonic() + self.ttl, saved)
                    self._values.move_to_end(key)
                    while len(self._values) > self.maxsize:
                        self._values.popitem(last=False)
            future.set_result(saved)
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            with self._guard:
                self._inflight.pop(flight_key, None)

    def get_or_load(self, key, loader, cacheable=lambda value: True, timeout=None):
        future, flight_key = self._reserve(key)
        if flight_key is not None:
            self._load(future, flight_key, loader, cacheable)
        return deepcopy(future.result(timeout=timeout))

    def submit(self, pool, key, loader, cacheable=lambda value: True):
        """Coalesce before acquiring a worker, including for requests still in flight."""
        future, flight_key = self._reserve(key)
        if flight_key is not None:
            try:
                pool.submit(self._load, future, flight_key, loader, cacheable)
            except BaseException as exc:
                with self._guard:
                    self._inflight.pop(flight_key, None)
                future.set_exception(exc)
        return future


class BoundedExecutor:
    """No unbounded queue; a running timeout retains its slot until it exits."""
    def __init__(self, workers=8):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-bars")
        self._slots = BoundedSemaphore(workers)

    def submit(self, fn, *args):
        if not self._slots.acquire(blocking=False):
            raise BusyError("行情取数线程已满，本周期暂不可用")
        try:
            future = self._pool.submit(copy_context().run, fn, *args)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        return future


MARKET_IO_POOL = BoundedExecutor(workers=8)
