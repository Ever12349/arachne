"""Incomplete TLS chain recovery for the httpx fetch path.

A server that omits intermediates fails verification with
``CERTIFICATE_VERIFY_FAILED`` / ``unable to get local issuer``. This module
fetches that leaf with the same proxy, pin-IP, and SSRF hooks as a page
request, downloads AIA URIs over httpx, and builds a temporary verify context:
certifi's default trust, plus optional ``./data/ca-bundle.pem``, plus the
chased intermediates.

``CERT_NONE`` is used only to read the leaf certificate. Page bodies are never
fetched with verification off. An AIA certificate is kept only when
aia-chaser can walk it to a root already in that trust store and the
signatures check out. A proxy MITM CA (Clash or otherwise) is not trusted
unless the operator put it in ``ca-bundle.pem`` on purpose.

The shared lifespan client is not mutated. Its verify setting stays the
default (certifi), plus the optional bundle when that file is valid.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import ssl
import threading
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from app.config import CONNECT_TIMEOUT
from app.ssrf import assert_public_http_url

logger = logging.getLogger("arachne")

# Fixed path. No ARACHNE_CA_BUNDLE env and no kill switch.
CA_BUNDLE_PATH = os.path.join("data", "ca-bundle.pem")

AIA_MAX_BYTES = 1024 * 1024
_AIA_DOWNLOAD_BUDGET = 5

_CHAIN_MARKERS = (
    "unable to get local issuer",
    "unable to get issuer certificate",
    "unable to verify the first certificate",
    "self-signed certificate in certificate chain",
    "self signed certificate in certificate chain",
)

_SSL_MARKERS = (
    "certificate_verify_failed",
    "certificate verify failed",
    "sslcertverificationerror",
    "[ssl:",
    "ssl:",
    "tls alert",
    "tlsv1_alert",
    "handshake failure",
)

_Fetcher = Callable[[Sequence[str]], list[x509.Certificate]]
_FETCHER: contextvars.ContextVar[_Fetcher | None] = contextvars.ContextVar(
    "arachne_aia_fetcher",
    default=None,
)

# issuer/AKI (or issuer alone) → intermediate DER encodings.
_issuer_cache: dict[str, tuple[bytes, ...]] = {}
_issuer_lock = asyncio.Lock()

_bundle_lock = threading.Lock()
_bundle_state: tuple[str, int, int, tuple[x509.Certificate, ...]] | None = None
_patch_installed = False


def clear_caches() -> None:
    """Drop process-local AIA and CA-bundle caches. Tests use this."""
    global _bundle_state
    _issuer_cache.clear()
    with _bundle_lock:
        _bundle_state = None


def error_text(exc: BaseException) -> str:
    """Lowercased text of an exception and a short cause chain."""
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        parts.append(str(current))
        for arg in getattr(current, "args", ()):
            if isinstance(arg, str):
                parts.append(arg)
        current = current.__cause__ or current.__context__
    return "\n".join(parts).lower()


def is_incomplete_chain_error(exc: BaseException) -> bool:
    """True for missing-intermediate verify failures, not expiry or hostname mismatch.

    ``CERTIFICATE_VERIFY_FAILED`` is the OpenSSL bucket for many verify errors.
    AIA runs only when the text also names a chain problem (unable to get a
    local issuer, and the other markers above).
    """
    text = error_text(exc)
    return any(marker in text for marker in _CHAIN_MARKERS)


def is_ssl_connect_error(exc: BaseException) -> bool:
    """True when a connect failure is TLS rather than a refused/reset TCP connect."""
    if is_incomplete_chain_error(exc):
        return True
    text = error_text(exc)
    return any(marker in text for marker in _SSL_MARKERS)


def issuer_cache_key(certificate: x509.Certificate) -> str:
    """Cache key: issuer DN plus AKI. Issuer DN alone when AKI is missing."""
    issuer = certificate.issuer.rfc4514_string()
    try:
        aki = certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    except x509.ExtensionNotFound:
        return issuer
    key_id = aki.key_identifier
    if not key_id:
        return issuer
    return f"{issuer}|{key_id.hex()}"


def load_ca_bundle(path: str | None = None) -> list[x509.Certificate]:
    """Load ``./data/ca-bundle.pem``.

    Missing or empty is success with no extra certificates (current verify
    behavior). An unreadable or invalid PEM file is logged and ignored so
    fetch keeps using certifi.
    """
    bundle_path = CA_BUNDLE_PATH if path is None else path
    try:
        if not os.path.isfile(bundle_path):
            return []
        with open(bundle_path, "rb") as handle:
            data = handle.read()
    except OSError:
        logger.warning("ignoring unreadable ca bundle path=%s", bundle_path)
        return []
    if not data.strip():
        return []
    try:
        certs = parse_pem_certificates(data)
    except (ValueError, TypeError):
        logger.warning("ignoring invalid ca bundle path=%s", bundle_path)
        return []
    if not certs:
        logger.warning("ignoring invalid ca bundle path=%s", bundle_path)
        return []
    return certs


def parse_pem_certificates(data: bytes) -> list[x509.Certificate]:
    """Parse a multi-PEM bundle. Non-certificate bytes are an error."""
    begin = b"-----BEGIN CERTIFICATE-----"
    end = b"-----END CERTIFICATE-----"
    if begin not in data:
        raise ValueError("no pem certificate")
    certs: list[x509.Certificate] = []
    rest = data
    while True:
        start = rest.find(begin)
        if start < 0:
            break
        if rest[:start].strip():
            raise ValueError("non-pem data")
        stop = rest.find(end, start)
        if stop < 0:
            raise ValueError("truncated pem")
        stop += len(end)
        certs.append(x509.load_pem_x509_certificate(rest[start:stop]))
        rest = rest[stop:]
    if rest.strip():
        raise ValueError("trailing data")
    if not certs:
        raise ValueError("no pem certificate")
    return certs


def cached_ca_bundle() -> list[x509.Certificate]:
    """Process-local memo of ``load_ca_bundle`` keyed by path, mtime, and size."""
    global _bundle_state
    path = CA_BUNDLE_PATH
    try:
        stat = os.stat(path)
        stamp = (path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (path, -1, -1)
    with _bundle_lock:
        if _bundle_state is not None and _bundle_state[:3] == stamp:
            return list(_bundle_state[3])
    certs = load_ca_bundle(path)
    with _bundle_lock:
        _bundle_state = (stamp[0], stamp[1], stamp[2], tuple(certs))
    return list(certs)


def build_verify_context(extra_certs: Sequence[x509.Certificate] = ()) -> ssl.SSLContext:
    """certifi default trust plus ``extra_certs`` (bundle and/or intermediates)."""
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    if extra_certs:
        pem = "".join(cert.public_bytes(Encoding.PEM).decode("ascii") for cert in extra_certs)
        ctx.load_verify_locations(cadata=pem)
    return ctx


def resolve_verify(verify: bool | ssl.SSLContext) -> bool | ssl.SSLContext:
    """Default ``True`` stays certifi unless a valid CA bundle is present.

    An explicit ``SSLContext`` (the one-shot AIA client) is returned unchanged.
    ``False`` is not rewritten; callers must not use it for page bodies.
    """
    if verify is not True:
        return verify
    extras = cached_ca_bundle()
    if not extras:
        return True
    return build_verify_context(extras)


def aia_timeout() -> httpx.Timeout:
    """Connect and read aligned with ``CONNECT_TIMEOUT`` (5s)."""
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=CONNECT_TIMEOUT,
        write=CONNECT_TIMEOUT,
        pool=CONNECT_TIMEOUT,
    )


def peer_capture_ssl_context() -> ssl.SSLContext:
    """Handshake context that only exists to read the peer leaf. Never a page client."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def peer_certificate_der(response: httpx.Response) -> bytes | None:
    """DER leaf from an httpx response, if the transport exposed the SSL object."""
    stream = response.extensions.get("network_stream")
    getter = getattr(stream, "get_extra_info", None)
    if not callable(getter):
        return None
    ssl_object = getter("ssl_object")
    if ssl_object is None:
        return None
    der = ssl_object.getpeercert(True)
    if isinstance(der, bytes) and der:
        return der
    return None


def intermediates_from_chain(chain: Sequence[x509.Certificate]) -> list[x509.Certificate]:
    """Drop the leaf and any self-signed root. Keep the intermediates AIA found."""
    found: list[x509.Certificate] = []
    seen: set[bytes] = set()
    for cert in list(chain)[1:]:
        if cert.subject == cert.issuer:
            continue
        der = cert.public_bytes(Encoding.DER)
        if der in seen:
            continue
        seen.add(der)
        found.append(cert)
    return found


def _install_downloader_patch() -> None:
    """Route aia-chaser's AIA HTTP through our fetcher when one is set.

    ``aia_chase_cert`` keeps the full chain (unlike ``fetch_ca_chain_for_host``,
    which slices ``[:1]``). Its built-in downloader uses ``urllib.request.urlopen``
    and would ignore ``ARACHNE_HTTP_PROXY``. The patch delegates to the context
    var set for the duration of one chase; otherwise the library function runs.
    """
    global _patch_installed
    if _patch_installed:
        return
    import aia_chaser.chaser as chaser_mod

    original = chaser_mod._try_download_certificate

    def patched(urls: Sequence[str]) -> list[x509.Certificate]:
        fetcher = _FETCHER.get()
        if fetcher is None:
            return original(urls)
        return fetcher(urls)

    chaser_mod._try_download_certificate = patched
    _patch_installed = True


def _new_chaser():
    """Chaser whose trust anchors are certifi plus the optional bundle."""
    from aia_chaser import AiaChaser

    extras = cached_ca_bundle()
    ctx = build_verify_context(extras)
    return AiaChaser(context=ctx, trusted_cas=extras)


def _parse_certificate_bytes(data: bytes) -> list[x509.Certificate]:
    """DER, PEM, or PKCS#7, via aia-chaser's parser (pinned with the package)."""
    from aia_chaser.chaser import _try_parse_certificate

    return _try_parse_certificate(data)


async def fetch_peer_certificate_der(url: str) -> bytes:
    """GET ``url`` with ``CERT_NONE`` and return the peer leaf.

    Caller cookies and Authorization are not sent. The response body is not
    read and is not a page fetch.
    """
    from app.fetch import create_http_client

    timeout = aia_timeout()
    async with create_http_client(verify=peer_capture_ssl_context(), timeout=timeout) as client:
        client.follow_redirects = False
        async with client.stream("GET", url, timeout=timeout) as response:
            der = peer_certificate_der(response)
    if not der:
        raise ValueError("peer certificate missing")
    return der


async def download_aia_bytes(url: str) -> bytes:
    """Download one AIA URI with the page proxy, pin-IP, and SSRF checks.

    Verification stays at the default (certifi plus a valid bundle). This is
    not a ``CERT_NONE`` fetch.
    """
    from app.fetch import create_http_client

    await assert_public_http_url(url)
    timeout = aia_timeout()
    async with create_http_client(timeout=timeout) as client:
        async with client.stream("GET", url, timeout=timeout) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > AIA_MAX_BYTES:
                    raise ValueError("AIA response exceeds size cap")
                chunks.append(chunk)
    return b"".join(chunks)


async def _cached_intermediates(leaf: x509.Certificate) -> list[x509.Certificate] | None:
    key = issuer_cache_key(leaf)
    async with _issuer_lock:
        cached = _issuer_cache.get(key)
    if cached is None:
        return None
    return await asyncio.to_thread(_ders_to_certs, cached)


async def _store_intermediates(leaf: x509.Certificate, certs: Sequence[x509.Certificate]) -> None:
    if not certs:
        return
    ders = tuple(cert.public_bytes(Encoding.DER) for cert in certs)
    key = issuer_cache_key(leaf)
    async with _issuer_lock:
        _issuer_cache[key] = ders


def _ders_to_certs(ders: tuple[bytes, ...]) -> list[x509.Certificate]:
    return [x509.load_der_x509_certificate(der) for der in ders]


async def _chase_intermediates(leaf: x509.Certificate) -> list[x509.Certificate]:
    """Run ``aia_chase_cert`` on a worker thread; AIA HTTP stays on the event loop."""
    from aia_chaser.verify import verify_certificate_chain

    _install_downloader_patch()
    loop = asyncio.get_running_loop()
    budget = {"left": _AIA_DOWNLOAD_BUDGET}

    def fetcher(urls: Sequence[str]) -> list[x509.Certificate]:
        if budget["left"] <= 0:
            raise RuntimeError("AIA chase depth exceeded")
        budget["left"] -= 1
        http_urls = [url for url in urls if url.lower().startswith(("http://", "https://"))]
        if not http_urls:
            raise ValueError("no http(s) AIA URI")
        last_error: BaseException | None = None
        for http_url in http_urls:
            try:
                payload = asyncio.run_coroutine_threadsafe(download_aia_bytes(http_url), loop).result()
                return _parse_certificate_bytes(payload)
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def sync_chase() -> list[x509.Certificate]:
        token = _FETCHER.set(fetcher)
        try:
            chaser = _new_chaser()
            chain = list(chaser.aia_chase_cert(leaf))
            # Reject AIA material that does not sign back to a trusted root.
            verify_certificate_chain(chain, trusted=chaser._trusted)
        finally:
            _FETCHER.reset(token)
        return intermediates_from_chain(chain)

    return await asyncio.to_thread(sync_chase)


async def recover_verify_context(url: str) -> ssl.SSLContext | None:
    """Build a one-shot verify context for ``url``, or None if AIA cannot.

    Does not fetch the page body. The caller opens a separate client with the
    returned context and does not call this again for the same error.
    """
    host = urlsplit(url).hostname or ""
    try:
        der = await fetch_peer_certificate_der(url)
        leaf = await asyncio.to_thread(x509.load_der_x509_certificate, der)
        intermediates = await _cached_intermediates(leaf)
        if intermediates is None:
            intermediates = await _chase_intermediates(leaf)
            if not intermediates:
                logger.warning("aia recover failed host=%s error=no_intermediate", host)
                return None
            await _store_intermediates(leaf, intermediates)
        extras = await asyncio.to_thread(cached_ca_bundle)
        return await asyncio.to_thread(build_verify_context, [*extras, *intermediates])
    except Exception as exc:
        logger.warning("aia recover failed host=%s error=%s", host, type(exc).__name__)
        return None
