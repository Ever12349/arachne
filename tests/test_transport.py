"""Pin-IP httpx transport: connect to resolved public IPs, keep Host + SNI."""

from __future__ import annotations

import httpx
import pytest

from app.errors import ArachneError
from app.fetch import create_http_client
from app.transport import PinIPAsyncTransport, pin_request_to_ip


class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, fail_hosts: set[str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_hosts = fail_hosts or set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host in self.fail_hosts:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, request=request, content=b"ok")

    async def aclose(self) -> None:
        return None


def test_pin_request_keeps_host_and_sets_sni():
    request = httpx.Request("GET", "https://example.com:8443/path")
    pinned = pin_request_to_ip(request, "1.2.3.4", "example.com")
    assert pinned.url.host == "1.2.3.4"
    assert pinned.url.port == 8443
    assert pinned.headers["host"] == "example.com:8443"
    assert pinned.extensions["sni_hostname"] == "example.com"


def test_pin_request_http_has_no_sni():
    request = httpx.Request("GET", "http://example.com/path")
    pinned = pin_request_to_ip(request, "1.2.3.4", "example.com")
    assert pinned.headers["host"] == "example.com"
    assert "sni_hostname" not in pinned.extensions


@pytest.mark.asyncio
async def test_tries_next_public_ip(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["1.2.3.4", "5.6.7.8"]

    monkeypatch.setattr("app.transport.resolve_host_ips", fake_resolve)
    inner = _ScriptedTransport(fail_hosts={"1.2.3.4"})
    transport = PinIPAsyncTransport(transport=inner)
    request = httpx.Request("GET", "https://example.com/")
    response = await transport.handle_async_request(request)
    assert response.status_code == 200
    assert [req.url.host for req in inner.requests] == ["1.2.3.4", "5.6.7.8"]
    assert inner.requests[-1].headers["host"] == "example.com"
    assert inner.requests[-1].extensions["sni_hostname"] == "example.com"


@pytest.mark.asyncio
async def test_skips_blocked_ips(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.1", "10.0.0.1", "8.8.8.8"]

    monkeypatch.setattr("app.transport.resolve_host_ips", fake_resolve)
    inner = _ScriptedTransport()
    transport = PinIPAsyncTransport(transport=inner)
    request = httpx.Request("GET", "https://example.com/")
    await transport.handle_async_request(request)
    assert [req.url.host for req in inner.requests] == ["8.8.8.8"]


@pytest.mark.asyncio
async def test_all_blocked_is_bad_url(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.1", "10.1.2.3"]

    monkeypatch.setattr("app.transport.resolve_host_ips", fake_resolve)
    transport = PinIPAsyncTransport(transport=_ScriptedTransport())
    request = httpx.Request("GET", "https://example.com/")
    with pytest.raises(ArachneError) as exc:
        await transport.handle_async_request(request)
    assert exc.value.code == "bad_url"


def test_create_http_client_uses_pin_transport():
    client = create_http_client()
    try:
        assert isinstance(client._transport, PinIPAsyncTransport)
    finally:
        # close() is sync-safe enough for construction check
        pass
