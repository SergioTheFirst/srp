"""Phase 5 — HTTP fallback: model extraction + printer-hint gate + RFC1918 guard."""

from __future__ import annotations

import pytest
from server.printers import http_probe
from server.printers.models import PrinterReading

pytestmark = pytest.mark.unit


class _Resp:
    def __init__(self, data: bytes) -> None:
        self._d = data

    def read(self, n: int = -1) -> bytes:
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_extract_model_from_title():
    assert (
        http_probe.extract_model("<html><title> HP LaserJet 400 </title></html>")
        == "HP LaserJet 400"
    )


def test_extract_model_none_without_title():
    assert http_probe.extract_model("<html>no title here</html>") is None


def test_looks_like_printer_gate():
    assert http_probe.looks_like_printer("<title>Kyocera ECOSYS M2040</title>")
    assert not http_probe.looks_like_printer("<title>My NAS admin console</title>")


def test_probe_rejects_non_rfc1918():
    assert http_probe.probe("8.8.8.8") is None


def test_probe_success_via_monkeypatched_open(monkeypatch):
    html = b"<html><title>Brother MFC-L2700DW</title></html>"
    monkeypatch.setattr(http_probe, "_open", lambda req, timeout=0: _Resp(html))
    r = http_probe.probe("192.168.1.50")
    assert isinstance(r, PrinterReading)
    assert r.model == "Brother MFC-L2700DW" and r.source_protocol == "http"


def test_probe_ignores_non_printer_page(monkeypatch):
    monkeypatch.setattr(
        http_probe, "_open", lambda req, timeout=0: _Resp(b"<title>Router admin</title>")
    )
    assert http_probe.probe("192.168.1.50") is None


def test_no_redirect_handler_refuses_to_follow():
    # SSRF guard: a 3xx must not be chased off the RFC1918-checked host.
    handler = http_probe._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/") is None
