"""ARACHNE_HTTP_PROXY selects the httpx proxy transport and the Playwright proxy."""

from __future__ import annotations

import logging
import sys
import types

import httpx
import pytest

from app.errors import ArachneError
from app.fetch import create_http_client, ssrf_request_hook
from app.proxy_util import playwright_proxy, redact_proxy_url
from app.transport import PinIPAsyncTransport


def _proxy_host(client: httpx.AsyncClient) -> str | None:
    for transport in client._mounts.values():
        pool = getattr(transport, "_pool", None)
        url = getattr(pool, "_proxy_url", None)
        if url is None:
            continue
        host = url.host.decode() if isinstance(url.host, bytes) else str(url.host)
        return host
    return None


async def test_empty_proxy_uses_pin_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.HTTP_PROXY", "   ")
    client = create_http_client()
    try:
        assert isinstance(client._transport, PinIPAsyncTransport)
        assert client._mounts == {}
        assert ssrf_request_hook in client.event_hooks["request"]
    finally:
        await client.aclose()


async def test_proxy_skips_pin_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.HTTP_PROXY", "http://proxy.example:8080")
    client = create_http_client()
    try:
        assert not isinstance(client._transport, PinIPAsyncTransport)
        assert all(not isinstance(transport, PinIPAsyncTransport) for transport in client._mounts.values())
        assert _proxy_host(client) == "proxy.example"
        assert ssrf_request_hook in client.event_hooks["request"]
        assert client.trust_env is False
    finally:
        await client.aclose()


async def test_proxy_userinfo_redacted_and_closed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setattr("app.config.HTTP_PROXY", "http://alice:s3cret@proxy.example:8080")
    with caplog.at_level(logging.INFO, logger="arachne"):
        client = create_http_client()
    try:
        assert "s3cret" not in caplog.text
        assert "alice" not in caplog.text
        assert redact_proxy_url("http://alice:s3cret@proxy.example:8080") == "http://***:***@proxy.example:8080"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "value",
    [
        "not a url",
        "ftp://proxy.example:8080",
        "http://",
        "socks5://127.0.0.1:1080",
        "http://alice:s3cret",
    ],
)
def test_invalid_proxy_is_fetch_failed(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setattr("app.config.HTTP_PROXY", value)
    with pytest.raises(ArachneError) as exc:
        create_http_client()
    assert exc.value.code == "fetch_failed"
    assert "Invalid ARACHNE_HTTP_PROXY" in exc.value.message
    blob = str(exc.value.detail)
    assert "s3cret" not in blob
    assert "alice" not in blob


def test_playwright_proxy_splits_userinfo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.HTTP_PROXY", "http://alice:s3cret@proxy.example:8080")
    assert playwright_proxy() == {
        "server": "http://proxy.example:8080",
        "username": "alice",
        "password": "s3cret",
    }


def test_playwright_proxy_off_when_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.HTTP_PROXY", "")
    assert playwright_proxy() is None


@pytest.mark.asyncio
async def test_render_launch_uses_same_proxy(monkeypatch: pytest.MonkeyPatch, public_dns):
    monkeypatch.setattr("app.config.HTTP_PROXY", "http://alice:s3cret@proxy.example:8080")
    launched: dict[str, object] = {}

    class _Response:
        status = 200
        headers = {"content-type": "text/html"}

    class _Page:
        url = "https://example.com/"

        async def goto(self, url, wait_until, timeout):
            return _Response()

        async def content(self):
            return "<html><title>t</title></html>"

    class _Context:
        async def add_cookies(self, cookies):
            return None

        async def new_page(self):
            return _Page()

    class _Browser:
        async def new_context(self, **kwargs):
            return _Context()

        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kwargs):
            launched.update(kwargs)
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    def async_playwright():
        return _Playwright()

    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    api.async_playwright = async_playwright
    pkg.async_api = api
    saved = {name: sys.modules.get(name) for name in ("playwright", "playwright.async_api")}
    sys.modules["playwright"] = pkg
    sys.modules["playwright.async_api"] = api
    try:
        from app.render import render_url

        page = await render_url("https://example.com/")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    assert page.status_code == 200
    assert launched["proxy"] == {
        "server": "http://proxy.example:8080",
        "username": "alice",
        "password": "s3cret",
    }
