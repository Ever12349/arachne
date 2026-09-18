"""Orchestrate validate → fetch → extract for a single URL."""

from __future__ import annotations

import logging

import httpx

from app.errors import ArachneError, fetch_failed, unauthorized_upstream, unsupported_content
from app.fetch import FetchedPage, fetch_url, is_html_content_type
from app.extract import extract_html
from app.models import ExtractResponse

logger = logging.getLogger("arachne")


def _reject_error_status(page: FetchedPage) -> None:
    if page.status_code < 400:
        return
    detail = {"status_code": page.status_code}
    if page.status_code in {401, 403}:
        raise unauthorized_upstream(
            f"Upstream returned {page.status_code}",
            detail,
        )
    raise fetch_failed(f"Upstream returned {page.status_code}", detail)


async def extract_page(url: str, client: httpx.AsyncClient) -> ExtractResponse:
    requested = (url or "").strip()
    page = await fetch_url(client, requested)
    _reject_error_status(page)
    if not is_html_content_type(page.content_type):
        raise unsupported_content(
            "Unsupported content type",
            {"content_type": page.content_type, "status_code": page.status_code},
        )
    try:
        return extract_html(
            page.body,
            requested_url=requested,
            final_url=page.final_url,
            status_code=page.status_code,
            content_type=page.content_type,
        )
    except ArachneError:
        raise
    except Exception:
        logger.exception("extract failed for %s", requested)
        raise
