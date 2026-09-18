"""Global extract QPS (fixed window), concurrency, and /stats counters."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable

from app.config import MAX_CONCURRENCY, QPS


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
    """In-process counters for GET /stats. Extract requests only."""

    def __init__(self) -> None:
        self.requests_total = 0
        self.errors_by_code: dict[str, int] = defaultdict(int)
        self.cache_hits = 0
        self.cache_misses = 0
        self.in_flight = 0
        self.latency_ms_sum = 0.0
        self.latency_ms_count = 0

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
