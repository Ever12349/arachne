"""Unit tests for HTML extraction and link ranking."""

from __future__ import annotations

import pytest
from lxml import html as lxml_html

from app.config import LINK_TEXT_MAX_CHARS, MAX_LINKS
from app.errors import ArachneError
from app.extract import collect_links, extract_html
from tests.conftest import EMPTY_HTML, SAMPLE_HTML


def _extract(html: str, url: str = "https://www.example.com/article"):
    return extract_html(
        html,
        requested_url="https://www.example.com/article",
        final_url=url,
        status_code=200,
        content_type="text/html; charset=utf-8",
    )


def test_sample_html_extracts_title_text_metadata_and_links():
    result = _extract(SAMPLE_HTML)
    assert result.title
    assert result.main_text
    assert "main article text" in result.main_text.lower() or "Sample Page Title" in result.title
    assert result.metadata.description == "A short description."
    assert result.metadata.language == "en"
    assert result.metadata.content_type == "text/html; charset=utf-8"
    assert result.metadata.og.title == "OG Title"
    assert result.metadata.og.description == "OG Description"
    assert result.metadata.og.image == "https://www.example.com/images/og.png"
    hrefs = [link.href for link in result.links]
    texts = {link.href: link.text for link in result.links}
    assert "https://www.example.com/about" in hrefs
    assert texts["https://www.example.com/about"] == "About"
    assert "mailto:test@example.com" not in hrefs
    assert not any(h.startswith("javascript:") for h in hrefs)
    assert not any(h.startswith("tel:") for h in hrefs)
    assert "https://www.example.com/article#section" not in hrefs
    # same-host (www. stripped) before external
    other_index = hrefs.index("https://other.test/x")
    same_host_hrefs = [h for h in hrefs if "other.test" not in h]
    assert hrefs.index(same_host_hrefs[0]) < other_index


def test_empty_html_is_extract_empty():
    with pytest.raises(ArachneError) as exc:
        _extract(EMPTY_HTML)
    assert exc.value.code == "extract_empty"


def test_title_only_is_success():
    html = "<html><head><title>Only Title</title></head><body></body></html>"
    result = _extract(html)
    assert result.title == "Only Title"
    assert result.truncated is False


def test_links_cap_and_same_host_priority():
    anchors = []
    for i in range(40):
        anchors.append(f'<a href="https://other.test/{i}">ext {i}</a>')
    for i in range(20):
        anchors.append(f'<a href="https://example.com/p/{i}">local {i}</a>')
    html = (
        "<html><head><title>Links</title></head><body><p>text for extractor "
        "with enough content to keep.</p>" + "".join(anchors) + "</body></html>"
    )
    tree = lxml_html.fromstring(html)
    links = collect_links(tree, "https://www.example.com/page", limit=MAX_LINKS)
    assert len(links) == MAX_LINKS
    assert all(link.href.startswith("https://example.com/") for link in links[:20])
    assert all(link.href.startswith("https://other.test/") for link in links[20:])


def test_skips_fragment_tel_and_caps_link_text():
    long_text = "L" * 500
    html = (
        "<html><head><title>Links</title></head><body><p>enough text here.</p>"
        '<a href="#section">frag</a>'
        '<a href="#">hash</a>'
        '<a href="tel:+15551212">call</a>'
        f'<a href="https://www.example.com/p">{long_text}</a>'
        "</body></html>"
    )
    tree = lxml_html.fromstring(html)
    links = collect_links(tree, "https://www.example.com/page")
    hrefs = [link.href for link in links]
    assert hrefs == ["https://www.example.com/p"]
    assert len(links[0].text) == LINK_TEXT_MAX_CHARS
