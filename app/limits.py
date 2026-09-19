"""Global extract QPS (fixed window), concurrency, and /stats counters."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable

from app.config import MAX_CONCURRENCY, QPS
from app.metrics import (
    CACHE_HITS_TOTAL,
    CACHE_MISSES_TOTAL,
    ERRORS_TOTAL,
    IN_FLIGHT,
    REQUEST_LATENCY_SECONDS,
    REQUESTS_TOTAL,
)


class FixedWindowRateLimiter:
    """Allow up to `qps` extract requests per fixed `window_seconds` interval."""

    def __init__(
        self,
        qps: int = QPS,
        window_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.qps = qps
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._window_start: float | None = None
        self._count = 0

    async def try_acquire(self) -> bool:
        async with self._lock:
            now = self._clock()
            if self._window_start is None or now - self._window_start >= self.window_seconds:
                self._window_start = now
                self._count = 0
            if self._count >= self.qps:
                return False
            self._count += 1
            return True


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

    def snapshot(self) -> dict[str, object]:
        return {
            "requests_total": self.requests_total,
            "errors_by_code": dict(self.errors_by_code),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "in_flight": self.in_flight,
            "latency_ms_sum": self.latency_ms_sum,
            "latency_ms_count": self.latency_ms_count,
        }
