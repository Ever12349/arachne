"""OpenAI-compatible chat client for LLM profile suggestion."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app import config
from app.errors import llm_failed, llm_unavailable

logger = logging.getLogger("arachne")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def llm_configured() -> bool:
    return bool((config.LLM_API_KEY or "").strip())


def chat_completions_url(base: str | None = None) -> str:
    text = (base if base is not None else config.LLM_BASE_URL) or ""
    text = text.strip() or "https://api.openai.com/v1"
    return text.rstrip("/") + "/chat/completions"


def parse_selector_json(text: str) -> dict[str, Any]:
    """Parse a JSON object of CSS selectors from a chat completion string."""
    stripped = (text or "").strip()
    if not stripped:
        raise llm_failed("LLM returned empty content")
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped).strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    last_error: Exception | None = None
    for blob in candidates:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(data, dict):
            return data
    raise llm_failed("LLM returned invalid JSON") from last_error


def normalize_selector_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only P3 selector fields from an LLM JSON object."""
    title = data.get("title_selector")
    main = data.get("main_selector")
    if not isinstance(title, str) or not title.strip():
        raise llm_failed("LLM omitted title_selector")
    if not isinstance(main, str) or not main.strip():
        raise llm_failed("LLM omitted main_selector")
    raw_remove = data.get("remove_selectors") or []
    if raw_remove is None:
        raw_remove = []
    if not isinstance(raw_remove, list):
        raise llm_failed("LLM remove_selectors must be a list")
    remove = [str(item).strip() for item in raw_remove if str(item).strip()]
    raw_meta = data.get("meta") or {}
    if raw_meta is None:
        raw_meta = {}
    if not isinstance(raw_meta, dict):
        raise llm_failed("LLM meta must be an object")
    meta = {str(k): "" if v is None else str(v) for k, v in raw_meta.items() if isinstance(k, str)}
    return {
        "title_selector": title.strip(),
        "main_selector": main.strip(),
        "remove_selectors": remove,
        "meta": meta,
    }


def _selector_system_prompt() -> str:
    return (
        "You propose CSS selectors for extracting an article title and main text from HTML. "
        "Reply with a single JSON object only, no markdown. Keys: "
        "title_selector (string), main_selector (string), "
        "remove_selectors (array of CSS strings), meta (object of name→selector). "
        "Selectors must be valid CSS for lxml cssselect. Prefer stable ids/classes over nth-of-type. "
        "title_selector must match an element whose text content is the title (not a meta content attribute). "
        "main_selector must match the main article body."
    )


def _selector_user_prompt(*, url: str, skeleton: str, draft: dict[str, Any] | None) -> str:
    draft_json = json.dumps(draft or {}, ensure_ascii=False, indent=2)
    return (
        f"URL: {url}\n\n"
        f"Heuristic draft selectors:\n{draft_json}\n\n"
        f"DOM skeleton (tag/id/class, truncated):\n{skeleton}\n"
    )


async def complete_chat(messages: list[dict[str, str]]) -> str:
    """POST OpenAI-compatible /chat/completions. Never logs the API key."""
    key = (config.LLM_API_KEY or "").strip()
    if not key:
        raise llm_unavailable()
    model = (config.LLM_MODEL or "").strip() or "gpt-4o-mini"
    url = chat_completions_url()
    payload = {"model": model, "messages": messages, "temperature": 0}
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(
        connect=5.0,
        read=float(config.LLM_TIMEOUT),
        write=15.0,
        pool=5.0,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise llm_failed("LLM request timed out") from exc
    except httpx.HTTPError as exc:
        raise llm_failed("LLM request failed") from exc
    if response.status_code >= 400:
        logger.warning("llm chat HTTP %s", response.status_code)
        raise llm_failed("LLM request failed", {"status_code": response.status_code})
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise llm_failed("LLM returned invalid JSON") from exc
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise llm_failed("LLM response missing message content") from exc
    if not isinstance(content, str):
        raise llm_failed("LLM response missing message content")
    return content


async def propose_selectors(*, url: str, skeleton: str, draft: dict[str, Any] | None) -> dict[str, Any]:
    """Ask the LLM for CSS selectors and parse them. Does not lxml-verify."""
    content = await complete_chat(
        [
            {"role": "system", "content": _selector_system_prompt()},
            {"role": "user", "content": _selector_user_prompt(url=url, skeleton=skeleton, draft=draft)},
        ]
    )
    return normalize_selector_payload(parse_selector_json(content))
