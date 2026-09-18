"""Request/response models for the batch jobs API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models import ErrorResponse, ExtractResponse, UAStrategy

JobStatus = Literal["queued", "running", "completed", "cancelled"]
ItemStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class JobDefaults(BaseModel):
    headers: dict[str, str] | None = None
    cookies: dict[str, str] | None = None
    max_chars: int | None = None
    session_id: str | None = None
    ua_strategy: UAStrategy = "default"
    render: bool = False
    site_profile: str | None = None


class JobItemInput(BaseModel):
    url: str = Field(..., description="http(s) URL to fetch and extract")
    headers: dict[str, str] | None = None
    cookies: dict[str, str] | None = None
    max_chars: int | None = None
    session_id: str | None = None
    ua_strategy: UAStrategy | None = None
    render: bool | None = None
    site_profile: str | None = None


class CreateJobRequest(BaseModel):
    defaults: JobDefaults = Field(default_factory=JobDefaults)
    items: list[JobItemInput] = Field(..., min_length=1)


class CreateJobResponse(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"
    total: int


class JobItemView(BaseModel):
    index: int
    url: str
    status: ItemStatus
    result: ExtractResponse | ErrorResponse | None = None


class JobView(BaseModel):
    job_id: str
    client_id: str = "default"
    status: JobStatus
    total: int
    succeeded_count: int
    failed_count: int
    cancelled_count: int
    items: list[JobItemView]


class JobSearchResponse(BaseModel):
    jobs: list[JobView]
