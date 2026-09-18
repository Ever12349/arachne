"""Site profiles: load JSON rules and apply CSS selectors before generic extract."""

from app.profiles.apply import extract_with_profile
from app.profiles.loader import ProfileRegistry, resolve_profile
from app.profiles.schema import PROFILE_ID_RE, SiteProfile

__all__ = [
    "PROFILE_ID_RE",
    "ProfileRegistry",
    "SiteProfile",
    "extract_with_profile",
    "resolve_profile",
]
