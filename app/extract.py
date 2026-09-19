"""HTML → title, main_text, metadata, links, images (trafilatura + lxml scans)."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import trafilatura
from lxml import html as lxml_html

from app.config import LINK_TEXT_MAX_CHARS, MAIN_TEXT_MAX_CHARS, MAX_IMAGES, MAX_LINKS
from app.errors import extract_empty
from app.models import ExtractResponse, ImageItem, Link, OgMetadata, PageMetadata
from app.ssrf import normalize_host

_DIM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*$", re.IGNORECASE)


def _clean(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split()).strip()


def _meta_map(tree: lxml_html.HtmlElement) -> tuple[str, OgMetadata]:
    description = ""
    og_title = ""
    og_description = ""
    og_image = ""
    for el in tree.xpath("//meta"):
        key = (el.get("property") or el.get("name") or "").strip().lower()
        val = (el.get("content") or "").strip()
        if not key or not val:
            continue
        if key == "description" and not description:
            description = val
        elif key == "og:title" and not og_title:
            og_title = val
        elif key == "og:description" and not og_description:
            og_description = val
        elif key == "og:image" and not og_image:
            og_image = val
    return description, OgMetadata(title=og_title, description=og_description, image=og_image)


def _html_title(tree: lxml_html.HtmlElement) -> str:
    el = tree.find(".//title")
    if el is None:
        return ""
    return _clean(el.text_content())


def _html_lang(tree: lxml_html.HtmlElement) -> str:
    lang = (tree.get("lang") or "").strip()
    if lang:
        return lang
    for el in tree.xpath("//html"):
        lang = (el.get("lang") or "").strip()
        if lang:
            return lang
    return ""


def _should_skip_href(raw: str) -> bool:
    lowered = raw.strip().lower()
    if not lowered:
        return True
    if lowered.startswith("#"):
        return True
    return (
        lowered.startswith("javascript:")
        or lowered.startswith("mailto:")
        or lowered.startswith("tel:")
    )


def collect_links(tree: lxml_html.HtmlElement, base_url: str, limit: int = MAX_LINKS) -> list[Link]:
    """Absolute http(s) <a href> links; same-host first (www. stripped); cap `limit`."""
    page_host = normalize_host(urlparse(base_url).hostname)
    same_host: list[Link] = []
    other: list[Link] = []
    seen: set[str] = set()

    for el in tree.xpath("//a[@href]"):
        raw = (el.get("href") or "").strip()
        if _should_skip_href(raw):
            continue
        absolute = urljoin(base_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        text = _clean(el.text_content())
        if len(text) > LINK_TEXT_MAX_CHARS:
            text = text[:LINK_TEXT_MAX_CHARS]
        link = Link(href=absolute, text=text)
        if normalize_host(parsed.hostname) == page_host:
            same_host.append(link)
        else:
            other.append(link)

    return (same_host + other)[:limit]


def _content_region(
    tree: lxml_html.HtmlElement,
    main_el: lxml_html.HtmlElement | None = None,
) -> lxml_html.HtmlElement:
    """Prefer a profile main subtree, else <article>, <main>, then <body>."""
    if main_el is not None:
        return main_el
    for xpath in (".//article", ".//main", ".//body"):
        matches = tree.xpath(xpath)
        if matches:
            return matches[0]
    return tree


def _parse_dimension(raw: str | None) -> float | None:
    if raw is None:
        return None
    match = _DIM_RE.match(raw)
    if not match:
        return None
    return float(match.group(1))


def _is_tracking_pixel(el: lxml_html.HtmlElement) -> bool:
    width = _parse_dimension(el.get("width"))
    height = _parse_dimension(el.get("height"))
    if width is not None and width <= 2:
        return True
    if height is not None and height <= 2:
        return True
    return width == 1 and height == 1


def _absolute_http_url(raw: str, base_url: str) -> str | None:
    text = (raw or "").strip()
    if not text or text.lower().startswith("data:"):
        return None
    absolute = urljoin(base_url, text)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return absolute


def collect_images(
    tree: lxml_html.HtmlElement,
    base_url: str,
    *,
    main_el: lxml_html.HtmlElement | None = None,
    limit: int | None = None,
) -> list[ImageItem]:
    """Content-region <img> list; absolute http(s); skip trackers; cap `limit`."""
    cap = MAX_IMAGES if limit is None else int(limit)
    if cap <= 0:
        return []

    region = _content_region(tree, main_el)
    seen: set[str] = set()
    images: list[ImageItem] = []
    for el in region.xpath(".//img"):
        absolute = _absolute_http_url(el.get("src") or "", base_url)
        if absolute is None or absolute in seen or _is_tracking_pixel(el):
            continue
        seen.add(absolute)
        alt = el.get("alt")
        images.append(ImageItem(url=absolute, alt="" if alt is None else alt))
        if len(images) >= cap:
            break
    return images


def _fallback_text(tree: lxml_html.HtmlElement) -> str:
    body = tree.find("body")
    target = body if body is not None else tree
    for el in target.xpath(".//script|.//style|.//noscript|.//template"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    return _clean(target.text_content())


def extract_html(
    html: str,
    *,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
) -> ExtractResponse:
    try:
        tree = lxml_html.fromstring(html)
    except Exception as exc:  # pragma: no cover - lxml recovers from most input
        raise extract_empty("HTML could not be parsed") from exc

    description, og = _meta_map(tree)
    if og.image:
        og = og.model_copy(update={"image": urljoin(final_url, og.image)})

    links = collect_links(tree, final_url)
    images = collect_images(tree, final_url)

    traf_text = trafilatura.extract(
        html,
        url=final_url,
        include_comments=False,
        include_links=False,
        favor_recall=True,
    ) or ""
    traf_meta = trafilatura.extract_metadata(html, default_url=final_url)

    title = _clean(traf_meta.title if traf_meta else "") or _html_title(tree) or _clean(og.title)
    main_text = _clean(traf_text) or _fallback_text(tree)

    if not title and not main_text:
        raise extract_empty()

    language = _html_lang(tree) or _clean(traf_meta.language if traf_meta else "")

    return ExtractResponse(
        url=final_url,
        requested_url=requested_url,
        status_code=status_code,
        title=title,
        main_text=main_text,
        metadata=PageMetadata(
            description=_clean(description) or _clean(og.description),
            language=language,
            content_type=content_type,
            og=OgMetadata(
                title=_clean(og.title),
                description=_clean(og.description),
                image=og.image,
            ),
        ),
        links=links,
        images=images,
        truncated=False,
    )


def apply_max_chars(response: ExtractResponse, max_chars: int | None) -> ExtractResponse:
    """Cap main_text after cache: omit max_chars → hard cap; else clamp to [1, hard cap]."""
    if max_chars is None:
        limit = MAIN_TEXT_MAX_CHARS
    else:
        limit = max(1, min(int(max_chars), MAIN_TEXT_MAX_CHARS))
    truncated = len(response.main_text) > limit
    text = response.main_text[:limit] if truncated else response.main_text
    if text == response.main_text and truncated == response.truncated:
        return response
    return response.model_copy(update={"main_text": text, "truncated": truncated})
