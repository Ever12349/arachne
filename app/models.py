"""Pydantic request/response models for the extract API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

UAStrategy = Literal["default", "rotate"]


class ExtractRequest(BaseModel):
    url: str = Field(..., description="http(s) URL to fetch and extract")
    headers: dict[str, str] | None = Field(
        default=None,
        description="Optional request headers (allowlisted names only; POST only)",
    )
    cookies: dict[str, str] | None = Field(
        default=None,
        description="Optional cookies forwarded to the upstream request (POST only)",
    )
    max_chars: int | None = Field(
        default=None,
        description="Optional main_text cap; clamped to [1, MAIN_TEXT_MAX_CHARS]",
    )
    session_id: str | None = Field(
        default=None,
        description="Optional encrypted session id loaded from ARACHNE_SESSIONS_DIR (POST only)",
    )
    ua_strategy: UAStrategy = Field(
        default="default",
        description="User-Agent strategy (POST only): default | rotate",
    )
    render: bool = Field(
        default=False,
        description="If true, fetch via headless Playwright (POST only; optional extra install)",
    )


class OgMetadata(BaseModel):
    title: str = ""
    description: str = ""
    image: str = ""


class PageMetadata(BaseModel):
    description: str = ""
    language: str = ""
    content_type: str = ""
    og: OgMetadata = Field(default_factory=OgMetadata)


class Link(BaseModel):
    href: str
    text: str = ""


class ExtractResponse(BaseModel):
    url: str
    requested_url: str
    status_code: int
    title: str
    main_text: str
    metadata: PageMetadata
    links: list[Link]
    truncated: bool = False


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class StatsResponse(BaseModel):
    requests_total: int
    errors_by_code: dict[str, int]
    cache_hits: int
    cache_misses: int
    in_flight: int
    latency_ms_sum: float
    latency_ms_count: int
