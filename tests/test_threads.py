"""CPU pool: contextvars, ArachneError passthrough, semaphore released before extract."""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.cache import ResultCache
from app.errors import ArachneError, extract_empty
from app.fetch import FetchedPage
from app.limits import FixedWindowRateLimiter, Stats
from app.logging_setup import request_id_var
from app.models import ExtractResponse, PageMetadata
from app.service import run_extract
from app.threads import run_in_cpu_pool, shutdown_cpu_pool, start_cpu_pool


def _page(url: str = "https://example.com/") -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type="text/html",
        body="<html><head><title>T</title></head><body><p>hello</p></body></html>",
    )


def _response(text: str = "hello") -> ExtractResponse:
    return ExtractResponse(
        url="https://example.com/",
        requested_url="https://example.com/",
        status_code=200,
        title="T",
        main_text=text,
        metadata=PageMetadata(),
        links=[],
        truncated=False,
    )


@pytest.mark.asyncio
async def test_run_in_cpu_pool_copies_request_id():
    start_cpu_pool()
    try:
        token = request_id_var.set("rid-from-test")
        try:

            def read_id() -> str:
                return request_id_var.get()

            assert await run_in_cpu_pool(read_id) == "rid-from-test"
        finally:
            request_id_var.reset(token)
    finally:
        await shutdown_cpu_pool()


@pytest.mark.asyncio
async def test_pool_arachne_error_is_not_rewritten_to_internal(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_ready(url: str, *_args, **_kwargs):
        return _page(url), 0, None

    def boom(*_args, **_kwargs):
        raise extract_empty("empty from pool")

    monkeypatch.setattr("app.service.fetch_ready_page", fake_ready)
    monkeypatch.setattr("app.service.extract_with_profile", boom)
    with pytest.raises(ArachneError) as exc:
        await run_extract(
            url="https://example.com/",
            client=None,  # type: ignore[arg-type]
            cache=ResultCache(),
            limiter=FixedWindowRateLimiter(qps=100),
            semaphore=asyncio.Semaphore(1),
            stats=Stats(),
        )
    assert exc.value.code == "extract_empty"
    assert exc.value.message == "empty from pool"


@pytest.mark.asyncio
async def test_semaphore_released_before_extract_cpu(monkeypatch: pytest.MonkeyPatch):
    fetches = {"n": 0}
    entered = threading.Event()
    release = threading.Event()
    second_fetch = threading.Event()

    async def fake_ready(url: str, *_args, **_kwargs):
        fetches["n"] += 1
        if fetches["n"] >= 2:
            second_fetch.set()
        return _page(url), 0, None

    def slow_extract_response(*_args, **_kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("extract was not released")
        return _response()

    monkeypatch.setattr("app.service.fetch_ready_page", fake_ready)
    monkeypatch.setattr("app.service._extract_response", slow_extract_response)
    start_cpu_pool()
    try:
        sem = asyncio.Semaphore(1)
        kwargs = dict(
            client=None,
            cache=ResultCache(),
            limiter=FixedWindowRateLimiter(qps=100),
            semaphore=sem,
            stats=Stats(),
        )
        first = asyncio.create_task(run_extract(url="https://example.com/a", **kwargs))  # type: ignore[arg-type]
        assert await asyncio.to_thread(entered.wait, 2), "first extract never started"
        second = asyncio.create_task(run_extract(url="https://example.com/b", **kwargs))  # type: ignore[arg-type]
        assert await asyncio.to_thread(second_fetch.wait, 2), f"second fetch did not start, n={fetches['n']}"
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.response.main_text == "hello"
        assert second_result.response.main_text == "hello"
    finally:
        release.set()
        await shutdown_cpu_pool()
