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


# --------------------------------------------------------------------------- #
# Get-Jobs (which-jobs=completed) -- user-level attribution straight from the
# printer, dominant when direct-IP printing bypasses the agent/print-server.
# --------------------------------------------------------------------------- #


def test_build_get_jobs_request_shape():
    body = ipp.build_get_jobs_request("ipp://192.168.9.8/ipp/print", request_id=3, limit=20)
    assert body[:2] == b"\x01\x01"  # version 1.1
    assert struct.unpack_from(">H", body, 2)[0] == 0x000A  # Get-Jobs
    assert struct.unpack_from(">I", body, 4)[0] == 3
    assert body[-1] == 0x03
    assert b"which-jobs" in body and b"completed" in body
    assert b"job-originating-user-name" in body


def _job_group(job_id: int, name: str, user: str, impressions: int) -> bytes:
    out = b"\x02"  # job-attributes-tag (group delimiter)
    fields = [
        (0x21, b"job-id", struct.pack(">i", job_id)),
        (0x42, b"job-name", name.encode()),
        (0x42, b"job-originating-user-name", user.encode()),
        (0x21, b"job-impressions-completed", struct.pack(">i", impressions)),
    ]
    for tag, fname, fval in fields:
        out += bytes([tag]) + struct.pack(">H", len(fname)) + fname
        out += struct.pack(">H", len(fval)) + fval
    return out


def _get_jobs_response(*jobs) -> bytes:
    out = b"\x01\x01" + b"\x00\x00" + struct.pack(">I", 1)
    for j in jobs:
        out += _job_group(*j)
    out += b"\x03"  # end-of-attributes
    return out


def test_parse_job_groups_splits_multiple_jobs():
    data = _get_jobs_response(
        (7, "doc-A", "ivanov", 3),
        (8, "doc-B", "petrov", 1),
    )
    jobs = ipp.parse_job_groups(data)
    assert [j["job-id"] for j in jobs] == [7, 8]
    assert jobs[0]["job-originating-user-name"] == "ivanov"
    assert jobs[0]["job-name"] == "doc-A"
    assert jobs[0]["job-impressions-completed"] == 3
    assert jobs[1]["job-originating-user-name"] == "petrov"


def test_parse_job_groups_ignores_non_job_groups():
    # operation-attributes-tag (0x01) attributes must not leak into a job dict.
    data = b"\x01\x01\x00\x00" + struct.pack(">I", 1)
    data += b"\x01"  # operation-attributes-tag
    data += bytes([0x47]) + struct.pack(">H", 6) + b"status" + struct.pack(">H", 2) + b"ok"
    data += _job_group(9, "doc-C", "sidorov", 5)
    data += b"\x03"
    jobs = ipp.parse_job_groups(data)
    assert len(jobs) == 1 and jobs[0]["job-id"] == 9
    assert "status" not in jobs[0]


def test_parse_job_groups_garbage_is_empty_list():
    assert ipp.parse_job_groups(b"\x00\x01\x02") == []


def test_parse_job_groups_caps_group_count_against_malformed_flood():
    """Security-review LOW: a response that is just repeated 0x02 bytes must
    not mint one transient dict per byte (up to _MAX_RESPONSE)."""
    data = b"\x01\x01\x00\x00" + struct.pack(">I", 1)
    data += b"\x02" * (ipp._MAX_JOB_GROUPS + 50)  # far more groups than any real limit
    data += b"\x03"
    jobs = ipp.parse_job_groups(data)
    assert len(jobs) == ipp._MAX_JOB_GROUPS


def test_get_completed_jobs_rejects_non_rfc1918_without_network():
    assert ipp.get_completed_jobs("8.8.8.8") == []


def test_get_completed_jobs_success_via_monkeypatched_open(monkeypatch):
    data = _get_jobs_response((11, "report.pdf", "ivanov", 4))
    monkeypatch.setattr(ipp, "_open", lambda req, timeout=0: _Resp(data))
    jobs = ipp.get_completed_jobs("192.168.9.8")
    assert jobs == [{"job_id": 11, "name": "report.pdf", "user_name": "ivanov", "impressions": 4}]


def test_get_completed_jobs_missing_fields_are_none_not_fabricated(monkeypatch):
    """A job whose printer response omits name/user/impressions must surface
    None for them (UNKNOWN over false confidence), never an empty-string or
    fabricated value -- these fields are genuinely absent on many printers."""
    data = b"\x01\x01\x00\x00" + struct.pack(">I", 1)
    data += b"\x02"  # job-attributes-tag
    data += bytes([0x21]) + struct.pack(">H", 6) + b"job-id" + struct.pack(">H", 4)
    data += struct.pack(">i", 12)
    data += b"\x03"
    monkeypatch.setattr(ipp, "_open", lambda req, timeout=0: _Resp(data))
    jobs = ipp.get_completed_jobs("192.168.9.8")
    assert jobs == [{"job_id": 12, "name": None, "user_name": None, "impressions": None}]


def test_get_completed_jobs_empty_on_network_failure(monkeypatch):
    def _boom(req, timeout=0):
        raise OSError("unreachable")

    monkeypatch.setattr(ipp, "_open", _boom)
    assert ipp.get_completed_jobs("192.168.9.8") == []
