"""Tiny `.env` reader and updater. Values are never logged."""

from __future__ import annotations

import re
from pathlib import Path

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RAW_VALUE_RE = re.compile(r"[A-Za-z0-9_./:+-]*")


def parse_env_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Blank lines and `#` comments are ignored."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        parsed = _parse_assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        found[key] = value
    return found


def load_env_file(path: Path) -> dict[str, str]:
    """Return assignments from `path`. A missing file is an empty dict."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_env_text(text)


def upsert_env_text(text: str, updates: dict[str, str]) -> str:
    """Replace known keys and append the rest. Comments and other keys stay."""
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        parsed = _parse_assignment(line)
        if parsed is not None and parsed[0] in updates:
            key = parsed[0]
            out.append(_format_assignment(key, updates[key]))
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key in seen:
            continue
        if out and out[-1] != "":
            out.append("")
        out.append(_format_assignment(key, value))
    rendered = "\n".join(out)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _parse_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    if not _KEY_RE.fullmatch(key):
        return None
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _format_assignment(key: str, value: str) -> str:
    if _RAW_VALUE_RE.fullmatch(value or ""):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'
