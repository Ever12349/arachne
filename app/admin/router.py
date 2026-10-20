"""Jinja2 admin pages and /admin/api helpers. Omitted from the OpenAPI schema."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app import config
from app.admin.capture_launch import LaunchResult, capture_job, finish_capture, start_capture
from app.admin.guard import require_admin
from app.admin.ops import (
    capture_status,
    delete_session,
    list_sessions,
    save_pasted_cookies,
    session_count,
)
from app.errors import ArachneError, bad_url
from app.failures import get_failure_ring, new_session_path
from app.service import run_extract
from app.sessions import session_key_usable
from app.ssrf import parse_http_url

router = APIRouter(
    prefix="/admin",
    include_in_schema=False,
    dependencies=[Depends(require_admin)],
)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
_TEXT_PREVIEW = 800


def _render(request: Request, name: str, context: dict[str, object], status_code: int = 200) -> Response:
    payload = {"request": request, "version": request.app.version, **context}
    return _TEMPLATES.TemplateResponse(request, name, payload, status_code=status_code)


def _external_href(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return url.strip()
    return None


_TEMPLATES.env.globals["external_href"] = _external_href


async def _health(request: Request) -> dict[str, object]:
    worker = getattr(request.app.state, "job_worker", None)
    worker_alive = worker is not None and worker.is_alive()
    db_ok = False
    engine = getattr(request.app.state, "db_engine", None)
    if engine is not None:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
    return {"worker_alive": worker_alive, "db_ok": db_ok, "ready": worker_alive and db_ok}


def _flags() -> dict[str, object]:
    return {
        "proxy_enabled": bool(config.HTTP_PROXY),
        "auto_render": bool(config.AUTO_RENDER),
        "auto_render_on": sorted(config.AUTO_RENDER_ON),
        "session_persist": bool(config.SESSION_PERSIST),
        "session_key_configured": session_key_usable(),
        "session_count": session_count(),
        "admin_enabled": True,
    }


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    text_value = value.strip()
    return text_value or None


@router.get("")
@router.get("/")
async def dashboard(request: Request) -> Response:
    failures = get_failure_ring().list_recent()
    return _render(
        request,
        "admin/dashboard.html",
        {
            "health": await _health(request),
            "flags": _flags(),
            "failures": failures[:10],
            "failure_total": len(failures),
        },
    )


@router.get("/failures")
async def failures_page(request: Request) -> Response:
    return _render(
        request,
        "admin/failures.html",
        {"failures": get_failure_ring().list_recent()},
    )


@router.get("/sessions")
async def sessions_page(request: Request) -> Response:
    return _render(request, "admin/sessions.html", {"sessions": list_sessions()})


@router.post("/sessions/{session_id}/delete")
async def sessions_delete(session_id: str) -> RedirectResponse:
    delete_session(session_id)
    return RedirectResponse("/admin/sessions", status_code=303)


@router.get("/sessions/new")
async def session_new(request: Request, url: str = "") -> Response:
    target = url.strip()
    status: dict[str, object] | None = None
    page_error = ""
    if target:
        try:
            status = capture_status(target)
        except ArachneError as exc:
            page_error = exc.message
    return _render(
        request,
        "admin/session_new.html",
        {
            "url": target,
            "external_href": _external_href(target) if target else None,
            "status": status,
            "error": page_error,
            "saved_id": "",
        },
    )


@router.post("/sessions")
async def session_paste(request: Request, url: str = Form(""), cookies_json: str = Form("")) -> Response:
    target = url.strip()
    saved_id = ""
    page_error = ""
    status: dict[str, object] | None = None
    try:
        if not target:
            raise bad_url("URL is empty")
        parse_http_url(target)
        try:
            parsed = json.loads(cookies_json or "")
        except json.JSONDecodeError as exc:
            raise bad_url("Cookie JSON is not valid") from exc
        saved_id = save_pasted_cookies(target, parsed)
        status = capture_status(target)
    except ArachneError as exc:
        page_error = exc.message
        if target:
            try:
                status = capture_status(target)
            except ArachneError:
                status = None
    return _render(
        request,
        "admin/session_new.html",
        {
            "url": target,
            "external_href": _external_href(target) if target else None,
            "status": status,
            "error": page_error,
            "saved_id": saved_id,
        },
        status_code=400 if page_error else 200,
    )


@router.get("/extract")
async def extract_form(request: Request, url: str = "", session_id: str = "") -> Response:
    return _render(
        request,
        "admin/extract.html",
        {
            "url": url.strip(),
            "session_id": session_id.strip(),
            "result": None,
            "error": None,
            "new_href": new_session_path(url.strip()) if url.strip() else "",
        },
    )


@router.post("/extract")
async def extract_submit(
    request: Request,
    url: str = Form(...),
    session_id: str = Form(""),
) -> Response:
    target = url.strip()
    sid = _blank(session_id)
    result = None
    error: dict[str, object] | None = None
    status_code = 200
    try:
        state = request.app.state
        extracted = await run_extract(
            url=target,
            client=state.http_client,
            session_id=sid,
            profiles=state.profile_registry,
            cache=state.extract_cache,
            limiter=state.rate_limiter,
            semaphore=state.extract_semaphore,
            stats=state.stats,
        )
        text_value = extracted.main_text or ""
        result = {
            "title": extracted.title,
            "summary": text_value[:_TEXT_PREVIEW],
            "summary_clipped": len(text_value) > _TEXT_PREVIEW,
            "status_code": extracted.status_code,
            "final_url": extracted.url,
        }
    except ArachneError as exc:
        status_code = 200
        error = {"code": exc.code, "message": exc.message, "detail": dict(exc.detail)}
    return _render(
        request,
        "admin/extract.html",
        {
            "url": target,
            "session_id": sid or "",
            "result": result,
            "error": error,
            "new_href": new_session_path(target) if target else "",
        },
        status_code=status_code,
    )


@router.get("/api/status")
async def api_status(request: Request) -> dict[str, object]:
    return {
        "version": request.app.version,
        "health": await _health(request),
        **_flags(),
        "failures": get_failure_ring().list_recent(),
    }


@router.get("/api/failures")
async def api_failures() -> dict[str, object]:
    return {"failures": get_failure_ring().list_recent()}


@router.get("/api/sessions")
async def api_sessions() -> dict[str, object]:
    return {"sessions": list_sessions()}


@router.delete("/api/sessions/{session_id}", status_code=204)
async def api_delete_session(session_id: str) -> Response:
    delete_session(session_id)
    return Response(status_code=204)


@router.post("/api/sessions")
async def api_paste_session(request: Request) -> dict[str, str]:
    try:
        body = await request.json()
    except Exception as exc:
        raise bad_url("Expected a JSON object") from exc
    if not isinstance(body, dict):
        raise bad_url("Expected a JSON object")
    url = body.get("url")
    if not isinstance(url, str):
        raise bad_url("url is required")
    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise bad_url("session_id must be a string")
    saved = save_pasted_cookies(url, body.get("cookies"), body.get("headers"), session_id)
    return {"session_id": saved, "filename": f"{saved}.bin"}


@router.get("/api/capture")
async def api_capture(url: str) -> dict[str, object]:
    return capture_status(url)


@router.post("/api/capture/start")
async def api_capture_start(request: Request) -> JSONResponse:
    body = await _json_object(request)
    url = body.get("url")
    if not isinstance(url, str):
        raise bad_url("url is required")
    return _launch(start_capture(url))


@router.get("/api/capture/jobs/{job_id}")
async def api_capture_job(job_id: str) -> JSONResponse:
    return _launch(capture_job(job_id))


@router.post("/api/capture/jobs/{job_id}/done")
async def api_capture_done(job_id: str) -> JSONResponse:
    return _launch(finish_capture(job_id))


def _launch(result: LaunchResult) -> JSONResponse:
    return JSONResponse(status_code=result.status_code, content=result.body)


async def _json_object(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except Exception as exc:
        raise bad_url("Expected a JSON object") from exc
    if not isinstance(body, dict):
        raise bad_url("Expected a JSON object")
    return body


@router.post("/api/extract")
async def api_extract(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except Exception as exc:
        raise bad_url("Expected a JSON object") from exc
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        raise bad_url("url is required")
    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise bad_url("session_id must be a string")
    state = request.app.state
    extracted = await run_extract(
        url=body["url"],
        client=state.http_client,
        session_id=_blank(session_id) if isinstance(session_id, str) else None,
        profiles=state.profile_registry,
        cache=state.extract_cache,
        limiter=state.rate_limiter,
        semaphore=state.extract_semaphore,
        stats=state.stats,
    )
    text_value = extracted.main_text or ""
    return {
        "title": extracted.title,
        "summary": text_value[:_TEXT_PREVIEW],
        "summary_clipped": len(text_value) > _TEXT_PREVIEW,
        "status_code": extracted.status_code,
        "url": extracted.url,
        "requested_url": extracted.requested_url,
    }
