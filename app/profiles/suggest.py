"""Heuristic (and LLM-backed) CSS selector suggestion for a fetched page."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from lxml import html as lxml_html
from lxml.etree import ParseError

from app import config
from app.errors import LLM_FAILED, ArachneError, extract_empty, llm_failed, llm_unavailable
from app.extract import _clean
from app.profiles import llm as profile_llm
from app.profiles.apply import _css_all, _css_first, apply_remove_selectors
from app.profiles.models import SelectorVariant, SuggestEvidence, SuggestResponse, SuggestStrategy
from app.profiles.schema import PROFILE_ID_RE, SiteProfile
from app.ssrf import normalize_host

logger = logging.getLogger("arachne")

_TITLE_PREVIEW_CHARS = 200
_MAIN_PREVIEW_CHARS = 400
_MAX_ALTERNATIVES = 3
_MAX_TITLE_CANDIDATES = 6
_MAX_MAIN_CANDIDATES = 8

_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe", "form", "button", "input", "link", "meta", "br", "hr"}
)
_CHROME_TAGS = frozenset({"nav", "footer", "header", "aside"})
_MAIN_TAGS = frozenset({"article", "main", "section", "div", "td"})
_IDENT_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_POSITIVE_RE = re.compile(
    r"article|body|content|entry|main|page|post|text|blog|story|markdown|prose|headline|title",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"combx|comment|contact|foot|footer|footnote|nav|header|masthead|media|"
    r"outbrain|promo|related|scroll|share|shoutbox|sidebar|sponsor|shopping|"
    r"tags|tool|widget|\bad\b|advert|cookie|modal|popup|subscribe|social|menu|login",
    re.I,
)
_NOISY_CLASS_RE = re.compile(r"^(js-|is-|has-|css-|wp-)", re.I)
_REMOVE_CANDIDATES = (
    "nav",
    "header",
    "footer",
    "aside",
    ".ads",
    ".ad",
    ".advertisement",
    ".sidebar",
    ".comments",
    ".share",
    ".social",
)


@dataclass(frozen=True)
class SelectorSet:
    title_selector: str
    main_selector: str
    remove_selectors: tuple[str, ...]
    title_preview: str
    main_preview: str
    meta: tuple[tuple[str, str], ...] = ()

    def as_variant(self) -> SelectorVariant:
        return SelectorVariant(
            title_selector=self.title_selector,
            main_selector=self.main_selector,
            remove_selectors=list(self.remove_selectors),
        )

    def as_profile(self, *, profile_id: str, host: str) -> SiteProfile:
        return SiteProfile(
            id=profile_id,
            version="1",
            hosts=[host],
            title_selector=self.title_selector,
            main_selector=self.main_selector,
            remove_selectors=list(self.remove_selectors),
            meta=dict(self.meta),
            strict=False,
            disable_links=False,
        )


def profile_id_from_host(host: str | None) -> str:
    """Normalized host with dots (and other illegal id chars) turned into dashes."""
    normalized = normalize_host(host)
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", normalized)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = "site"
    cleaned = cleaned[:64]
    if not PROFILE_ID_RE.fullmatch(cleaned):
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", cleaned)[:64] or "site"
    return cleaned


def host_from_url(url: str) -> str:
    return normalize_host(urlparse(url).hostname)


def parse_html_tree(html: str) -> lxml_html.HtmlElement:
    try:
        return lxml_html.fromstring(html)
    except (ParseError, TypeError, ValueError) as exc:
        raise extract_empty("HTML could not be parsed") from exc
    except Exception as exc:  # pragma: no cover - lxml recovers from most input
        raise extract_empty("HTML could not be parsed") from exc


def _preview(text: str, limit: int) -> str:
    cleaned = _clean(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit]


def _attr_blob(el: lxml_html.HtmlElement) -> str:
    return f"{el.get('id') or ''} {el.get('class') or ''} {el.get('role') or ''}"


def _tag_name(el: lxml_html.HtmlElement) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.lower().split("}", 1)[-1]


def _safe_ident(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or not _IDENT_RE.fullmatch(text):
        return None
    if _NOISY_CLASS_RE.match(text):
        return None
    return text


def _classes(el: lxml_html.HtmlElement) -> list[str]:
    out: list[str] = []
    for part in (el.get("class") or "").split():
        ident = _safe_ident(part)
        if ident and ident not in out:
            out.append(ident)
    return out


def _page_text(tree: lxml_html.HtmlElement) -> str:
    clone = lxml_html.fromstring(lxml_html.tostring(tree, encoding="unicode", method="html"))
    for el in clone.xpath(".//script|.//style|.//noscript|.//template"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    return _clean(clone.text_content())


def _word_set(text: str) -> set[str]:
    return {w for w in re.findall(r"[A-Za-z0-9]+", text.lower()) if w}


def overlaps_page_text(selected: str, page_text: str, *, ratio: float | None = None) -> bool:
    sel = _clean(selected)
    page = _clean(page_text)
    if not sel or not page:
        return False
    if sel in page:
        return True
    sel_words = _word_set(sel)
    page_words = _word_set(page)
    if not sel_words:
        return False
    threshold = config.SUGGEST_OVERLAP_RATIO if ratio is None else ratio
    return (len(sel_words & page_words) / len(sel_words)) >= threshold


def self_test(
    html: str,
    title_selector: str,
    main_selector: str,
    remove_selectors: list[str] | None = None,
    *,
    min_title: int | None = None,
    min_main: int | None = None,
    page_text: str | None = None,
) -> tuple[str, str] | None:
    """Apply CSS selectors with lxml; require min lengths and overlap with page text."""
    if not (title_selector or "").strip() or not (main_selector or "").strip():
        return None
    try:
        tree = parse_html_tree(html)
    except Exception:
        return None
    source_text = page_text if page_text is not None else _page_text(tree)
    apply_remove_selectors(tree, list(remove_selectors or []))
    title_el = _css_first(tree, title_selector)
    main_el = _css_first(tree, main_selector)
    if title_el is None or main_el is None:
        return None
    title = _clean(title_el.text_content())
    main = _clean(main_el.text_content())
    title_min = config.SUGGEST_MIN_TITLE_CHARS if min_title is None else min_title
    main_min = config.SUGGEST_MIN_MAIN_CHARS if min_main is None else min_main
    if len(title) < title_min or len(main) < main_min:
        return None
    if not overlaps_page_text(title, source_text) or not overlaps_page_text(main, source_text):
        return None
    return title, main


def _link_density(el: lxml_html.HtmlElement) -> float:
    text_len = len(_clean(el.text_content())) or 1
    link_len = 0
    for anchor in el.xpath(".//a"):
        link_len += len(_clean(anchor.text_content()))
    return link_len / text_len


def _has_chrome_ancestor(el: lxml_html.HtmlElement) -> bool:
    for ancestor in el.iterancestors():
        tag = _tag_name(ancestor)
        if tag in _CHROME_TAGS:
            return True
        if _NEGATIVE_RE.search(_attr_blob(ancestor)):
            return True
    return False


def score_main_node(el: lxml_html.HtmlElement, *, min_main: int) -> float:
    tag = _tag_name(el)
    if not tag or tag in _SKIP_TAGS or tag in {"html", "head", "body", "a", "ul", "ol", "li"}:
        return -1.0
    if tag not in _MAIN_TAGS and (el.get("role") or "").lower() != "main":
        return -1.0
    text = _clean(el.text_content())
    length = len(text)
    if length < min_main:
        return -1.0
    score = math.log(length + 1) * 10.0
    if tag == "article":
        score += 50
    elif tag == "main" or (el.get("role") or "").lower() == "main":
        score += 40
    elif tag == "section":
        score += 15
    elif tag == "div":
        score += 1
    blob = _attr_blob(el)
    if _POSITIVE_RE.search(blob):
        score += 25
    if _NEGATIVE_RE.search(blob):
        score -= 50
    density = _link_density(el)
    if density > 0.5:
        score -= 40
    elif density > 0.3:
        score -= 15
    else:
        score += 10
    p_count = len(el.xpath("./p"))
    score += min(p_count, 8) * 4
    if _has_chrome_ancestor(el):
        score -= 40
    return score


def score_title_node(el: lxml_html.HtmlElement, *, main_el: lxml_html.HtmlElement | None, min_title: int) -> float:
    tag = _tag_name(el)
    text = _clean(el.text_content())
    if len(text) < min_title:
        return -1.0
    score = 5.0
    if tag == "h1":
        score += 40
    elif tag == "h2":
        score += 15
    elif tag == "title":
        score += 8
    blob = _attr_blob(el)
    if _POSITIVE_RE.search(blob):
        score += 20
    if _NEGATIVE_RE.search(blob):
        score -= 30
    if main_el is not None:
        if main_el is el or _is_descendant(el, main_el) or _is_descendant(main_el, el):
            score += 25
    if _has_chrome_ancestor(el) and tag != "title":
        score -= 20
    return score


def _is_descendant(el: lxml_html.HtmlElement, ancestor: lxml_html.HtmlElement) -> bool:
    current = el.getparent()
    while current is not None:
        if current is ancestor:
            return True
        current = current.getparent()
    return False


def _nth_path(el: lxml_html.HtmlElement, *, max_depth: int = 8) -> str | None:
    parts: list[str] = []
    current: lxml_html.HtmlElement | None = el
    depth = 0
    while current is not None and depth < max_depth:
        tag = _tag_name(current)
        if not tag or tag in {"html"}:
            break
        parent = current.getparent()
        if parent is None or tag == "body":
            parts.append(tag)
            break
        siblings = [s for s in parent if _tag_name(s) == tag]
        if len(siblings) == 1:
            parts.append(tag)
        else:
            index = siblings.index(current) + 1
            parts.append(f"{tag}:nth-of-type({index})")
        current = parent
        depth += 1
    if not parts:
        return None
    return " > ".join(reversed(parts))


def iter_selector_candidates(el: lxml_html.HtmlElement) -> list[str]:
    """Stable CSS selector variants for one node (id / class / parent context / nth)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(selector: str | None) -> None:
        text = (selector or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        out.append(text)

    tag = _tag_name(el) or "*"
    eid = _safe_ident(el.get("id"))
    if eid:
        add(f"#{eid}")
        add(f"{tag}#{eid}")
    classes = _classes(el)
    for cls in classes[:4]:
        add(f"{tag}.{cls}")
        add(f".{cls}")
    if len(classes) >= 2:
        add(tag + "".join(f".{c}" for c in classes[:3]))
    parent = el.getparent()
    if parent is not None:
        ptag = _tag_name(parent)
        pid = _safe_ident(parent.get("id"))
        pclasses = _classes(parent)
        child = f"{tag}.{classes[0]}" if classes else tag
        if pid:
            add(f"#{pid} > {child}")
            add(f"#{pid} {child}")
        if ptag and pclasses:
            add(f"{ptag}.{pclasses[0]} {child}")
            add(f"{ptag}.{pclasses[0]} > {child}")
        if ptag in {"article", "main", "section"}:
            add(f"{ptag} {child}")
            add(f"{ptag} > {child}")
    add(_nth_path(el))
    return out


def _selector_rank(selector: str) -> int:
    if ":nth-" in selector:
        return 4
    if selector.startswith("#") or "#" in selector.split()[0]:
        return 0
    if "." in selector:
        return 1
    if " " in selector or ">" in selector:
        return 2
    return 3


def _unique_or_first_is(tree: lxml_html.HtmlElement, selector: str, target: lxml_html.HtmlElement) -> bool:
    matches = _css_all(tree, selector)
    return bool(matches) and matches[0] is target


def _collect_main_nodes(tree: lxml_html.HtmlElement, *, min_main: int) -> list[lxml_html.HtmlElement]:
    scored: list[tuple[float, int, lxml_html.HtmlElement]] = []
    for el in tree.iter():
        score = score_main_node(el, min_main=min_main)
        if score < 0:
            continue
        text_len = len(_clean(el.text_content()))
        scored.append((score, -text_len, el))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [el for _score, _neg_len, el in scored[:_MAX_MAIN_CANDIDATES]]


def _collect_title_nodes(
    tree: lxml_html.HtmlElement,
    *,
    main_el: lxml_html.HtmlElement | None,
    min_title: int,
) -> list[lxml_html.HtmlElement]:
    nodes: list[lxml_html.HtmlElement] = []
    for xpath in (".//h1", ".//h2", ".//*[@itemprop='headline']", ".//title"):
        nodes.extend(tree.xpath(xpath))
    scored: list[tuple[float, lxml_html.HtmlElement]] = []
    seen: set[int] = set()
    for el in nodes:
        ident = id(el)
        if ident in seen:
            continue
        seen.add(ident)
        score = score_title_node(el, main_el=main_el, min_title=min_title)
        if score >= 0:
            scored.append((score, el))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [el for _score, el in scored[:_MAX_TITLE_CANDIDATES]]


def _choose_remove_selectors(
    html: str,
    tree: lxml_html.HtmlElement,
    title_selector: str,
    main_selector: str,
    *,
    page_text: str,
) -> list[str]:
    chosen: list[str] = []
    for selector in _REMOVE_CANDIDATES:
        if not _css_all(tree, selector):
            continue
        trial = chosen + [selector]
        if self_test(html, title_selector, main_selector, trial, page_text=page_text):
            chosen.append(selector)
    return chosen


def _meta_selectors(tree: lxml_html.HtmlElement) -> dict[str, str]:
    meta: dict[str, str] = {}
    if _css_first(tree, "meta[name='description']") is not None:
        meta["description"] = "meta[name='description']"
    return meta


def _build_selector_set(
    html: str,
    tree: lxml_html.HtmlElement,
    *,
    title_selector: str,
    main_selector: str,
    page_text: str,
) -> SelectorSet | None:
    probed = self_test(html, title_selector, main_selector, [], page_text=page_text)
    if probed is None:
        return None
    remove = _choose_remove_selectors(html, tree, title_selector, main_selector, page_text=page_text)
    verified = self_test(html, title_selector, main_selector, remove, page_text=page_text)
    if verified is None:
        remove = []
        verified = probed
    title, main = verified
    return SelectorSet(
        title_selector=title_selector,
        main_selector=main_selector,
        remove_selectors=tuple(remove),
        title_preview=_preview(title, _TITLE_PREVIEW_CHARS),
        main_preview=_preview(main, _MAIN_PREVIEW_CHARS),
        meta=tuple(_meta_selectors(tree).items()),
    )


def suggest_heuristic(html: str, *, url: str) -> list[SelectorSet]:
    """Score the DOM and return passing selector sets, best first."""
    tree = parse_html_tree(html)
    page_text = _page_text(tree)
    min_title = config.SUGGEST_MIN_TITLE_CHARS
    min_main = config.SUGGEST_MIN_MAIN_CHARS
    main_nodes = _collect_main_nodes(tree, min_main=min_main)
    if not main_nodes:
        # Last resort: body itself if it has enough text.
        body = tree.find("body")
        if body is not None and len(_clean(body.text_content())) >= min_main:
            main_nodes = [body]
    passing: list[SelectorSet] = []
    seen_pair: set[tuple[str, str]] = set()
    for main_el in main_nodes or [tree]:
        title_nodes = _collect_title_nodes(tree, main_el=main_el, min_title=min_title)
        main_sels = [s for s in iter_selector_candidates(main_el) if _unique_or_first_is(tree, s, main_el)]
        if not main_sels:
            continue
        title_sels: list[str] = []
        for title_el in title_nodes:
            title_sels.extend(s for s in iter_selector_candidates(title_el) if _unique_or_first_is(tree, s, title_el))
        if not title_sels:
            continue
        # Dedup while preserving order.
        main_sels = list(dict.fromkeys(main_sels))
        title_sels = list(dict.fromkeys(title_sels))
        for main_sel in main_sels[:8]:
            for title_sel in title_sels[:5]:
                key = (title_sel, main_sel)
                if key in seen_pair:
                    continue
                seen_pair.add(key)
                built = _build_selector_set(
                    html,
                    tree,
                    title_selector=title_sel,
                    main_selector=main_sel,
                    page_text=page_text,
                )
                if built is not None:
                    passing.append(built)
    passing.sort(key=lambda item: (_selector_rank(item.main_selector), _selector_rank(item.title_selector), -len(item.main_preview)))
    # Unique by (title, main)
    uniq: list[SelectorSet] = []
    seen: set[tuple[str, str]] = set()
    for item in passing:
        key = (item.title_selector, item.main_selector)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def dom_skeleton(html: str, *, max_nodes: int | None = None, max_chars: int | None = None) -> str:
    """Compact tag/id/class outline for the LLM (~200 nodes / ~30k chars by default)."""
    node_limit = config.SUGGEST_SKELETON_MAX_NODES if max_nodes is None else max_nodes
    char_limit = config.SUGGEST_SKELETON_MAX_CHARS if max_chars is None else max_chars
    tree = parse_html_tree(html)
    lines: list[str] = []
    count = 0

    def walk(el: lxml_html.HtmlElement, depth: int) -> None:
        nonlocal count
        if count >= node_limit:
            return
        tag = _tag_name(el)
        if not tag or tag in _SKIP_TAGS:
            if tag in _SKIP_TAGS:
                return
            return
        attrs: list[str] = []
        eid = el.get("id")
        if eid:
            attrs.append(f'id="{eid.strip()[:40]}"')
        classes = (el.get("class") or "").split()
        if classes:
            attrs.append(f'class="{" ".join(classes[:4])}"')
        role = el.get("role")
        if role:
            attrs.append(f'role="{role.strip()[:20]}"')
        text = _clean(el.text or "")
        snippet = text[:60] if text else ""
        attr_s = (" " + " ".join(attrs)) if attrs else ""
        extra = f" {snippet}" if snippet else ""
        lines.append(f"{'  ' * depth}<{tag}{attr_s}>{extra}")
        count += 1
        for child in el:
            if count >= node_limit:
                return
            walk(child, depth + 1)

    walk(tree, 0)
    blob = "\n".join(lines)
    if len(blob) > char_limit:
        return blob[:char_limit]
    return blob


def _draft_payload(item: SelectorSet | None) -> dict[str, object]:
    if item is None:
        return {}
    return {
        "title_selector": item.title_selector,
        "main_selector": item.main_selector,
        "remove_selectors": list(item.remove_selectors),
        "meta": dict(item.meta),
    }


def _response_from_set(
    item: SelectorSet,
    *,
    url: str,
    strategy_used: str,
    alternatives: list[SelectorSet],
    llm_skipped: str | None = None,
) -> SuggestResponse:
    host = host_from_url(url)
    if not host:
        raise extract_empty("Could not derive a profile host from the URL")
    profile_id = profile_id_from_host(host)
    variants = [alt.as_variant() for alt in alternatives[:_MAX_ALTERNATIVES]]
    evidence = SuggestEvidence(
        strategy_used=strategy_used,  # type: ignore[arg-type]
        title_preview=item.title_preview,
        main_preview=item.main_preview,
        alternatives=variants or None,
        llm_skipped=llm_skipped,
    )
    return SuggestResponse(profile=item.as_profile(profile_id=profile_id, host=host), evidence=evidence)


def selector_set_from_payload(html: str, payload: dict[str, object], *, url: str) -> SelectorSet | None:
    """lxml-verify an LLM (or other) selector payload. None if it fails self-test."""
    title = str(payload.get("title_selector") or "").strip()
    main = str(payload.get("main_selector") or "").strip()
    raw_remove = payload.get("remove_selectors") or []
    remove = [str(x).strip() for x in raw_remove if str(x).strip()] if isinstance(raw_remove, list) else []
    verified = self_test(html, title, main, remove)
    if verified is None:
        # Try without remove_selectors; LLM may over-strip.
        if remove:
            verified = self_test(html, title, main, [])
            remove = []
        if verified is None:
            return None
    title_text, main_text = verified
    tree = parse_html_tree(html)
    raw_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    meta_items: list[tuple[str, str]] = []
    if isinstance(raw_meta, dict):
        for key, value in raw_meta.items():
            selector = "" if value is None else str(value).strip()
            if not isinstance(key, str) or not selector:
                continue
            if _css_first(tree, selector) is not None:
                meta_items.append((key, selector))
    if not meta_items:
        meta_items = list(_meta_selectors(tree).items())
    return SelectorSet(
        title_selector=title,
        main_selector=main,
        remove_selectors=tuple(remove),
        title_preview=_preview(title_text, _TITLE_PREVIEW_CHARS),
        main_preview=_preview(main_text, _MAIN_PREVIEW_CHARS),
        meta=tuple(meta_items),
    )


async def suggest_from_html(html: str, *, url: str, strategy: SuggestStrategy = "heuristic") -> SuggestResponse:
    """Build one primary profile plus optional alternatives. Does not write disk."""
    heuristic_sets = suggest_heuristic(html, url=url)
    primary = heuristic_sets[0] if heuristic_sets else None
    alternatives = heuristic_sets[1:]

    if strategy == "heuristic":
        if primary is None:
            raise extract_empty("Could not suggest a site profile")
        return _response_from_set(primary, url=url, strategy_used="heuristic", alternatives=alternatives)

    if not profile_llm.llm_configured():
        if strategy == "llm":
            raise llm_unavailable("LLM is not configured")
        if primary is None:
            raise extract_empty("Could not suggest a site profile")
        return _response_from_set(
            primary,
            url=url,
            strategy_used="heuristic",
            alternatives=alternatives,
            llm_skipped="no_key",
        )

    skeleton = dom_skeleton(html)
    try:
        payload = await profile_llm.propose_selectors(
            url=url,
            skeleton=skeleton,
            draft=_draft_payload(primary),
        )
        verified = selector_set_from_payload(html, payload, url=url)
        if verified is None:
            raise llm_failed("LLM selectors failed lxml verification")
        other = [item for item in heuristic_sets if (item.title_selector, item.main_selector) != (verified.title_selector, verified.main_selector)]
        return _response_from_set(verified, url=url, strategy_used="llm", alternatives=other)
    except ArachneError as exc:
        if exc.code != LLM_FAILED:
            raise
        if strategy == "llm":
            raise
        logger.warning("llm suggest failed; falling back to heuristic")
        if primary is None:
            raise
        return _response_from_set(
            primary,
            url=url,
            strategy_used="heuristic",
            alternatives=alternatives,
            llm_skipped="llm_failed",
        )
    except Exception as exc:
        wrapped = llm_failed("LLM profile suggestion failed")
        if strategy == "llm":
            raise wrapped from exc
        logger.warning("llm suggest failed; falling back to heuristic")
        if primary is None:
            raise wrapped from exc
        return _response_from_set(
            primary,
            url=url,
            strategy_used="heuristic",
            alternatives=alternatives,
            llm_skipped="llm_failed",
        )
