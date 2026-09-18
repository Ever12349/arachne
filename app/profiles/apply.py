"""Apply a site profile to fetched HTML, falling back to generic extract when allowed."""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from lxml import html as lxml_html
from lxml.etree import ParseError

from app.errors import extract_empty
from app.extract import _clean, _html_lang, _meta_map, collect_links, extract_html
from app.models import ExtractResponse, OgMetadata, PageMetadata
from app.profiles.schema import SiteProfile

logger = logging.getLogger("arachne")


def _parse_html(html: str) -> lxml_html.HtmlElement:
    try:
        return lxml_html.fromstring(html)
    except (ParseError, TypeError, ValueError) as exc:
        raise extract_empty("HTML could not be parsed") from exc
    except Exception as exc:  # pragma: no cover - lxml recovers from most input
        raise extract_empty("HTML could not be parsed") from exc


def _css_all(tree: lxml_html.HtmlElement, selector: str) -> list[lxml_html.HtmlElement]:
    text = (selector or "").strip()
    if not text:
        return []
    try:
        return list(tree.cssselect(text))
    except Exception:
        logger.warning("invalid CSS selector %r", text)
        return []


def _css_first(tree: lxml_html.HtmlElement, selector: str) -> lxml_html.HtmlElement | None:
    matches = _css_all(tree, selector)
    return matches[0] if matches else None


def _element_string(el: lxml_html.HtmlElement) -> str:
    content = el.get("content")
    if content:
        return _clean(content)
    return _clean(el.text_content())


def apply_remove_selectors(tree: lxml_html.HtmlElement, selectors: list[str]) -> None:
    for selector in selectors:
        for node in _css_all(tree, selector):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)


def _serialize(tree: lxml_html.HtmlElement) -> str:
    return lxml_html.tostring(tree, encoding="unicode", method="html")


def _overlay_meta(metadata: PageMetadata, tree: lxml_html.HtmlElement, profile: SiteProfile) -> PageMetadata:
    updates: dict[str, str] = {}
    for key, selector in (profile.meta or {}).items():
        el = _css_first(tree, selector)
        if el is None:
            continue
        value = _element_string(el)
        if key == "description":
            updates["description"] = value
        elif key == "language":
            updates["language"] = value
    if not updates:
        return metadata
    return metadata.model_copy(update=updates)


def _attach_profile(
    response: ExtractResponse,
    profile: SiteProfile,
    *,
    fallback: bool,
    tree: lxml_html.HtmlElement | None = None,
) -> ExtractResponse:
    updates: dict[str, object] = {
        "profile_id": profile.id,
        "profile_version": profile.version,
        "profile_fallback": fallback,
    }
    if profile.disable_links:
        updates["links"] = []
    if tree is not None:
        updates["metadata"] = _overlay_meta(response.metadata, tree, profile)
    return response.model_copy(update=updates)


def _profile_success_response(
    *,
    tree: lxml_html.HtmlElement,
    profile: SiteProfile,
    title: str,
    main_text: str,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
) -> ExtractResponse:
    description, og = _meta_map(tree)
    if og.image:
        og = og.model_copy(update={"image": urljoin(final_url, og.image)})
    language = _html_lang(tree)
    links = [] if profile.disable_links else collect_links(tree, final_url)
    metadata = PageMetadata(
        description=_clean(description) or _clean(og.description),
        language=language,
        content_type=content_type,
        og=OgMetadata(
            title=_clean(og.title),
            description=_clean(og.description),
            image=og.image,
        ),
    )
    metadata = _overlay_meta(metadata, tree, profile)
    return ExtractResponse(
        url=final_url,
        requested_url=requested_url,
        status_code=status_code,
        title=title,
        main_text=main_text,
        metadata=metadata,
        links=links,
        truncated=False,
        profile_id=profile.id,
        profile_version=profile.version,
        profile_fallback=False,
    )


def extract_with_profile(
    html: str,
    *,
    profile: SiteProfile | None,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
) -> ExtractResponse:
    """Run profile CSS extract, or the generic trafilatura path when no profile is selected."""
    if profile is None:
        return extract_html(
            html,
            requested_url=requested_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
        )

    tree = _parse_html(html)
    apply_remove_selectors(tree, profile.remove_selectors)

    title = ""
    if (profile.title_selector or "").strip():
        el = _css_first(tree, profile.title_selector)
        if el is not None:
            title = _clean(el.text_content())

    main_text = ""
    if (profile.main_selector or "").strip():
        el = _css_first(tree, profile.main_selector)
        if el is not None:
            main_text = _clean(el.text_content())

    if title or main_text:
        return _profile_success_response(
            tree=tree,
            profile=profile,
            title=title,
            main_text=main_text,
            requested_url=requested_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
        )

    if profile.strict:
        raise extract_empty()

    cleaned = _serialize(tree)
    generic = extract_html(
        cleaned,
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        content_type=content_type,
    )
    return _attach_profile(generic, profile, fallback=True, tree=tree)
