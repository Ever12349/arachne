"""SSRF: http(s) only; reject private/loopback/link-local/unspecified."""

from __future__ import annotations

import pytest

from app.errors import ArachneError
from app.ssrf import assert_public_http_url, is_blocked_ip, normalize_host, parse_http_url


def test_normalize_host_strips_leading_www():
    assert normalize_host("www.Example.com") == "example.com"
    assert normalize_host("example.com") == "example.com"
    assert normalize_host("www.www.example.com") == "www.example.com"


@pytest.mark.parametrize(
    "raw",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.5.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "::",
        "fc00::1",
        "::ffff:127.0.0.1",
    ],
)
def test_blocked_ips(raw: str):
    assert is_blocked_ip(raw) is True


def test_public_ip_allowed():
    assert is_blocked_ip("8.8.8.8") is False
    assert is_blocked_ip("93.184.216.34") is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "javascript:alert(1)",
        "mailto:a@b.com",
        "not-a-url",
        "",
        "http://",
        "https://",
    ],
)
@pytest.mark.asyncio
async def test_bad_scheme_or_host(url: str):
    with pytest.raises(ArachneError) as exc:
        await assert_public_http_url(url)
    assert exc.value.code == "bad_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/secret",
        "http://localhost/",
        "http://10.1.2.3/",
        "http://192.168.0.5/x",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://127.1/",
    ],
)
@pytest.mark.asyncio
async def test_rejects_non_public_targets(url: str):
    with pytest.raises(ArachneError) as exc:
        await assert_public_http_url(url)
    assert exc.value.code == "bad_url"


@pytest.mark.asyncio
async def test_public_literal_ip_passes_ssrf():
    await assert_public_http_url("http://8.8.8.8/")


@pytest.mark.asyncio
async def test_public_hostname_with_mocked_dns(public_dns):
    await assert_public_http_url("https://example.com/path")


def test_parse_http_url_defaults_port():
    scheme, host, port = parse_http_url("https://Example.com/a")
    assert scheme == "https"
    assert host.lower() == "example.com"
    assert port == 443
    scheme, host, port = parse_http_url("http://example.com:8080/")
    assert port == 8080
