"""Ф8 T6: active banner identification (TLS cert name + HTTP Server header).

The one *active* passive-tier collector: a bounded TCP touch of 443/80 on an
already-known RFC1918 host -- no more invasive than the reachability cycle, which
already connects to those ports for liveness. Two PURE parsers are the testable
core (a ``getpeercert()`` dict -> hostname; a header bag -> ``Server`` string);
the fetchers around them are RFC1918-gated, use a no-redirect opener (SSRF-safe),
and are injectable so the suite never opens a socket.

Fail-closed throughout: a self-signed cert, a hung host, a missing/garbage header
yields no hint, never a guess. Stdlib ``ssl`` only hands back a parsed peer-cert
dict for a CA-validated chain, so a LAN device's self-signed cert fails closed to
no name and the HTTP ``Server`` banner carries the identification. The
hostname/model produced here is the lowest-priority hint -- the writer only ever
fills an empty field (see :func:`server.db.fill_net_device_identity`).
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.request
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.error import HTTPError

from server.netdisco.passive import PassiveHint
from server.printers.discovery import is_rfc1918

_DEFAULT_CAP = 256  # hard ceiling on hosts touched per cycle (anti-blast)
_DEFAULT_TIMEOUT = 2.0
_MAX_NAME = 253  # DNS name length ceiling
_MAX_BANNER = 120  # Server header is a label, not a document

_CertFn = Callable[[str, float], Optional[dict]]
_HttpFn = Callable[[str, float], Optional[Mapping[str, str]]]


# --------------------------------------------------------------------------- #
# pure parsers                                                                  #
# --------------------------------------------------------------------------- #


def _clean_host(name: Any) -> Optional[str]:
    """A cert name trimmed to a safe hostname, or ``None`` (fail-closed).

    Wildcards, control bytes, whitespace and injection characters are rejected
    rather than trusted -- the value flows into a hostname field and a tooltip."""
    if not isinstance(name, str):
        return None
    host = name.strip().rstrip(".")
    if not host or len(host) > _MAX_NAME or "*" in host:
        return None
    if not all(c.isalnum() or c in "-._" for c in host):
        return None
    return host


def parse_cert_names(cert: Optional[dict]) -> Optional[str]:
    """Best hostname from a ``getpeercert()`` dict: a subjectAltName DNS entry
    first (modern certs put the real names there), else the subject commonName.
    Wildcards/garbage are rejected; fail-closed to ``None``."""
    if not isinstance(cert, dict):
        return None
    for entry in cert.get("subjectAltName", ()) or ():
        if isinstance(entry, (tuple, list)) and len(entry) == 2 and entry[0] == "DNS":
            name = _clean_host(entry[1])
            if name:
                return name
    for rdn in cert.get("subject", ()) or ():
        for pair in rdn or ():
            if isinstance(pair, (tuple, list)) and len(pair) == 2 and pair[0] == "commonName":
                name = _clean_host(pair[1])
                if name:
                    return name
    return None


def parse_http_server(headers: Optional[Mapping[str, str]]) -> Optional[str]:
    """The HTTP ``Server`` banner, trimmed and length-bounded, or ``None``. The
    header key is matched case-insensitively; any control byte fails closed."""
    if not headers:
        return None
    raw: Optional[str] = None
    for key, val in headers.items():
        if str(key).lower() == "server":
            raw = val
            break
    if not raw:
        return None
    cleaned = " ".join(str(raw).split())[:_MAX_BANNER]
    if not cleaned or any(ord(c) < 32 for c in cleaned):
        return None
    return cleaned


# --------------------------------------------------------------------------- #
# fetchers (real socket I/O; injected away in tests)                           #
# --------------------------------------------------------------------------- #


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: is_rfc1918 validates only the initial host, so a
    3xx Location could otherwise bounce the probe to a public host (SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _fetch_cert(ip: str, timeout: float) -> Optional[dict]:
    """The peer cert dict from a TLS handshake to ``ip``:443, or ``None``.

    Connected by IP with chain validation left ON (``check_hostname=False`` only
    because there is no SNI name) -- stdlib returns a parsed dict only for a
    CA-trusted chain, so a self-signed LAN cert fails closed to ``None``."""
    if not is_rfc1918(ip):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # connecting by IP; the chain is still required
    try:
        with (
            socket.create_connection((ip, 443), timeout=timeout) as raw,
            ctx.wrap_socket(raw) as tls,
        ):
            cert = tls.getpeercert()
        return cert or None
    except (OSError, ssl.SSLError, ValueError):
        return None


def _fetch_http_server(ip: str, timeout: float) -> Optional[Mapping[str, str]]:
    """The response headers from a HEAD of ``http://ip/``, or ``None``. A 4xx/5xx
    still carries a ``Server`` header, so its headers are returned too."""
    if not is_rfc1918(ip):
        return None
    req = urllib.request.Request(
        f"http://{ip}/", method="HEAD", headers={"User-Agent": "SRP-netdisco"}
    )
    try:
        # B310: no-redirect opener, hardcoded http:// to an RFC1918-checked host.
        with _OPENER.open(req, timeout=timeout) as resp:  # nosec B310
            return dict(resp.headers.items())
    except HTTPError as exc:  # an error response still has a Server banner
        return dict(exc.headers.items()) if exc.headers else None
    except (OSError, http.client.HTTPException):
        return None


# --------------------------------------------------------------------------- #
# collector                                                                     #
# --------------------------------------------------------------------------- #


def collect_banner(
    targets: Iterable[str],
    *,
    cap: int = _DEFAULT_CAP,
    timeout: float = _DEFAULT_TIMEOUT,
    cert_fn: _CertFn = _fetch_cert,
    http_fn: _HttpFn = _fetch_http_server,
) -> Dict[str, PassiveHint]:
    """Touch each RFC1918 ``target`` on 443/80 and return ``{ip: PassiveHint}``
    for the hosts that yield a cert name or a ``Server`` banner. Public hosts are
    skipped, the fan-out stops at ``cap``, and every fetch is fail-closed."""
    out: Dict[str, PassiveHint] = {}
    touched = 0
    for ip in targets:
        if not is_rfc1918(ip):
            continue
        if touched >= cap:
            break
        touched += 1
        name = parse_cert_names(cert_fn(ip, timeout))
        model = parse_http_server(http_fn(ip, timeout))
        if name or model:
            out[ip] = PassiveHint(ip=ip, source="banner", hostname=name, model=model)
    return out
