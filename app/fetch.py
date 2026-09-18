"""Async HTTP fetch with timeouts, redirect limit, body cap, and SSRF hooks."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import (
    CONNECT_TIMEOUT,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    READ_TIMEOUT,
    USER_AGENT,
)
from app.errors import ArachneError, fetch_failed, timeout_error, too_large
from app.ssrf import assert_public_http_url

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# httpx requires all four fields; connect/read are the locked P0 values.
HTTP_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT,
    read=READ_TIMEOUT,
    write=READ_TIMEOUT,
    pool=CONNECT_TIMEOUT,
)


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: str


async def ssrf_request_hook(request: httpx.Request) -> None:
    """httpx request hook: SSRF-check the original URL and every redirect hop."""
    await assert_public_http_url(str(request.url))


def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        timeout=HTTP_TIMEOUT,
        verify=True,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [ssrf_request_hook]},
    )


def media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_html_content_type(content_type: str) -> bool:
    media = media_type(content_type)
    if not media:
        return True
    return media in HTML_TYPES


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    lower = content_type.lower()
    if "charset=" in lower:
        charset = lower.split("charset=", 1)[1].split(";", 1)[0].strip().strip("\"'")
        if not charset:
            charset = "utf-8"
    try:
        return body.decode(charset)
    except LookupError:
        return body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> FetchedPage:
    """GET `url`, following redirects, capping the body at MAX_BODY_BYTES."""
    await assert_public_http_url(url)
    try:
        async with client.stream(
            "GET",
            url,
            headers=headers or None,
            cookies=cookies or None,
        ) as response:
            content_type = response.headers.get("content-type", "")
            length_header = response.headers.get("content-length")
            if length_header is not None:
                try:
                    declared = int(length_header)
                except ValueError:
                    declared = 0
                if declared > MAX_BODY_BYTES:
                    raise too_large(
                        "Response body exceeds 2MB limit",
                        {"max_bytes": MAX_BODY_BYTES, "content_length": declared},
                    )

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise too_large(
                        "Response body exceeds 2MB limit",
                        {"max_bytes": MAX_BODY_BYTES},
                    )
                chunks.append(chunk)

            body = _decode_body(b"".join(chunks), content_type)
            return FetchedPage(
                requested_url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=content_type,
                body=body,
            )
    except ArachneError:
        raise
    except httpx.TimeoutException as exc:
        raise timeout_error() from exc
    except httpx.TooManyRedirects as exc:
        raise fetch_failed("Too many redirects", {"max_redirects": MAX_REDIRECTS}) from exc
    except httpx.ConnectError as exc:
        raise fetch_failed("Failed to fetch URL", {"reason": str(exc), "kind": "connect"}) from exc
    except httpx.HTTPError as exc:
        raise fetch_failed("Failed to fetch URL", {"reason": str(exc)}) from exc
