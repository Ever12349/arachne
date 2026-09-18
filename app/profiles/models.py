"""Request/response models for profile suggest and write APIs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.profiles.schema import SiteProfile

SuggestStrategy = Literal["heuristic", "llm", "auto"]


class SuggestRequest(BaseModel):
    url: str = Field(..., description="http(s) page URL to fetch and suggest selectors for")
    headers: dict[str, str] | None = Field(
        default=None,
        description="Optional request headers (allowlisted names only)",
    )
    cookies: dict[str, str] | None = Field(
        default=None,
        description="Optional cookies forwarded to the upstream request",
    )
    session_id: str | None = Field(
        default=None,
        description="Optional encrypted session id loaded from ARACHNE_SESSIONS_DIR",
    )
    render: bool = Field(
        default=False,
        description="If true, fetch via headless Playwright",
    )
    strategy: SuggestStrategy = Field(
        default="heuristic",
        description="heuristic | llm | auto (default heuristic)",
    )


class SelectorVariant(BaseModel):
    title_selector: str = ""
    main_selector: str = ""
    remove_selectors: list[str] = Field(default_factory=list)


class SuggestEvidence(BaseModel):
    strategy_used: Literal["heuristic", "llm"]
    title_preview: str
    main_preview: str
    alternatives: list[SelectorVariant] | None = None
    llm_skipped: str | None = None


class SuggestResponse(BaseModel):
    profile: SiteProfile
    evidence: SuggestEvidence


class WriteProfileRequest(BaseModel):
    profile: SiteProfile
    overwrite: bool = False


class WriteProfileResponse(BaseModel):
    profile: SiteProfile
