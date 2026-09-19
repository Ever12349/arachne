"""Optional Playwright HTML render. Dynamically imported so the default install stays browser-free."""

from __future__ import annotations

import logging

from app import config
from app.errors import ArachneError, render_failed, render_unavailable
from app.fetch import FetchedPage
from app.ssrf import assert_public_http_url

logger = logging.getLogger("arachne")


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
) -> FetchedPage:
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

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"],
            )
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
                return FetchedPage(
                    requested_url=url,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    body=html,
                )
            finally:
                await browser.close()
    except ArachneError:
        raise
    except Exception:
        logger.info("render failed url=%s", url)
        raise render_failed() from None
