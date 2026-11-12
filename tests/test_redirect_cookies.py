"""Per-fetch cookie jar and manual redirect following."""

from __future__ import annotations

import httpx
import pytest

from app.config import MAX_BODY_BYTES, MAX_REDIRECTS, USER_AGENT
from app.errors import ArachneError
from app.fetch import _NoStoreCookies, create_http_client, fetch_url, ssrf_request_hook


def _client(handler) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        max_redirects=20,
        timeout=httpx.Timeout(connect=5, read=15, write=15, pool=5),
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [ssrf_request_hook]},
    )
    client._cookies = _NoStoreCookies()
    return client


@pytest.mark.asyncio
async def test_set_cookie_does_not_leak_across_fetches(public_dns):
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        if request.url.path == "/first":
            return httpx.Response(
                200,
                content=b"<html><title>One</title></html>",
                headers={
                    "content-type": "text/html",
                    "set-cookie": "sess=from-first; Path=/",
                },
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Two</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(client, "https://example.com/first")
        await fetch_url(client, "https://example.com/second")
    assert seen[0] is None
    assert seen[1] is None
    assert client.cookies.get("sess") is None


@pytest.mark.asyncio
async def test_caller_cookies_follow_same_host_redirect(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers.get("cookie")
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "https://example.com/end", "content-type": "text/html"},
                content=b"go",
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        page = await fetch_url(
            client,
            "https://example.com/start",
            cookies={"sid": "fromfield"},
        )
    assert page.final_url == "https://example.com/end"
    assert page.status_code == 200
    assert seen["https://example.com/start"] == "sid=fromfield"
    assert seen["https://example.com/end"] == "sid=fromfield"


@pytest.mark.asyncio
async def test_caller_cookies_not_sent_to_other_host(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("cookie")
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"location": "https://other.example/end"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(
            client,
            "https://example.com/start",
            cookies={"sid": "fromfield"},
        )
    assert seen["example.com"] == "sid=fromfield"
    assert seen["other.example"] is None


@pytest.mark.asyncio
async def test_caller_cookies_not_sent_to_www(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("cookie")
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"location": "https://www.example.com/end"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(
            client,
            "https://example.com/start",
            cookies={"sid": "fromfield"},
        )
    assert seen["example.com"] == "sid=fromfield"
    assert seen["www.example.com"] is None


@pytest.mark.asyncio
async def test_set_cookie_with_domain_can_go_to_www(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("cookie")
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://www.example.com/end",
                    "set-cookie": "sess=abc; Domain=example.com; Path=/",
                },
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(client, "https://example.com/start")
    assert seen["example.com"] is None
    assert seen["www.example.com"] == "sess=abc"
    assert client.cookies.get("sess") is None


@pytest.mark.asyncio
async def test_http_to_https_same_host_keeps_caller_cookies(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers.get("cookie")
        if request.url.scheme == "http":
            return httpx.Response(
                301,
                headers={"location": "https://example.com/end"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        page = await fetch_url(
            client,
            "http://example.com/start",
            cookies={"sid": "fromfield"},
        )
    assert page.final_url == "https://example.com/end"
    assert seen["http://example.com/start"] == "sid=fromfield"
    assert seen["https://example.com/end"] == "sid=fromfield"


@pytest.mark.asyncio
async def test_https_to_http_same_host_drops_caller_cookies(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers.get("cookie")
        if request.url.scheme == "https":
            return httpx.Response(
                302,
                headers={"location": "http://example.com/end"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(
            client,
            "https://example.com/start",
            cookies={"sid": "fromfield"},
        )
    assert seen["https://example.com/start"] == "sid=fromfield"
    assert seen["http://example.com/end"] is None


@pytest.mark.asyncio
async def test_empty_location_is_final_response(public_dns):
    hops = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hops["n"] += 1
        return httpx.Response(
            302,
            content=b"<html><title>Stay</title></html>",
            headers={"content-type": "text/html", "location": "  "},
            request=request,
        )

    async with _client(handler) as client:
        page = await fetch_url(client, "https://example.com/start")
    assert hops["n"] == 1
    assert page.status_code == 302
    assert "Stay" in page.body


@pytest.mark.asyncio
async def test_too_many_redirects(public_dns):
    hops = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hops["n"] += 1
        return httpx.Response(
            302,
            headers={"location": f"https://example.com/h{hops['n']}"},
            request=request,
        )

    async with _client(handler) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/start")
    assert exc.value.code == "fetch_failed"
    assert exc.value.detail["max_redirects"] == MAX_REDIRECTS
    # Initial request plus 20 followed redirects; the 21st redirect is not fetched.
    assert hops["n"] == MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_redirect_body_is_not_capped(public_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                content=b"x" * (MAX_BODY_BYTES + 50),
                headers={
                    "location": "https://example.com/end",
                    "content-length": str(MAX_BODY_BYTES + 50),
                    "content-type": "text/html",
                },
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        page = await fetch_url(client, "https://example.com/start")
    assert page.status_code == 200
    assert "Ok" in page.body


@pytest.mark.asyncio
async def test_authorization_dropped_on_cross_host_redirect(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"location": "https://other.example/end"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(
            client,
            "https://example.com/start",
            headers={"Authorization": "Bearer tok"},
        )
    assert seen["example.com"] == "Bearer tok"
    assert seen["other.example"] is None


@pytest.mark.asyncio
async def test_authorization_kept_on_http_to_https_upgrade(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url)] = request.headers.get("authorization")
        if request.url.scheme == "http":
            return httpx.Response(
                301,
                headers={"location": "https://example.com/end"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(
            client,
            "http://example.com/start",
            headers={"Authorization": "Bearer tok"},
        )
    assert seen["http://example.com/start"] == "Bearer tok"
    assert seen["https://example.com/end"] == "Bearer tok"


@pytest.mark.asyncio
async def test_set_cookie_overwrites_same_name_on_later_hop(public_dns):
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers.get("cookie")
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "location": "https://example.com/end",
                    "set-cookie": "sid=new; Path=/",
                },
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<html><title>Ok</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async with _client(handler) as client:
        await fetch_url(
            client,
            "https://example.com/start",
            cookies={"sid": "old"},
        )
    assert seen["/start"] == "sid=old"
    assert seen["/end"] == "sid=new"


@pytest.mark.asyncio
async def test_create_http_client_disables_auto_follow_and_cookie_store():
    client = create_http_client()
    try:
        assert client.follow_redirects is False
        assert type(client.cookies).__name__ == "_NoStoreCookies"
    finally:
        await client.aclose()
