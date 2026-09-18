"""Service orchestration: upstream status handling and content-type gate."""

from __future__ import annotations

import httpx
import pytest

from app.errors import ArachneError
from app.fetch import ssrf_request_hook
from app.service import extract_page
from tests.conftest import SAMPLE_HTML


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        event_hooks={"request": [ssrf_request_hook]},
    )


@pytest.mark.parametrize("status,code", [(401, "unauthorized_upstream"), (403, "unauthorized_upstream"), (404, "fetch_failed"), (500, "fetch_failed")])
@pytest.mark.asyncio
async def test_error_status_not_extracted(public_dns, status: int, code: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        with pytest.raises(ArachneError) as exc:
            await extract_page("https://example.com/", client)
    assert exc.value.code == code
    assert exc.value.detail.get("status_code") == status


@pytest.mark.asyncio
async def test_unsupported_content_type(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.4",
            headers={"content-type": "application/pdf"},
            request=request,
        )

    async with _client(handler) as client:
        with pytest.raises(ArachneError) as exc:
            await extract_page("https://example.com/file.pdf", client)
    assert exc.value.code == "unsupported_content"


@pytest.mark.asyncio
async def test_success_extract(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    async with _client(handler) as client:
        result = await extract_page("https://www.example.com/article", client)
    assert result.status_code == 200
    assert result.requested_url == "https://www.example.com/article"
    assert result.title
    assert result.main_text
