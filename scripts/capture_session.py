#!/usr/bin/env python3
"""Open a headed browser on the host and save a Fernet auto-<host> session.

The admin button talks to scripts/arachne-host-helper so a person does not
run this by hand. This entry remains for a machine where the helper is not
installed. Run it on a machine with a display, not inside the container.

Never prints cookie or Authorization values.

Example:

    export ARACHNE_SESSION_KEY='...'
    export ARACHNE_SESSIONS_DIR=./data/sessions
    python scripts/capture_session.py --url 'https://example.com/article'
"""

from __future__ import annotations

import argparse
import select
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.capture_broker import CaptureBroker, CaptureBusy, CaptureFailed, CaptureJob  # noqa: E402
from app.errors import ArachneError  # noqa: E402
from app.ssrf import parse_http_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a headed-browser session into auto-<host>.bin.",
    )
    parser.add_argument("--url", required=True, help="http(s) page to open")
    args = parser.parse_args(argv)

    try:
        parse_http_url(args.url)
    except ArachneError:
        print("url must be http(s) with a hostname", file=sys.stderr)
        return 1

    broker = CaptureBroker()
    try:
        job = broker.start(args.url)
    except CaptureFailed as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except CaptureBusy:
        print("another capture is already running", file=sys.stderr)
        return 1

    print("Pass the wall in the browser, then press Enter here to save the session.")
    print("If the challenge page clears on its own, the script saves without Enter.")
    try:
        early = _wait_for_enter(job)
    except KeyboardInterrupt:
        job.abort()
        print("capture cancelled", file=sys.stderr)
        return 1
    if early is not None:
        return early
    job.finished.wait(timeout=30)
    state = job.public()
    if state["status"] == "ready":
        print(f"wrote {state['filename']}")
        return 0
    print(state["error"] or "capture failed", file=sys.stderr)
    return 1


def _wait_for_enter(job: CaptureJob) -> int | None:
    """Block until Enter, stdin EOF, or the runner finishes on its own.

    Returns 1 when stdin closes before the person confirms. Otherwise None
    and the caller waits for the runner to finish.
    """
    if sys.stdin.isatty():
        while not job.finished.wait(timeout=0.25):
            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if not readable:
                continue
            sys.stdin.readline()
            job.request_done()
        return None
    line = sys.stdin.readline()
    if line == "":
        job.abort()
        job.finished.wait(timeout=30)
        print("no confirmation on stdin", file=sys.stderr)
        return 1
    job.request_done()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
