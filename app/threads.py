"""Dedicated pool for extract/suggest CPU work and session fsync."""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

CPU_POOL_WORKERS = 4

_executor: ThreadPoolExecutor | None = None


def start_cpu_pool(max_workers: int = CPU_POOL_WORKERS) -> ThreadPoolExecutor:
    """Start the process-wide pool once. Later calls return the same executor."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="arachne-cpu",
        )
    return _executor


async def shutdown_cpu_pool() -> None:
    """Wait for in-flight work, then drop the process-wide pool."""
    global _executor
    executor = _executor
    _executor = None
    if executor is None:
        return
    await asyncio.to_thread(executor.shutdown, True)


async def run_in_cpu_pool(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run a sync callable off the event loop, copying contextvars like to_thread.

    Uses the dedicated pool when it has been started; otherwise the loop default
    executor, so unit tests need not boot the pool.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    wrapped = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(_executor, wrapped)
