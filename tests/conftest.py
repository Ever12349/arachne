"""Shared fixtures for unit tests (no live network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.fetch import ssrf_request_hook
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_HTML = (FIXTURES / "sample.html").read_text(encoding="utf-8")
EMPTY_HTML = (FIXTURES / "empty.html").read_text(encoding="utf-8")

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch):
    """Resolve hostnames without touching DNS; IP literals stay as-is."""
    import ipaddress

    async def fake_resolve(host: str, port: int) -> list[str]:
        stripped = host.strip("[]")
        try:
            ipaddress.ip_address(stripped)
            return [stripped]
        except ValueError:
            pass
        if host.lower() in {"localhost"}:
            return ["127.0.0.1"]
        return [PUBLIC_IP]

    monkeypatch.setattr("app.ssrf.resolve_host_ips", fake_resolve)
    return fake_resolve


def _handler_for(
    *,
    status: int = 200,
    body: str = SAMPLE_HTML,
    content_type: str = "text/html; charset=utf-8",
    extra_headers: dict[str, str] | None = None,
    raise_exc: BaseException | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        headers = {"content-type": content_type}
        if extra_headers:
            headers.update(extra_headers)
        return httpx.Response(status, content=body.encode("utf-8"), headers=headers, request=request)

    return handler


@pytest.fixture
def mock_transport_factory():
    def factory(**kwargs) -> httpx.MockTransport:
        return httpx.MockTransport(_handler_for(**kwargs))

    return factory


@pytest.fixture
def api_client(public_dns, monkeypatch: pytest.MonkeyPatch):
    """TestClient whose upstream HTTP is mocked to return SAMPLE_HTML."""

    def fake_create() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler_for()),
            follow_redirects=True,
            max_redirects=20,
            timeout=httpx.Timeout(connect=5, read=15, write=15, pool=5),
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    with TestClient(app) as client:
        yield client
