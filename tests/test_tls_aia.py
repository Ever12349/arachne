"""Incomplete-chain AIA recovery. Network is mocked; no live origin."""

from __future__ import annotations

import logging
import ssl
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.antibot import fetch_with_retries, is_retryable
from app.config import auto_render_on_codes
from app.errors import ArachneError, fetch_failed
from app.fetch import fetch_url, ssrf_request_hook
from app.tls_aia import (
    aia_timeout,
    build_verify_context,
    clear_caches,
    download_aia_bytes,
    fetch_peer_certificate_der,
    is_incomplete_chain_error,
    is_ssl_connect_error,
    issuer_cache_key,
    load_ca_bundle,
    parse_pem_certificates,
    peer_capture_ssl_context,
    peer_certificate_der,
    recover_verify_context,
    resolve_verify,
)

AIA_URL = "http://aia.test/intermediate.cer"
CHAIN_MESSAGE = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "unable to get local issuer certificate (_ssl.c:1000)"
)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, cn)])


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(
    subject: x509.Name,
    issuer: x509.Name,
    subject_key,
    issuer_key,
    *,
    is_ca: bool = False,
    aia: str | None = None,
    aki_from: x509.Certificate | None = None,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()),
            critical=False,
        )
    )
    if is_ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    if aki_from is not None:
        ski = aki_from.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
            critical=False,
        )
    if aia:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        x509.OID_CA_ISSUERS,
                        x509.UniformResourceIdentifier(aia),
                    )
                ]
            ),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


class _Chain:
    def __init__(self) -> None:
        root_key = _key()
        root_name = _name("arachne-test-root")
        self.root = _sign(root_name, root_name, root_key, root_key, is_ca=True)
        inter_key = _key()
        inter_name = _name("arachne-test-intermediate")
        self.intermediate = _sign(
            inter_name,
            root_name,
            inter_key,
            root_key,
            is_ca=True,
            aki_from=self.root,
        )
        leaf_key = _key()
        self.leaf = _sign(
            _name("example.com"),
            inter_name,
            leaf_key,
            inter_key,
            aia=AIA_URL,
            aki_from=self.intermediate,
        )
        self.leaf_no_aki = _sign(
            _name("example.com"),
            inter_name,
            _key(),
            inter_key,
            aia=AIA_URL,
        )
        self.leaf_no_aki_b = _sign(_name("other.example"), inter_name, _key(), inter_key)
        other_key = _key()
        other = _sign(_name("other-intermediate"), root_name, other_key, root_key, is_ca=True, aki_from=self.root)
        self.leaf_other_aki = _sign(
            _name("example.com"),
            other.subject,
            _key(),
            other_key,
            aia=AIA_URL,
            aki_from=other,
        )
        self.other_intermediate = other

    def install_bundle(self, path, monkeypatch: pytest.MonkeyPatch) -> None:
        path.write_bytes(self.root.public_bytes(serialization.Encoding.PEM))
        monkeypatch.setattr("app.tls_aia.CA_BUNDLE_PATH", str(path))
        clear_caches()


def _der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _chain_error(message: str = CHAIN_MESSAGE) -> httpx.ConnectError:
    return httpx.ConnectError(message, request=httpx.Request("GET", "https://example.com/article"))


class _FakeSSL:
    def __init__(self, der: bytes) -> None:
        self._der = der

    def getpeercert(self, binary_form: bool = False):
        assert binary_form is True
        return self._der


class _FakeStream:
    def __init__(self, der: bytes) -> None:
        self._der = der

    def get_extra_info(self, info: str):
        if info == "ssl_object":
            return _FakeSSL(self._der)
        return None


def test_classification_is_chain_only_for_missing_issuer():
    chain = _chain_error()
    assert is_incomplete_chain_error(chain) is True
    assert is_ssl_connect_error(chain) is True
    expired = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired"
    )
    assert is_incomplete_chain_error(expired) is False
    assert is_ssl_connect_error(expired) is True
    mismatch = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, "
        "certificate is not valid for 'example.com'"
    )
    assert is_incomplete_chain_error(mismatch) is False
    assert is_ssl_connect_error(mismatch) is True
    self_signed = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    )
    assert is_incomplete_chain_error(self_signed) is False
    in_chain = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate in certificate chain"
    )
    assert is_incomplete_chain_error(in_chain) is True
    first = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to verify the first certificate"
    )
    assert is_incomplete_chain_error(first) is True
    refused = httpx.ConnectError("connection refused")
    assert is_incomplete_chain_error(refused) is False
    assert is_ssl_connect_error(refused) is False


def test_ca_bundle_missing_and_empty_are_ignored(tmp_path, caplog: pytest.LogCaptureFixture):
    missing = tmp_path / "missing.pem"
    with caplog.at_level(logging.WARNING, logger="arachne"):
        assert load_ca_bundle(str(missing)) == []
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"\n\n")
    with caplog.at_level(logging.WARNING, logger="arachne"):
        assert load_ca_bundle(str(empty)) == []
    assert "ignoring" not in caplog.text


def test_ca_bundle_invalid_pem_is_soft_fail(tmp_path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "ca-bundle.pem"
    path.write_bytes(b"-----BEGIN CERTIFICATE-----\nnot-a-cert\n-----END CERTIFICATE-----\n")
    with caplog.at_level(logging.WARNING, logger="arachne"):
        assert load_ca_bundle(str(path)) == []
    assert "ignoring invalid ca bundle" in caplog.text
    assert "not-a-cert" not in caplog.text


def test_ca_bundle_multi_pem_and_verify_context(tmp_path, monkeypatch: pytest.MonkeyPatch):
    root_key = _key()
    root_name = _name("bundle-root")
    first = _sign(root_name, root_name, root_key, root_key, is_ca=True)
    second = _sign(_name("bundle-other"), _name("bundle-other"), _key(), _key(), is_ca=True)
    blob = _pem(first) + b"\n" + _pem(second)
    path = tmp_path / "ca-bundle.pem"
    path.write_bytes(blob)
    loaded = load_ca_bundle(str(path))
    assert [cert.subject for cert in loaded] == [first.subject, second.subject]
    ctx = build_verify_context(loaded)
    ders = ctx.get_ca_certs(True)
    assert _der(first) in ders
    assert _der(second) in ders
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    explicit = peer_capture_ssl_context()
    assert resolve_verify(explicit) is explicit
    monkeypatch.setattr("app.tls_aia.CA_BUNDLE_PATH", str(path))
    clear_caches()
    resolved = resolve_verify(True)
    assert isinstance(resolved, ssl.SSLContext)
    assert resolved.verify_mode == ssl.CERT_REQUIRED
    assert _der(first) in resolved.get_ca_certs(True)
    assert _der(second) in resolved.get_ca_certs(True)


def test_issuer_cache_key_uses_aki_or_issuer():
    chain = _Chain()
    keyed = issuer_cache_key(chain.leaf)
    assert "|" in keyed
    assert issuer_cache_key(chain.leaf) == keyed
    plain = issuer_cache_key(chain.leaf_no_aki)
    assert "|" not in plain
    assert issuer_cache_key(chain.leaf_no_aki_b) == plain
    assert issuer_cache_key(chain.leaf_other_aki) != keyed


def test_parse_pem_rejects_trailing_junk():
    cert = _sign(_name("solo"), _name("solo"), _key(), _key(), is_ca=True)
    with pytest.raises(ValueError):
        parse_pem_certificates(_pem(cert) + b"junk")


@pytest.mark.asyncio
async def test_peer_der_and_leaf_client_is_cert_none(monkeypatch: pytest.MonkeyPatch):
    sentinel = b"\x30\x03leaf"
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"page-body-must-not-be-the-certificate",
            extensions={"network_stream": _FakeStream(sentinel)},
            request=request,
        )

    def fake_create(verify: bool | ssl.SSLContext = True, *, timeout: httpx.Timeout | None = None):
        seen["verify"] = verify
        seen["timeout"] = timeout
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.fetch.create_http_client", fake_create)
    response = httpx.Response(200, extensions={"network_stream": _FakeStream(sentinel)})
    assert peer_certificate_der(response) == sentinel
    der = await fetch_peer_certificate_der("https://example.com/article")
    assert der == sentinel
    verify = seen["verify"]
    assert isinstance(verify, ssl.SSLContext)
    assert verify.verify_mode == ssl.CERT_NONE
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 5.0


@pytest.mark.asyncio
async def test_aia_download_blocks_non_public_and_uses_verified_client(
    public_dns,
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(ArachneError) as exc:
        await download_aia_bytes("http://127.0.0.1/ca.crt")
    assert exc.value.code == "bad_url"
    with pytest.raises(ArachneError) as exc:
        await download_aia_bytes("http://10.1.2.3/ca.crt")
    assert exc.value.code == "bad_url"

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"der-bytes", request=request)

    def fake_create(verify: bool | ssl.SSLContext = True, *, timeout: httpx.Timeout | None = None):
        seen["verify"] = verify
        seen["timeout"] = timeout
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.fetch.create_http_client", fake_create)
    body = await download_aia_bytes("https://example.com/intermediate.cer")
    assert body == b"der-bytes"
    assert seen["url"] == "https://example.com/intermediate.cer"
    assert seen["verify"] is True
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == aia_timeout().connect


@pytest.mark.asyncio
async def test_cache_and_one_shot_aia_without_urllib(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    public_dns,
):
    chain = _Chain()
    chain.install_bundle(tmp_path / "ca-bundle.pem", monkeypatch)
    downloads: list[str] = []
    leaves = {"next": chain.leaf_no_aki}

    async def fake_leaf(_url: str) -> bytes:
        return _der(leaves["next"])

    async def fake_download(url: str) -> bytes:
        downloads.append(url)
        if leaves["next"] is chain.leaf_other_aki:
            return _pem(chain.other_intermediate)
        return _pem(chain.intermediate)

    def boom(*_args, **_kwargs):
        raise AssertionError("aia-chaser must not open its own socket or urllib")

    import aia_chaser.chaser as chaser_mod
    from aia_chaser import AiaChaser

    monkeypatch.setattr(chaser_mod, "urlopen", boom)
    monkeypatch.setattr(chaser_mod.socket, "create_connection", boom)
    monkeypatch.setattr(AiaChaser, "make_ssl_context_for_url", boom)
    monkeypatch.setattr(AiaChaser, "fetch_ca_chain_for_host", boom)
    monkeypatch.setattr(AiaChaser, "fetch_cert_chain_for_host", boom)
    monkeypatch.setattr(AiaChaser, "fetch_host_cert", boom)
    chase_calls = {"n": 0}
    original_chase = AiaChaser.aia_chase_cert

    def counting_chase(self, certificate):
        chase_calls["n"] += 1
        return original_chase(self, certificate)

    monkeypatch.setattr(AiaChaser, "aia_chase_cert", counting_chase)
    monkeypatch.setattr("app.tls_aia.fetch_peer_certificate_der", fake_leaf)
    monkeypatch.setattr("app.tls_aia.download_aia_bytes", fake_download)

    first = await recover_verify_context("https://example.com/article")
    second_leaf = chain.leaf_no_aki_b
    leaves["next"] = second_leaf
    second = await recover_verify_context("https://example.com/other")
    assert downloads == [AIA_URL]
    assert chase_calls["n"] == 1
    assert first is not None and second is not None
    assert _der(chain.intermediate) in first.get_ca_certs(True)
    assert _der(chain.leaf_no_aki) not in first.get_ca_certs(True)
    assert first.verify_mode == ssl.CERT_REQUIRED

    leaves["next"] = chain.leaf_other_aki
    third = await recover_verify_context("https://example.com/third")
    assert downloads == [AIA_URL, AIA_URL]
    assert chase_calls["n"] == 2
    assert third is not None
    assert _der(chain.other_intermediate) in third.get_ca_certs(True)

    created: list[ssl.SSLContext] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><title>Recovered</title></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    def fake_create(verify: bool | ssl.SSLContext = True, *, timeout: httpx.Timeout | None = None):
        assert isinstance(verify, ssl.SSLContext)
        assert verify.verify_mode == ssl.CERT_REQUIRED
        assert verify.check_hostname is True
        created.append(verify)
        return httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))

    monkeypatch.setattr("app.fetch.create_http_client", fake_create)
    leaves["next"] = chain.leaf_no_aki

    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise _chain_error(CHAIN_MESSAGE + " ORIGINAL")

    with caplog.at_level(logging.INFO, logger="arachne"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler)) as client:
            page = await fetch_url(client, "https://example.com/article")
    assert page.status_code == 200
    assert "Recovered" in page.body
    assert len(created) == 1
    assert "example.com" in caplog.text
    assert "aia_recovered=1" in caplog.text
    assert downloads == [AIA_URL, AIA_URL]


@pytest.mark.asyncio
async def test_untrusted_aia_certificate_is_not_installed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A self-signed AIA payload (proxy MITM CA) must not become a trust anchor."""
    chain = _Chain()
    chain.install_bundle(tmp_path / "ca-bundle.pem", monkeypatch)
    attacker_key = _key()
    attacker = _sign(_name("clash-mitm"), _name("clash-mitm"), attacker_key, attacker_key, is_ca=True)

    async def fake_leaf(_url: str) -> bytes:
        return _der(chain.leaf)

    async def fake_download(_url: str) -> bytes:
        return _pem(attacker)

    monkeypatch.setattr("app.tls_aia.fetch_peer_certificate_der", fake_leaf)
    monkeypatch.setattr("app.tls_aia.download_aia_bytes", fake_download)
    with caplog.at_level(logging.WARNING, logger="arachne"):
        assert await recover_verify_context("https://example.com/article") is None
    assert "aia recover failed" in caplog.text
    assert "example.com" in caplog.text


@pytest.mark.asyncio
async def test_aia_failure_preserves_original_ssl_error(public_dns, monkeypatch: pytest.MonkeyPatch):
    async def fake_leaf(_url: str) -> bytes:
        raise ValueError("no peer")

    monkeypatch.setattr("app.tls_aia.fetch_peer_certificate_der", fake_leaf)

    def handler(request: httpx.Request) -> httpx.Response:
        raise _chain_error(CHAIN_MESSAGE + " ORIGINAL")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/article")
    assert exc.value.code == "fetch_failed"
    assert exc.value.detail["kind"] == "ssl"
    assert "ORIGINAL" in exc.value.detail["reason"]
    assert is_retryable(exc.value) is False


@pytest.mark.asyncio
async def test_recovered_context_still_failing_ssl_does_not_chase_again(
    public_dns,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = {"n": 0}

    async def fake_recover(_url: str):
        calls["n"] += 1
        return ssl.create_default_context()

    monkeypatch.setattr("app.fetch.recover_verify_context", fake_recover)

    def retry_handler(request: httpx.Request) -> httpx.Response:
        raise _chain_error(CHAIN_MESSAGE + " RETRY")

    def fake_create(verify: bool | ssl.SSLContext = True, *, timeout: httpx.Timeout | None = None):
        return httpx.AsyncClient(transport=httpx.MockTransport(retry_handler))

    monkeypatch.setattr("app.fetch.create_http_client", fake_create)

    def handler(request: httpx.Request) -> httpx.Response:
        raise _chain_error(CHAIN_MESSAGE + " ORIGINAL")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/article")
    assert calls["n"] == 1
    assert exc.value.detail["kind"] == "ssl"
    assert "ORIGINAL" in exc.value.detail["reason"]
    assert "RETRY" not in exc.value.detail["reason"]


@pytest.mark.asyncio
async def test_expired_ssl_does_not_start_aia(public_dns, monkeypatch: pytest.MonkeyPatch):
    async def forbid(_url: str):
        raise AssertionError("expired certificates must not chase AIA")

    monkeypatch.setattr("app.fetch.recover_verify_context", forbid)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArachneError) as exc:
            await fetch_url(client, "https://example.com/")
    assert exc.value.detail["kind"] == "ssl"
    assert is_retryable(exc.value) is False


@pytest.mark.asyncio
async def test_fetch_with_retries_skips_ssl_kind(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    async def fake_fetch(*_args, **_kwargs):
        calls["n"] += 1
        raise fetch_failed("Failed to fetch URL", {"kind": "ssl", "reason": CHAIN_MESSAGE})

    monkeypatch.setattr("app.antibot.fetch_url", fake_fetch)
    with pytest.raises(ArachneError) as exc:
        await fetch_with_retries(None, "https://example.com/")  # type: ignore[arg-type]
    assert calls["n"] == 1
    assert exc.value.detail["kind"] == "ssl"
    assert is_retryable(exc.value) is False


def test_ssl_token_is_not_an_auto_render_code(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARACHNE_AUTO_RENDER_ON", "challenge,ssl,fetch_failed")
    assert auto_render_on_codes() == frozenset({"challenge_detected"})
    assert "fetch_failed" not in auto_render_on_codes()
