"""Optional egress domain allowlist for user crawl URLs only.

Does not apply to ARACHNE_LLM_BASE_URL. Empty allowlist disables the check.
"""

from __future__ import annotations

from app import config
from app.errors import egress_blocked
from app.ssrf import normalize_host


def host_allowed(host: str, allowlist: tuple[str, ...] | None = None) -> bool:
    suffixes = config.EGRESS_ALLOWLIST if allowlist is None else allowlist
    if not suffixes:
        return True
    normalized = normalize_host(host)
    if not normalized:
        return False
    for raw in suffixes:
        suffix = normalize_host(raw)
        if not suffix:
            continue
        if normalized == suffix or normalized.endswith("." + suffix):
            return True
    return False


def assert_egress_allowed(host: str) -> None:
    if not host_allowed(host):
        raise egress_blocked("Host is not on the egress allowlist", {"host": host})
