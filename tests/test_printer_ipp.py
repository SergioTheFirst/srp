"""Phase 5 — IPP fallback: request build + response parse + RFC1918 guard."""

from __future__ import annotations

import struct

import pytest
from server.printers import ipp
from server.printers.models import PrinterReading

pytestmark = pytest.mark.unit


def _ipp_response(model: str = "HP LaserJet 400", state: int = 3) -> bytes:
    out = b"\x01\x01" + b"\x00\x00" + struct.pack(">I", 1)  # version, status, request-id
    out += b"\x04"  # printer-attributes-tag
    name = b"printer-make-and-model"
    val = model.encode()
    out += b"\x41" + struct.pack(">H", len(name)) + name + struct.pack(">H", len(val)) + val
    name2 = b"printer-state"
    out += b"\x23" + struct.pack(">H", len(name2)) + name2 + struct.pack(">H", 4)
    out += struct.pack(">i", state)
    out += b"\x03"  # end-of-attributes
    return out


class _Resp:
    def __init__(self, data: bytes) -> None:
        self._d = data

    def read(self, n: int = -1) -> bytes:
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_build_request_is_get_printer_attributes():
    body = ipp.build_request("ipp://192.168.1.5/ipp/print", request_id=7)
    assert body[:2] == b"\x01\x01"  # version 1.1
    assert struct.unpack_from(">H", body, 2)[0] == 0x000B  # Get-Printer-Attributes
    assert struct.unpack_from(">I", body, 4)[0] == 7
    assert body[-1] == 0x03  # end-of-attributes
    assert b"printer-uri" in body and b"ipp://192.168.1.5/ipp/print" in body


def test_parse_attributes_extracts_model_and_state():
    attrs = ipp.parse_attributes(_ipp_response("HP LaserJet 400", 3))
    assert attrs["printer-make-and-model"] == "HP LaserJet 400"
    assert attrs["printer-state"] == 3


def test_parse_attributes_garbage_is_empty():
    assert ipp.parse_attributes(b"\x00\x01\x02") == {}


def test_probe_rejects_non_rfc1918_without_network():
    assert ipp.probe("8.8.8.8") is None
    assert ipp.probe("not-an-ip") is None


def test_probe_success_via_monkeypatched_open(monkeypatch):
    monkeypatch.setattr(ipp, "_open", lambda req, timeout=0: _Resp(_ipp_response("Canon iR", 4)))
    r = ipp.probe("192.168.1.50")
    assert isinstance(r, PrinterReading)
    assert r.model == "Canon iR" and r.status == "printing" and r.source_protocol == "ipp"


def test_no_redirect_handler_refuses_to_follow():
    # SSRF guard: a 3xx must not be chased to an off-host (public/link-local) Location.
    handler = ipp._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/") is None
