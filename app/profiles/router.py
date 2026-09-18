"""HTTP routes for profile suggest (no disk) and profile write."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.models import ErrorResponse
from app.profiles.loader import ProfileRegistry
from app.profiles.models import SuggestRequest, SuggestResponse, WriteProfileRequest, WriteProfileResponse
from app.service import run_suggest

router = APIRouter(tags=["profiles"])

PROFILE_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "bad_url | session_invalid | profile_invalid"},
    403: {"model": ErrorResponse, "description": "challenge_detected"},
    409: {"model": ErrorResponse, "description": "profile_exists"},
    422: {"model": ErrorResponse, "description": "unsupported_content | extract_empty | too_large"},
    429: {"model": ErrorResponse, "description": "rate_limited"},
    501: {"model": ErrorResponse, "description": "render_unavailable | llm_unavailable"},
    502: {"model": ErrorResponse, "description": "fetch_failed | unauthorized_upstream | render_failed | llm_failed"},
    504: {"model": ErrorResponse, "description": "timeout"},
}


def _registry(request: Request) -> ProfileRegistry:
    return request.app.state.profile_registry


@router.post(
    "/profiles/suggest",
    response_model=SuggestResponse,
    response_model_exclude_none=True,
    responses=PROFILE_ERROR_RESPONSES,
)
async def suggest_profile(request: Request, body: SuggestRequest) -> SuggestResponse:
    state = request.app.state
    return await run_suggest(
        url=body.url,
        client=state.http_client,
        headers=body.headers,
        cookies=body.cookies,
        session_id=body.session_id,
        render=body.render,
        strategy=body.strategy,
        limiter=state.rate_limiter,
        semaphore=state.extract_semaphore,
        stats=state.stats,
    )


@router.post(
    "/profiles",
    response_model=WriteProfileResponse,
    responses={
        400: {"model": ErrorResponse, "description": "bad_url | profile_invalid"},
        409: {"model": ErrorResponse, "description": "profile_exists"},
    },
)
async def write_profile(request: Request, body: WriteProfileRequest) -> WriteProfileResponse:
    saved = _registry(request).save_profile(body.profile, overwrite=body.overwrite)
    return WriteProfileResponse(profile=saved)
