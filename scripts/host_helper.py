#!/usr/bin/env python3
"""Loopback HTTP helper that opens a headed browser and writes auto-<host>.bin.

Bind address is 127.0.0.1 only. A shared secret from ARACHNE_HOST_HELPER_TOKEN
is required on every route except GET /health and CORS preflight. Cookie
values are never written to the response.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.admin.guard import is_loopback_peer  # noqa: E402
from app.capture_broker import JOB_ID_RE_TEXT, CaptureBroker, CaptureBusy, CaptureFailed  # noqa: E402
from app.errors import ArachneError  # noqa: E402
from app.hostcfg import HELPER_SERVICE, helper_port, host_helper_token  # noqa: E402

_JOB_ID_RE = re.compile(JOB_ID_RE_TEXT)
_MAX_BODY = 65_536

HelperResult = tuple[int, dict[str, Any], dict[str, str]]


def presented_token(headers: Mapping[str, str]) -> str:
    """Bearer or X-Arachne-Helper-Token. Query strings are not accepted."""
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    direct = lowered.get("x-arachne-helper-token", "").strip()
    if direct:
        return direct
    auth = lowered.get("authorization", "").strip()
    if len(auth) >= 7 and auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def token_matches(provided: str, expected: str) -> bool:
    import hmac

    if not expected or not provided:
        return False
    try:
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def is_local_browser_origin(origin: str | None) -> bool:
    """True for http://127.0.0.1, localhost, or ::1, with an optional port."""
    if origin is None:
        return True
    parsed = urlparse(origin.strip())
    if parsed.scheme != "http" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def cors_headers(origin: str | None) -> dict[str, str]:
    """Allow the admin page on another loopback port to call the helper."""
    headers = {"Vary": "Origin"}
    if not origin or not is_local_browser_origin(origin):
        return headers
    headers["Access-Control-Allow-Origin"] = origin
    headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Arachne-Helper-Token"
    headers["Access-Control-Allow-Private-Network"] = "true"
    headers["Access-Control-Max-Age"] = "600"
    return headers


def dispatch(
    broker: CaptureBroker,
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    peer: str | None,
    token: str,
) -> HelperResult:
    """Auth, then one capture route. Non-loopback peers are rejected before the token is checked."""
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    origin = lowered.get("origin")
    extra = cors_headers(origin if origin else None)
    if not is_loopback_peer(peer):
        return 403, {"error": "localhost_only"}, extra
    if origin is not None and not is_local_browser_origin(origin):
        return 403, {"error": "localhost_only"}, extra
    route = urlsplit(path).path.rstrip("/") or "/"
    if method.upper() == "OPTIONS":
        return 204, {}, extra
    if method.upper() == "GET" and route == "/health":
        return 200, {"ok": True, "service": HELPER_SERVICE}, extra
    if not token_matches(presented_token(lowered), token):
        return 401, {"error": "unauthorized"}, extra
    try:
        return _route(broker, method.upper(), route, body, extra)
    except ArachneError as exc:
        return 400, {"error": exc.code, "message": exc.message}, extra
    except CaptureBusy as exc:
        payload = exc.job.public()
        payload["error"] = "capture_busy"
        payload["message"] = "已经有一个采集在进行。"
        return 409, payload, extra
    except CaptureFailed as exc:
        status = 400 if exc.code == "session_key_missing" else 501
        return status, {"error": exc.code, "message": exc.message}, extra


def _route(
    broker: CaptureBroker,
    method: str,
    route: str,
    body: bytes,
    extra: dict[str, str],
) -> HelperResult:
    if method == "POST" and route == "/capture":
        payload = _json_object(body)
        url = payload.get("url")
        if not isinstance(url, str) or not url.strip():
            return 400, {"error": "bad_url", "message": "url is required"}, extra
        job = broker.start(url)
        return 200, job.public(), extra
    parts = [part for part in route.split("/") if part]
    if len(parts) == 2 and parts[0] == "capture" and method == "GET":
        return _job_response(broker, parts[1], extra, done=False)
    if len(parts) == 3 and parts[0] == "capture" and parts[2] == "done" and method == "POST":
        return _job_response(broker, parts[1], extra, done=True)
    return 404, {"error": "not_found"}, extra


def _job_response(broker: CaptureBroker, job_id: str, extra: dict[str, str], *, done: bool) -> HelperResult:
    if not _JOB_ID_RE.fullmatch(job_id):
        return 404, {"error": "not_found"}, extra
    job = broker.done(job_id) if done else broker.get(job_id)
    if job is None:
        return 404, {"error": "not_found"}, extra
    return 200, job.public(), extra


def _json_object(body: bytes) -> dict[str, Any]:
    if len(body) > _MAX_BODY:
        raise ArachneError("bad_url", "Body is too large")
    try:
        data = json.loads(body.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArachneError("bad_url", "Expected a JSON object") from exc
    if not isinstance(data, dict):
        raise ArachneError("bad_url", "Expected a JSON object")
    return data


class HelperHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 handler. The server injects `broker` and `token` on the class."""

    broker: CaptureBroker
    token: str
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, fmt: str, *args: object) -> None:
        """Do not write request lines. Paths are safe; headers are not."""
        return

    def _handle(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send(400, {"error": "bad_url"}, {})
            return
        if length < 0 or length > _MAX_BODY:
            self._send(413, {"error": "bad_url", "message": "Body is too large"}, {})
            return
        raw = self.rfile.read(length) if length else b""
        peer = self.client_address[0] if self.client_address else None
        try:
            status, payload, extra = dispatch(
                self.broker,
                method=self.command,
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=raw,
                peer=peer,
                token=self.token,
            )
        except Exception:
            status, payload, extra = 500, {"error": "internal"}, {}
        self._send(status, payload, extra)

    def _send(self, status: int, payload: dict[str, Any], extra: dict[str, str]) -> None:
        if status == 204:
            raw = b""
        else:
            raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        if raw:
            self.wfile.write(raw)


def create_server(host: str, port: int, token: str, broker: CaptureBroker) -> ThreadingHTTPServer:
    """Bind `127.0.0.1` only. Any other host is refused before the socket opens."""
    if host != "127.0.0.1":
        raise ValueError("host helper binds to 127.0.0.1 only")
    if not token:
        raise ValueError("ARACHNE_HOST_HELPER_TOKEN is required")

    class _Bound(HelperHandler):
        pass

    _Bound.broker = broker
    _Bound.token = token
    return ThreadingHTTPServer((host, port), _Bound)


def serve(host: str, port: int, token: str, broker: CaptureBroker | None = None) -> None:
    server = create_server(host, port, token, broker or CaptureBroker())
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Loopback helper for headed session capture.")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    host = os.environ.get("ARACHNE_HOST_HELPER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host != "127.0.0.1":
        print("refusing to bind anything except 127.0.0.1", file=sys.stderr)
        return 1
    token = host_helper_token()
    if not token:
        print(
            "ARACHNE_HOST_HELPER_TOKEN is empty. Run: python scripts/install_host_helper.py",
            file=sys.stderr,
        )
        return 1
    port = args.port if args.port is not None else helper_port()
    try:
        server = create_server(host, port, token, CaptureBroker())
    except OSError:
        print(f"could not listen on 127.0.0.1:{port}", file=sys.stderr)
        return 1
    bound = server.server_address[1]
    print(f"arachne host helper listening on http://127.0.0.1:{bound}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
