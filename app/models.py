"""Pydantic request/response models for the extract API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExtractRequest(BaseModel):
    url: str = Field(..., description="http(s) URL to fetch and extract")


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


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
