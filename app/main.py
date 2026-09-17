"""Arachne FastAPI stub — placeholder extract contract only (no crawl/fetch)."""

from __future__ import annotations

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

app = FastAPI(title="arachne", version="0.0.1")


class ExtractRequest(BaseModel):
    url: str = Field(..., description="Page URL to extract (stub: not fetched)")


class ExtractResponse(BaseModel):
    url: str
    title: str
    main_text: str
    metadata: dict
    links: list[str]


def _placeholder(url: str) -> ExtractResponse:
    return ExtractResponse(
        url=url,
        title="",
        main_text="",
        metadata={},
        links=[],
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/extract", response_model=ExtractResponse)
def extract_get(url: str = Query(..., description="Page URL (stub: not fetched)")) -> ExtractResponse:
    return _placeholder(url)


@app.post("/extract", response_model=ExtractResponse)
def extract_post(body: ExtractRequest) -> ExtractResponse:
    return _placeholder(body.url)
