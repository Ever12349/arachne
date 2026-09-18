"""Load site profiles from ARACHNE_PROFILES_DIR; poll mtime with a short debounce."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from app import config
from app.errors import profile_invalid
from app.profiles.schema import PROFILE_ID_RE, SiteProfile
from app.ssrf import normalize_host

logger = logging.getLogger("arachne")

_GENERIC_INVALID = "Invalid site profile"


def _dir_stamp(directory: Path) -> tuple:
    """Directory mtime plus each `*.json` name/mtime/size. Missing dir → `('missing',)`."""
    try:
        if not directory.is_dir():
            return ("missing",)
        files: list[tuple[str, float, int]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                st = path.stat()
                files.append((path.name, st.st_mtime, st.st_size))
            except OSError:
                continue
        return (directory.stat().st_mtime, tuple(files))
    except OSError:
        return ("missing",)


def _load_file(path: Path) -> SiteProfile | None:
    stem = path.stem
    if not PROFILE_ID_RE.fullmatch(stem):
        logger.warning("skipping profile file with invalid id name: %s", path.name)
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("skipping invalid profile JSON %s: %s", path.name, exc)
        return None
    try:
        profile = SiteProfile.model_validate(data)
    except ValidationError as exc:
        logger.warning("skipping invalid profile schema %s: %s", path.name, exc)
        return None
    if profile.id != stem:
        logger.warning(
            "skipping profile %s: id %r does not match filename",
            path.name,
            profile.id,
        )
        return None
    return profile


def _index_hosts(profiles: dict[str, SiteProfile]) -> dict[str, SiteProfile]:
    claims: dict[str, list[SiteProfile]] = {}
    for profile in profiles.values():
        seen_hosts: set[str] = set()
        for host in profile.hosts:
            normalized = normalize_host(host)
            if not normalized or normalized in seen_hosts:
                continue
            seen_hosts.add(normalized)
            claims.setdefault(normalized, []).append(profile)

    by_host: dict[str, SiteProfile] = {}
    for host, claimed in claims.items():
        unique_ids = sorted({p.id for p in claimed})
        winner = max(claimed, key=lambda p: p.id)
        if len(unique_ids) > 1:
            logger.warning(
                "multiple profiles claim host %s: %s; using %s",
                host,
                unique_ids,
                winner.id,
            )
        by_host[host] = winner
    return by_host


class ProfileRegistry:
    """In-memory map of profile id → SiteProfile, refreshed when the dir mtime changes."""

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        debounce_seconds: float | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._dir = Path(directory if directory is not None else config.PROFILES_DIR).expanduser()
        self._debounce = (
            config.PROFILE_RELOAD_DEBOUNCE_SECONDS if debounce_seconds is None else float(debounce_seconds)
        )
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._last_check = float("-inf")
        self._seen_stamp: tuple | None = None
        self._by_id: dict[str, SiteProfile] = {}
        self._by_host: dict[str, SiteProfile] = {}

    @classmethod
    def from_config(cls) -> ProfileRegistry:
        return cls(config.PROFILES_DIR, debounce_seconds=config.PROFILE_RELOAD_DEBOUNCE_SECONDS)

    def _reload_unlocked(self) -> None:
        loaded: dict[str, SiteProfile] = {}
        try:
            if self._dir.is_dir():
                for path in sorted(self._dir.glob("*.json")):
                    if not path.is_file():
                        continue
                    profile = _load_file(path)
                    if profile is not None:
                        loaded[profile.id] = profile
        except OSError as exc:
            logger.warning("failed to read profiles dir %s: %s", self._dir, exc)
            loaded = {}
        self._by_id = loaded
        self._by_host = _index_hosts(loaded)

    def maybe_reload(self, *, force: bool = False) -> None:
        with self._lock:
            now = self._monotonic()
            if not force and (now - self._last_check) < self._debounce:
                return
            self._last_check = now
            stamp = _dir_stamp(self._dir)
            if not force and self._seen_stamp is not None and stamp == self._seen_stamp:
                return
            self._reload_unlocked()
            self._seen_stamp = stamp

    def get(self, profile_id: str) -> SiteProfile:
        """Force-load by id. Missing or invalid id → profile_invalid (HTTP 400)."""
        raw = (profile_id or "").strip()
        if not PROFILE_ID_RE.fullmatch(raw):
            raise profile_invalid(_GENERIC_INVALID, {"site_profile": raw})
        self.maybe_reload()
        with self._lock:
            profile = self._by_id.get(raw)
        if profile is None:
            raise profile_invalid(_GENERIC_INVALID, {"site_profile": raw})
        return profile

    def match_host(self, host: str | None) -> SiteProfile | None:
        normalized = normalize_host(host)
        if not normalized:
            return None
        self.maybe_reload()
        with self._lock:
            return self._by_host.get(normalized)

    def match_url(self, url: str) -> SiteProfile | None:
        return self.match_host(urlparse(url).hostname)


def resolve_profile(
    registry: ProfileRegistry | None,
    *,
    site_profile: str | None,
    url: str,
) -> SiteProfile | None:
    """Explicit id wins (and 400s if invalid); otherwise auto-match `url`'s host."""
    explicit = (site_profile or "").strip() or None
    if explicit:
        if registry is None:
            raise profile_invalid(_GENERIC_INVALID, {"site_profile": explicit})
        return registry.get(explicit)
    if registry is None:
        return None
    return registry.match_url(url)
