"""Unit tests for HTML extraction, link ranking, and content-area images."""

from __future__ import annotations

import pytest
from lxml import html as lxml_html

from app.config import LINK_TEXT_MAX_CHARS, MAX_IMAGES, MAX_LINKS
from app.errors import ArachneError
from app.extract import collect_images, collect_links, extract_html
from app.profiles.apply import extract_with_profile
from app.profiles.schema import SiteProfile
from tests.conftest import EMPTY_HTML, IMAGES_HTML, SAMPLE_HTML


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
    assert result.images == []
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


def test_images_from_article_skip_chrome_trackers_and_absolutize():
    result = _extract(IMAGES_HTML)
    urls = [img.url for img in result.images]
    alts = {img.url: img.alt for img in result.images}
    assert urls == [
        "https://www.example.com/photos/hero.jpg",
        "https://www.example.com/pics/inline.png",
        "https://cdn.example.com/a.jpg",
    ]
    assert alts["https://www.example.com/photos/hero.jpg"] == "Hero photo"
    assert alts["https://www.example.com/pics/inline.png"] == "Inline"
    assert alts["https://cdn.example.com/a.jpg"] == "CDN"
    assert "https://www.example.com/chrome/logo.png" not in urls
    assert "https://www.example.com/chrome/footer.png" not in urls
    assert "https://www.example.com/tracker/tiny.gif" not in urls
    assert "https://www.example.com/tracker/short.gif" not in urls
    assert not any(u.startswith("data:") for u in urls)
    assert result.metadata.og.image == "https://www.example.com/images/og.png"
    assert result.metadata.og.image not in urls


def test_og_image_is_not_copied_into_images():
    html = (
        "<html><head><title>OG only</title>"
        '<meta property="og:image" content="https://cdn.example.com/cover.jpg">'
        "</head><body><p>enough text here for the extractor to keep.</p></body></html>"
    )
    result = _extract(html)
    assert result.metadata.og.image == "https://cdn.example.com/cover.jpg"
    assert result.images == []


def test_empty_images_when_no_qualifying_img():
    html = (
        "<html><head><title>No pics</title></head>"
        "<body><article><p>enough text here for the extractor to keep.</p>"
        '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
        '<img width="1" height="1" src="/pixel.gif">'
        "<img src=\"\">"
        "</article></body></html>"
    )
    result = _extract(html)
    assert result.images == []
    assert result.title


def test_images_prefer_main_then_body_and_missing_alt_is_empty():
    main_html = (
        "<html><head><title>Main region</title></head><body>"
        '<img src="/outside.jpg" alt="chrome">'
        "<main><p>enough text here for the extractor to keep.</p>"
        '<img src="/inside.jpg">'
        "</main></body></html>"
    )
    main_result = _extract(main_html)
    assert [(img.url, img.alt) for img in main_result.images] == [
        ("https://www.example.com/inside.jpg", ""),
    ]

    body_html = (
        "<html><head><title>Body region</title></head><body>"
        "<p>enough text here for the extractor to keep.</p>"
        '<img src="/only.jpg" alt="Only">'
        "</body></html>"
    )
    body_result = _extract(body_html)
    assert [(img.url, img.alt) for img in body_result.images] == [
        ("https://www.example.com/only.jpg", "Only"),
    ]


def test_collect_images_dedupes_and_caps():
    imgs = []
    for i in range(15):
        imgs.append(f'<img src="/p/{i}.jpg" alt="n{i}">')
    imgs.append('<img src="/p/0.jpg" alt="dup">')
    html = (
        "<html><body><article><p>enough text here.</p>"
        + "".join(imgs)
        + "</article></body></html>"
    )
    tree = lxml_html.fromstring(html)
    images = collect_images(tree, "https://www.example.com/page")
    assert len(images) == MAX_IMAGES
    assert images[0].url == "https://www.example.com/p/0.jpg"
    assert images[0].alt == "n0"
    assert [img.url for img in images] == [
        f"https://www.example.com/p/{i}.jpg" for i in range(MAX_IMAGES)
    ]
    capped = collect_images(tree, "https://www.example.com/page", limit=2)
    assert [img.url for img in capped] == [
        "https://www.example.com/p/0.jpg",
        "https://www.example.com/p/1.jpg",
    ]


def test_profile_main_selector_limits_image_scan():
    html = (
        "<html><head><title>Wrong</title>"
        '<meta property="og:image" content="/images/og.png">'
        "</head><body>"
        '<img src="/chrome.jpg" alt="chrome">'
        '<article>'
        '<h1 class="article-title">Profile Title</h1>'
        '<div class="content">'
        "Profile main text that is unique enough for assertions. "
        "Second sentence of the article body."
        '<img src="/in-content.jpg" alt="inside">'
        "</div>"
        '<img src="/in-article-outside-content.jpg" alt="outside content">'
        "</article>"
        "</body></html>"
    )
    profile = SiteProfile.model_validate(
        {
            "id": "example-com",
            "version": "1",
            "hosts": ["example.com"],
            "title_selector": "h1.article-title",
            "main_selector": "article .content",
        }
    )
    result = extract_with_profile(
        html,
        profile=profile,
        requested_url="https://example.com/article",
        final_url="https://example.com/article",
        status_code=200,
        content_type="text/html",
    )
    assert result.profile_fallback is False
    assert [(img.url, img.alt) for img in result.images] == [
        ("https://example.com/in-content.jpg", "inside"),
    ]
    assert result.metadata.og.image == "https://example.com/images/og.png"
    assert result.metadata.og.image not in {img.url for img in result.images}
