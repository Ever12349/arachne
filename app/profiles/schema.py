"""JSON schema for one site profile file."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SiteProfile(BaseModel):
    """One host-scoped CSS extract profile. Filename stem must equal `id`."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Profile id; must match filename stem")
    version: str = Field(..., min_length=1)
    hosts: list[str] = Field(..., min_length=1)
    title_selector: str = ""
    main_selector: str = ""
    remove_selectors: list[str] = Field(default_factory=list)
    meta: dict[str, str] = Field(default_factory=dict)
    strict: bool = False
    disable_links: bool = False

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, value: str) -> str:
        if not PROFILE_ID_RE.fullmatch(value or ""):
            raise ValueError("id must match ^[A-Za-z0-9_-]{1,64}$")
        return value

    @field_validator("version")
    @classmethod
    def _version_non_empty(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("version must be a non-empty string")
        return text

    @field_validator("hosts")
    @classmethod
    def _hosts_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [h.strip() for h in value if isinstance(h, str) and h.strip()]
        if not cleaned:
            raise ValueError("hosts must contain at least one hostname")
        return cleaned

    @field_validator("title_selector", "main_selector", mode="before")
    @classmethod
    def _optional_selector(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value)

    @field_validator("remove_selectors", mode="before")
    @classmethod
    def _remove_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("remove_selectors must be a list")
        return [str(item) for item in value]

    @field_validator("meta", mode="before")
    @classmethod
    def _meta_map(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("meta must be an object of selector strings")
        out: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            out[key] = "" if item is None else str(item)
        return out
