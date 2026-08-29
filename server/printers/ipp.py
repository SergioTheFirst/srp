"""IPP fallback probe (Phase 5): identify a printer that has SNMP disabled.

Sends one IPP Get-Printer-Attributes over HTTP via ``urllib`` (RFC 8011 binary
encoding) and parses make/model + state. Read-only. Privacy/safety: only RFC1918
hosts, hard timeout, bounded response size. A lifetime page count is not a
standard IPP attribute, so ``total_pages`` usually stays None here (UNKNOWN, not
fabricated) -- IPP's job is identification + liveness when SNMP is silent.
"""

from __future__ import annotations

import http.client
import struct
import urllib.request
from typing import Dict, Optional

from server.printers.discovery import is_rfc1918
from server.printers.models import PrinterReading

_GET_PRINTER_ATTRIBUTES = 0x000B
_GET_JOBS = 0x000A
_OP_ATTRS_TAG = 0x01
_JOB_ATTRS_TAG = 0x02
_PRINTER_ATTRS_TAG = 0x04
_END_TAG = 0x03
_MAX_RESPONSE = 256 * 1024  # bound a hostile/huge response

_INT_TAGS = {0x21, 0x23}  # integer, enum
# text/name/keyword/uri/charset/naturalLanguage/mimeMediaType
_TEXT_TAGS = {0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49}
_GROUP_TAGS = {0x00, 0x01, 0x02, 0x04, 0x05}  # delimiter / attribute-group tags

_STATE = {3: "idle", 4: "printing", 5: "stopped"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: is_rfc1918 validates only the initial host, so a
    3xx Location could otherwise bounce the probe to an arbitrary public/link-local
    host (SSRF). A printer's own IPP root never needs a redirect to answer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(req: urllib.request.Request, timeout: float):
    # B310: no-redirect opener, hardcoded http:// to an RFC1918-checked printer.
    return _OPENER.open(req, timeout=timeout)  # nosec B310


def _attr(tag: int, name: bytes, value: bytes) -> bytes:
    return (
        bytes([tag]) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value
    )


def build_request(printer_uri: str, request_id: int = 1) -> bytes:
    """Build an IPP/1.1 Get-Printer-Attributes request body."""
    body = (
        bytes([0x01, 0x01])  # version 1.1
        + struct.pack(">H", _GET_PRINTER_ATTRIBUTES)
        + struct.pack(">I", request_id)
        + bytes([_OP_ATTRS_TAG])
        + _attr(0x47, b"attributes-charset", b"utf-8")
        + _attr(0x48, b"attributes-natural-language", b"en")
        + _attr(0x45, b"printer-uri", printer_uri.encode())
        + _attr(0x44, b"requested-attributes", b"printer-make-and-model")
        + _attr(0x44, b"", b"printer-state")
        + _attr(0x44, b"", b"printer-state-message")
        + bytes([_END_TAG])
    )
    return body


def parse_attributes(data: bytes) -> Dict[str, object]:
    """Parse attribute name->value pairs from an IPP response. Garbage -> {}."""
    out: Dict[str, object] = {}
    pos = 8  # skip version(2) + status-code(2) + request-id(4)
    last_name = ""
    n = len(data)
    try:
        while pos < n:
            tag = data[pos]
            pos += 1
            if tag == _END_TAG:
                break
            if tag in _GROUP_TAGS:
                continue  # delimiter / group header carries no name/value
            (name_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            name = data[pos : pos + name_len].decode("ascii", "replace")
            pos += name_len
            (val_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            raw = data[pos : pos + val_len]
            pos += val_len
            key = name or last_name  # name_len 0 => additional value of last attr
            if name:
                last_name = name
            if tag in _INT_TAGS and len(raw) == 4:
                out[key] = int.from_bytes(raw, "big", signed=True)
            elif tag in _TEXT_TAGS:
                out[key] = raw.decode("utf-8", "replace")
    except (struct.error, IndexError):
        return out
    return out


def build_get_jobs_request(printer_uri: str, request_id: int = 1, limit: int = 50) -> bytes:
    """Build an IPP/1.1 Get-Jobs (which-jobs=completed) request body.

    A supplementary user-attribution source (RFC 8011): the printer's own
    completed-job buffer is short and not every printer answers, but where it
    does, job-originating-user-name is the one signal Windows Event 307 cannot
    give for direct-IP printing that bypasses the agent entirely.
    """
    body = (
        bytes([0x01, 0x01])  # version 1.1
        + struct.pack(">H", _GET_JOBS)
        + struct.pack(">I", request_id)
        + bytes([_OP_ATTRS_TAG])
        + _attr(0x47, b"attributes-charset", b"utf-8")
        + _attr(0x48, b"attributes-natural-language", b"en")
        + _attr(0x45, b"printer-uri", printer_uri.encode())
        + _attr(0x44, b"which-jobs", b"completed")
        + _attr(0x21, b"limit", struct.pack(">i", limit))
        + _attr(0x44, b"requested-attributes", b"job-id")
        + _attr(0x44, b"", b"job-name")
        + _attr(0x44, b"", b"job-originating-user-name")
        + _attr(0x44, b"", b"job-impressions-completed")
        + bytes([_END_TAG])
    )
    return body


_MAX_JOB_GROUPS = 200  # the request itself asks for limit=50; this is a hard backstop


def parse_job_groups(data: bytes) -> list[Dict[str, object]]:
    """Get-Jobs response -> one dict per job-attributes group (tag 0x02).

    Unlike ``parse_attributes`` (which flattens everything into one dict and
    would silently keep only the LAST job), this splits on the group
    delimiter so every job in the response survives. Attributes outside a
    job-attributes group (operation/printer/unsupported groups) are skipped.

    A response that is just repeated 0x02 bytes could otherwise mint one dict
    per byte (up to _MAX_RESPONSE); _MAX_JOB_GROUPS bounds that regardless of
    what a malicious/malformed printer sends.
    """
    jobs: list[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    pos, last_name, n = 8, "", len(data)
    try:
        while pos < n:
            tag = data[pos]
            pos += 1
            if tag == _END_TAG:
                break
            if tag in _GROUP_TAGS:
                if tag == _JOB_ATTRS_TAG:
                    if len(jobs) >= _MAX_JOB_GROUPS:
                        break
                    cur = {}
                    jobs.append(cur)
                else:
                    cur = None  # a non-job group: stop collecting until the next one
                continue
            (name_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            name = data[pos : pos + name_len].decode("ascii", "replace")
            pos += name_len
            (val_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            raw = data[pos : pos + val_len]
            pos += val_len
            key = name or last_name
            if name:
                last_name = name
            if cur is None:
                continue
            if tag in _INT_TAGS and len(raw) == 4:
                cur[key] = int.from_bytes(raw, "big", signed=True)
            elif tag in _TEXT_TAGS:
                cur[key] = raw.decode("utf-8", "replace")
    except (struct.error, IndexError):
        pass
    return jobs


def get_completed_jobs(ip: str, *, timeout: float = 2.0) -> list[Dict[str, object]]:
    """Completed print jobs straight from the printer: [{job_id, name,
    user_name, impressions}]. Supplementary source (a short buffer, fields
    often absent) -- absent fields are None, never fabricated (project
    invariant: UNKNOWN over false confidence).

    timeout matches probe()'s default: this now runs against every printer
    that just answered SNMP (not only SNMP-silent ones), so its worst case
    (2 paths x timeout) adds directly to every live printer's poll latency
    when printers.ipp_jobs is on -- keep it no higher than probe()'s.
    """
    if not is_rfc1918(ip):
        return []
    for path in ("/ipp/print", "/"):
        body = build_get_jobs_request(f"ipp://{ip}{path}")
        req = urllib.request.Request(
            f"http://{ip}:631{path}",
            data=body,
            headers={"Content-Type": "application/ipp"},
            method="POST",
        )
        try:
            with _open(req, timeout) as resp:
                data = resp.read(_MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException):
            continue
        if not data or len(data) > _MAX_RESPONSE:
            continue
        out: list[Dict[str, object]] = []
        for j in parse_job_groups(data):
            jid = j.get("job-id")
            if not isinstance(jid, int):
                continue
            name = j.get("job-name")
            user = j.get("job-originating-user-name")
            impressions = j.get("job-impressions-completed")
            out.append(
                {
                    "job_id": jid,
                    "name": name if isinstance(name, str) else None,
                    "user_name": user if isinstance(user, str) else None,
                    "impressions": impressions if isinstance(impressions, int) else None,
                }
            )
        if out:
            return out
    return []


def probe(ip: str, *, timeout: float = 2.0, request_id: int = 1) -> Optional[PrinterReading]:
    """IPP Get-Printer-Attributes -> PrinterReading, or None on any failure."""
    if not is_rfc1918(ip):
        return None
    for path in ("/ipp/print", "/"):
        body = build_request(f"ipp://{ip}{path}", request_id)
        req = urllib.request.Request(
            f"http://{ip}:631{path}",
            data=body,
            headers={"Content-Type": "application/ipp"},
            method="POST",
        )
        try:
            with _open(req, timeout) as resp:
                data = resp.read(_MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException):
            continue  # network/protocol failure -> try the next path, else None
        if not data or len(data) > _MAX_RESPONSE:
            continue
        attrs = parse_attributes(data)
        model = attrs.get("printer-make-and-model")
        state = attrs.get("printer-state")
        if not isinstance(model, str) and not isinstance(state, int):
            continue  # nothing useful at this path
        return PrinterReading(
            ip=ip,
            model=model if isinstance(model, str) and model else None,
            status=_STATE.get(state) if isinstance(state, int) else None,
            source_protocol="ipp",
        )
    return None
