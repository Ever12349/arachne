"""Shared fixtures for unit tests (no live network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.fetch import ssrf_request_hook
from app.limits import FixedWindowRateLimiter
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_HTML = (FIXTURES / "sample.html").read_text(encoding="utf-8")
EMPTY_HTML = (FIXTURES / "empty.html").read_text(encoding="utf-8")
ARTICLE_PROFILE_HTML = (FIXTURES / "article_profile.html").read_text(encoding="utf-8")
CHALLENGE_CF_HTML = (FIXTURES / "challenge_cf.html").read_text(encoding="utf-8")
CHALLENGE_ATTENTION_HTML = (FIXTURES / "challenge_attention.html").read_text(encoding="utf-8")

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch):
    """Resolve hostnames without touching DNS; IP literals stay as-is."""
    import ipaddress

    async def fake_resolve(host: str, port: int) -> list[str]:
        stripped = host.strip("[]")
        try:
            ipaddress.ip_address(stripped)
            return [stripped]
        except ValueError:
            pass
        if host.lower() in {"localhost"}:
            return ["127.0.0.1"]
        return [PUBLIC_IP]

    monkeypatch.setattr("app.ssrf.resolve_host_ips", fake_resolve)
    return fake_resolve


def _handler_for(
    *,
    status: int = 200,
    body: str = SAMPLE_HTML,
    content_type: str = "text/html; charset=utf-8",
    extra_headers: dict[str, str] | None = None,
    raise_exc: BaseException | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        headers = {"content-type": content_type}
        if extra_headers:
            headers.update(extra_headers)
        return httpx.Response(status, content=body.encode("utf-8"), headers=headers, request=request)

    return handler


@pytest.fixture
def api_client(public_dns, monkeypatch: pytest.MonkeyPatch):
    """TestClient whose upstream HTTP is mocked to return SAMPLE_HTML."""

    def fake_create() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler_for()),
            follow_redirects=True,
            max_redirects=20,
            timeout=httpx.Timeout(connect=5, read=15, write=15, pool=5),
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    with TestClient(app) as client:
        yield client


def set_high_qps(client: TestClient, qps: int = 10_000) -> None:
    """Replace the process limiter so a test can issue many extract calls."""
    client.app.state.rate_limiter = FixedWindowRateLimiter(qps=qps)


@pytest.fixture(autouse=True)
def _isolated_profiles_dir(tmp_path_factory, monkeypatch: pytest.MonkeyPatch):
    """Keep tests off the default ./data/profiles and the shipped examples dir."""
    directory = tmp_path_factory.mktemp("profiles")
    monkeypatch.setattr("app.config.PROFILES_DIR", str(directory))


@pytest.fixture(autouse=True)
def _isolated_job_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Give each test its own SQLite file; never touch ./data/arachne.db."""
    db_path = tmp_path / "arachne.db"
    monkeypatch.setattr("app.config.DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setattr("app.config.JOB_DB_TTL_SECONDS", 0)


@pytest.fixture(autouse=True)
def _p7_safe_defaults(monkeypatch: pytest.MonkeyPatch):
    """Keep existing tests anonymous; allow profile writes unless a test opts out."""
    monkeypatch.setattr("app.config.REQUIRE_AUTH", False)
    monkeypatch.setattr("app.config.API_KEYS", ())
    monkeypatch.setattr("app.config.METRICS_PUBLIC", False)
    monkeypatch.setattr("app.config.STATS_PUBLIC", False)
    monkeypatch.setattr("app.config.PROFILES_WRITE", True)
    monkeypatch.setattr("app.config.EGRESS_ALLOWLIST", ())
    monkeypatch.setattr("app.config.LOG_JSON", False)


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch: pytest.MonkeyPatch):
    """Unit tests should not wait on real retry sleeps."""

    async def instant(_delay: float = 0) -> None:
        return None

    monkeypatch.setattr("app.antibot.asyncio.sleep", instant)
