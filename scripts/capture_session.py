#!/usr/bin/env python3
"""Open a headed browser on the host and save a Fernet auto-<host> session.

Run this on a machine with a display, not inside the arachne container.
The container stays headless. Docker Compose mounts ./data so the file
written here is visible to the app at ARACHNE_SESSIONS_DIR.

Never prints cookie or Authorization values.

Example:

    export ARACHNE_SESSION_KEY='...'
    export ARACHNE_SESSIONS_DIR=./data/sessions
    python scripts/capture_session.py --url 'https://example.com/article'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auto_session import auto_session_id  # noqa: E402
from app.errors import ArachneError  # noqa: E402
from app.render import cookies_for_host  # noqa: E402
from app.sessions import write_session_file  # noqa: E402
from app.ssrf import parse_http_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a headed-browser session into auto-<host>.bin.",
    )
    parser.add_argument("--url", required=True, help="http(s) page to open")
    args = parser.parse_args(argv)

    try:
        _scheme, host, _port = parse_http_url(args.url)
    except ArachneError:
        print("url must be http(s) with a hostname", file=sys.stderr)
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed. On the host: "
            "pip install -r requirements-playwright.txt && playwright install chromium",
            file=sys.stderr,
        )
        return 1

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=False)
            except Exception:
                print(
                    "could not open a headed browser; run this on the host, not in the container",
                    file=sys.stderr,
                )
                return 1
            try:
                context = browser.new_context()
                page = context.new_page()
                try:
                    page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
                except Exception:
                    print(
                        "navigation did not finish; the window stays open so you can pass the wall",
                        file=sys.stderr,
                    )
                print("Pass the wall in the browser, then press Enter here to save the session.")
                try:
                    input()
                except EOFError:
                    print("no confirmation on stdin", file=sys.stderr)
                    return 1
                raw = context.cookies()
            finally:
                browser.close()
    except Exception:
        print("capture failed before a session file was written", file=sys.stderr)
        return 1

    hostname = urlparse(args.url.strip()).hostname or host
    cookies = cookies_for_host(raw, hostname)
    if not cookies:
        print("no cookies for this host; nothing written", file=sys.stderr)
        return 1

    session_id = auto_session_id(args.url)
    try:
        path = write_session_file(session_id, cookies, {})
    except ArachneError:
        print("failed to write session", file=sys.stderr)
        return 1

    print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
