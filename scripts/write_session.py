#!/usr/bin/env python3
"""Write a Fernet-encrypted session file for POST /extract `session_id`.

Never prints cookie or Authorization values.

Example:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    export ARACHNE_SESSION_KEY='...'
    export ARACHNE_SESSIONS_DIR=./data/sessions
    python scripts/write_session.py --id demo \\
      --cookies '{"sid":"abc"}' \\
      --headers '{"Authorization":"Bearer …"}'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.errors import ArachneError  # noqa: E402
from app.sessions import write_session_file  # noqa: E402


def _parse_json_object(raw: str, flag: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{flag} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{flag} must be a JSON object of string keys and values")
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SystemExit(f"{flag} must be a JSON object of string keys and values")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an encrypted arachne session file.")
    parser.add_argument("--id", required=True, dest="session_id", help="Session id ([A-Za-z0-9_-]{1,64})")
    parser.add_argument("--cookies", default="{}", help="JSON object of cookie name → value")
    parser.add_argument("--headers", default="{}", help="JSON object of header name → value")
    parser.add_argument(
        "--cookies-file",
        default=None,
        help="Path to JSON object used instead of --cookies",
    )
    parser.add_argument(
        "--headers-file",
        default=None,
        help="Path to JSON object used instead of --headers",
    )
    args = parser.parse_args(argv)

    cookies_raw = Path(args.cookies_file).read_text(encoding="utf-8") if args.cookies_file else args.cookies
    headers_raw = Path(args.headers_file).read_text(encoding="utf-8") if args.headers_file else args.headers
    cookies = _parse_json_object(cookies_raw, "--cookies")
    headers = _parse_json_object(headers_raw, "--headers")

    try:
        path = write_session_file(args.session_id, cookies, headers)
    except ArachneError:
        print("failed to write session", file=sys.stderr)
        return 1

    print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
