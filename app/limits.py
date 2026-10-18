"""Global extract QPS (fixed window), concurrency, and /stats counters."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from app import config
from app.config import MAX_CONCURRENCY, QPS
from app.errors import ArachneError, rate_limited
from app.metrics import (
    AUTO_RENDERS_TOTAL,
    CACHE_HITS_TOTAL,
    CACHE_MISSES_TOTAL,
    ERRORS_TOTAL,
    IN_FLIGHT,
    QPS_WAIT_SECONDS,
    QPS_WAITS_TOTAL,
    REQUEST_LATENCY_SECONDS,
    REQUESTS_TOTAL,
)

SleepFn = Callable[[float], Awaitable[None]]


def normalize_qps_mode(mode: str | None) -> str:
    """`reject` is the only non-queue mode. Invalid or empty values queue."""
    if mode is not None and str(mode).strip().lower() == "reject":
        return "reject"
    return "queue"


class FixedWindowRateLimiter:
    """Allow up to `qps` requests per fixed `window_seconds` interval.

    `acquire` is the pipeline entry. In `queue` mode it sleeps until the next
    window boundary when the current window is full, up to `wait_seconds`.
    `reject` (and `wait_seconds <= 0`) fails immediately with `rate_limited`.
    """

    def __init__(
        self,
        qps: int = QPS,
        window_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        *,
        mode: str | None = None,
        wait_seconds: float | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self.qps = qps
        self.window_seconds = window_seconds
        self._clock = clock
        self.mode = normalize_qps_mode(config.QPS_MODE if mode is None else mode)
        self.wait_seconds = config.QPS_WAIT_SECONDS if wait_seconds is None else wait_seconds
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._lock = asyncio.Lock()
        self._window_start: float | None = None
        self._count = 0

    async def try_acquire(self) -> bool:
        acquired, _sleep_for = await self._reserve()
        return acquired

    async def acquire(self) -> float:
        """Take one slot. Returns milliseconds spent waiting.

        Raises `rate_limited` when the slot cannot be taken. The exception
        carries `wait_ms` (not copied into `detail`, so the HTTP body stays
        unchanged). A positive `wait_ms` means the caller already slept.
        """
        start = self._clock()
        while True:
            acquired, sleep_for = await self._reserve()
            waited_ms = max(0.0, (self._clock() - start) * 1000.0)
            if acquired:
                return waited_ms
            if self.mode != "queue" or self.wait_seconds <= 0:
                raise self._denied(waited_ms)
            elapsed = self._clock() - start
            if sleep_for <= 0:
                if elapsed > self.wait_seconds:
                    raise self._denied(waited_ms)
                continue
            if elapsed + sleep_for > self.wait_seconds:
                raise self._denied(waited_ms)
            await self._sleep(sleep_for)

    async def _reserve(self) -> tuple[bool, float]:
        """Reserve a slot or report seconds until the current window ends."""
        async with self._lock:
            now = self._clock()
            if self._window_start is None or now - self._window_start >= self.window_seconds:
                self._window_start = now
                self._count = 0
            if self._count < self.qps:
                self._count += 1
                return True, 0.0
            remaining = self.window_seconds - (now - self._window_start)
            return False, max(0.0, remaining)

    def _denied(self, waited_ms: float) -> ArachneError:
        exc = rate_limited()
        exc.wait_ms = waited_ms  # type: ignore[attr-defined]
        return exc


def extract_semaphore(limit: int = MAX_CONCURRENCY) -> asyncio.Semaphore:
    return asyncio.Semaphore(limit)


class Stats:
    """In-process counters for GET /stats. Dual-writes Prometheus metrics."""

    def __init__(self) -> None:
        self.requests_total = 0
        self.errors_by_code: dict[str, int] = defaultdict(int)
        self.cache_hits = 0
        self.cache_misses = 0
        self.in_flight = 0
        self.latency_ms_sum = 0.0
        self.latency_ms_count = 0
        self.qps_waits = 0
        self.qps_wait_ms_sum = 0.0
        self.auto_renders = 0

    def record_request(self) -> None:
        self.requests_total += 1
        REQUESTS_TOTAL.inc()

    def record_error(self, code: str) -> None:
        self.errors_by_code[code] += 1
        ERRORS_TOTAL.labels(code=code).inc()

    def record_cache_hit(self) -> None:
        self.cache_hits += 1
        CACHE_HITS_TOTAL.inc()

    def record_cache_miss(self) -> None:
        self.cache_misses += 1
        CACHE_MISSES_TOTAL.inc()

    def enter_in_flight(self) -> None:
        self.in_flight += 1
        IN_FLIGHT.inc()

    def leave_in_flight(self) -> None:
        self.in_flight -= 1
        IN_FLIGHT.dec()

    def record_latency(self, latency_ms: float) -> None:
        self.latency_ms_sum += latency_ms
        self.latency_ms_count += 1
        REQUEST_LATENCY_SECONDS.observe(latency_ms / 1000.0)

    def record_qps_wait(self, wait_ms: float) -> None:
        """Count a wait only when the caller actually slept. Sum is milliseconds."""
        if wait_ms <= 0:
            return
        self.qps_waits += 1
        self.qps_wait_ms_sum += wait_ms
        QPS_WAITS_TOTAL.inc()
        QPS_WAIT_SECONDS.observe(wait_ms / 1000.0)

    def record_auto_render(self) -> None:
        self.auto_renders += 1
        AUTO_RENDERS_TOTAL.inc()

    def snapshot(self) -> dict[str, object]:
        return {
            "requests_total": self.requests_total,
            "errors_by_code": dict(self.errors_by_code),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "in_flight": self.in_flight,
            "latency_ms_sum": self.latency_ms_sum,
            "latency_ms_count": self.latency_ms_count,
            "qps_waits": self.qps_waits,
            "qps_wait_ms_sum": self.qps_wait_ms_sum,
            "auto_renders": self.auto_renders,
        }
