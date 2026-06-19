"""HTTP fallback probe (Phase 5): last-resort printer identification.

Fetches the device web root over HTTP and best-effort extracts a make/model from
the page title. Read-only; RFC1918 only; hard timeout; bounded body. It does NOT
scrape page counters -- vendor status HTML is too divergent to read a counter
safely, and a fabricated number would violate UNKNOWN-over-false-confidence. A
printer-hint gate keeps it from mislabelling an arbitrary web host as a printer.
"""

from __future__ import annotations

import http.client
import re
import urllib.request
from typing import Optional

from server.printers.discovery import is_rfc1918
from server.printers.models import PrinterReading

_MAX_BODY = 64 * 1024
# Bounded spans ({0,200} attrs, {0,300} title) avoid super-linear backtracking on
# hostile bodies; extract_model truncates to 120 chars anyway.
_TITLE = re.compile(r"<title\b[^>]{0,200}>(.{0,300}?)</title>", re.IGNORECASE | re.DOTALL)
# Page must look printer-ish before we claim it (avoids labelling a random host).
_PRINTER_HINT = re.compile(
    r"printer|laserjet|officejet|imageclass|workcentre|workcenter|ecosys|taskalfa|"
    r"versalink|altalink|bizhub|aficio|\bmfp\b|\bmfc\b|pixma|stylus|lexmark|kyocera|"
    r"brother|ricoh|xerox|epson|canon|konica",
    re.IGNORECASE,
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: is_rfc1918 validates only the initial host, so a
    3xx Location could otherwise bounce the probe to an arbitrary public/link-local
    host (SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(req: urllib.request.Request, timeout: float):
    # B310: no-redirect opener, hardcoded http:// to an RFC1918-checked host.
    return _OPENER.open(req, timeout=timeout)  # nosec B310


def extract_model(html: str) -> Optional[str]:
    m = _TITLE.search(html)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title[:120] or None


def looks_like_printer(html: str) -> bool:
    return _PRINTER_HINT.search(html) is not None


def probe(ip: str, *, timeout: float = 2.0) -> Optional[PrinterReading]:
    """Fetch the web root -> PrinterReading (model only), or None."""
    if not is_rfc1918(ip):
        return None
    for url in (f"http://{ip}/", f"http://{ip}:80/"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SRP-printer-probe"})
            with _open(req, timeout) as resp:
                body = resp.read(_MAX_BODY)
        except (OSError, http.client.HTTPException):
            continue  # unreachable / non-HTTP -> try the next URL, else None
        html = body.decode("utf-8", "replace")
        if not looks_like_printer(html):
            continue  # not obviously a printer -> do not claim it
        model = extract_model(html)
        if model is None:
            continue
        return PrinterReading(ip=ip, model=model, source_protocol="http")
    return None
