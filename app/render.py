"""Optional Playwright HTML render. Dynamically imported so the default install stays browser-free."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app import config
from app.errors import ArachneError, render_failed, render_unavailable
from app.fetch import FetchedPage
from app.proxy_util import playwright_proxy
from app.ssrf import assert_public_http_url, normalize_host

logger = logging.getLogger("arachne")


@dataclass(frozen=True)
class RenderedPage:
    """Browser fetch plus the cookie jar captured before the context closes."""

    page: FetchedPage
    cookies: dict[str, str]


def flatten_playwright_cookies(raw: object) -> dict[str, str]:
    """name → value. The same name later in the list wins. Domain is not filtered."""
    if not isinstance(raw, list):
        return {}
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            out[name] = value
    return out


def _domain_covers_host(domain: str, host: str) -> bool:
    domain_l = domain.strip().lstrip(".").lower().rstrip(".")
    host_l = host.strip().lower().rstrip(".")
    if not domain_l or not host_l:
        return False
    norm_domain = normalize_host(domain_l)
    norm_host = normalize_host(host_l)
    return (
        host_l == domain_l
        or host_l.endswith("." + domain_l)
        or norm_host == norm_domain
        or bool(norm_domain) and norm_host.endswith("." + norm_domain)
    )


def cookies_for_host(raw: object, host: str) -> dict[str, str]:
    """Cookies whose domain covers `host`, flattened name → value.

    A missing domain is treated as host-only for this navigation. Used by the
    host capture script. In-container auto-persist still uses the unfiltered
    `flatten_playwright_cookies`.
    """
    if not isinstance(raw, list):
        return {}
    matched: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            matched.append(item)
            continue
        if _domain_covers_host(domain, host):
            matched.append(item)
    return flatten_playwright_cookies(matched)


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


async def render_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> RenderedPage:
    """Headless Chromium goto(wait_until=domcontentloaded).

    SSRF-checks the URL (scheme, public IP, egress allowlist) first. The browser
    still connects by hostname — we do not pin-IP in Playwright.
    """
    await assert_public_http_url(url)
    if not playwright_available():
        raise render_unavailable()

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise render_unavailable() from exc

    timeout_ms = max(int(config.RENDER_TIMEOUT * 1000), 1)
    hdrs = dict(headers or {})
    user_agent = hdrs.pop("User-Agent", None)
    proxy = playwright_proxy()
    launch_kwargs: dict[str, object] = {
        "headless": True,
        "args": ["--disable-dev-shm-usage"],
    }
    if proxy is not None:
        launch_kwargs["proxy"] = proxy

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context_kwargs: dict[str, object] = {}
                if user_agent:
                    context_kwargs["user_agent"] = user_agent
                if hdrs:
                    context_kwargs["extra_http_headers"] = hdrs
                context = await browser.new_context(**context_kwargs)
                if cookies:
                    await context.add_cookies(
                        [{"name": name, "value": value, "url": url} for name, value in cookies.items()]
                    )
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                html = await page.content()
                final_url = page.url
                status_code = response.status if response is not None else 200
                content_type = "text/html"
                if response is not None:
                    content_type = response.headers.get("content-type") or "text/html"
                browser_cookies = flatten_playwright_cookies(await context.cookies())
                return RenderedPage(
                    page=FetchedPage(
                        requested_url=url,
                        final_url=final_url,
                        status_code=status_code,
                        content_type=content_type,
                        body=html,
                    ),
                    cookies=browser_cookies,
                )
            finally:
                await browser.close()
    except ArachneError:
        raise
    except Exception:
        logger.info("render failed url=%s", url)
        raise render_failed() from None
