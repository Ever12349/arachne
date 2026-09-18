"""Fetch layer: body cap, timeout, content-type helpers."""

from __future__ import annotations

import httpx
import pytest

from app.config import MAX_BODY_BYTES
from app.errors import ArachneError
from app.fetch import fetch_url, is_html_content_type, ssrf_request_hook


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        max_redirects=20,
        timeout=httpx.Timeout(connect=5, read=15, write=15, pool=5),
        event_hooks={"request": [ssrf_request_hook]},
    )


def test_html_content_types():
    assert is_html_content_type("") is True
    assert is_html_content_type("text/html; charset=utf-8") is True
    assert is_html_content_type("application/xhtml+xml") is True
    assert is_html_content_type("application/pdf") is False
    assert is_html_content_type("image/png") is False


@pytest.mark.asyncio
async def test_too_large_from_content_length(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"tiny",
            headers={
                "content-type": "text/html",
                "content-length": str(MAX_BODY_BYTES + 1),
            },
            request=request,
        )

    async with _client(handler) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/")
    assert exc.value.code == "too_large"


@pytest.mark.asyncio
async def test_too_large_from_streamed_body(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = b"a" * (MAX_BODY_BYTES + 50)
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/")
    assert exc.value.code == "too_large"


@pytest.mark.asyncio
async def test_timeout_mapped(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    async with _client(handler) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/")
    assert exc.value.code == "timeout"


@pytest.mark.asyncio
async def test_connect_error_is_fetch_failed(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/")
    assert exc.value.code == "fetch_failed"


@pytest.mark.asyncio
async def test_fetch_success_returns_final_url(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    async with _client(handler) as client:
        page = await fetch_url(client, "https://example.com/start")
    assert page.status_code == 200
    assert "text/html" in page.content_type
    assert "Ok" in page.body
