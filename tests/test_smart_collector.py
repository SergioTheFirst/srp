"""Deep SMART (ssd3 Ф1): Tier A ATA-via-CIM + Tier B NVMe-via-IOCTL.

Both tiers are independent and best-effort -- a single disk or a single tier
failing must never raise, only yield fewer overlay fields (K7). Privacy
invariant: the raw disk serial and the ATA SMART blob must never survive into
what ``collect_smart`` returns -- only the sha256 disk_key and decoded ints do.
"""

from __future__ import annotations

import base64
import json
import struct

import pytest
from client.collectors import smart
from client.collectors.ps import PsResult

pytestmark = pytest.mark.unit


def _synthetic_blob(attrs: dict[int, int]) -> bytes:
    """Fake FailurePredictData blob: 2-byte header + 30 x 12-byte slots."""
    buf = bytearray(2 + 30 * 12)
    for i, (attr_id, raw) in enumerate(attrs.items()):
        off = 2 + i * 12
        buf[off] = attr_id
        buf[off + 5 : off + 11] = raw.to_bytes(6, "little")
    return bytes(buf)


# --------------------------------------------------------------------------- #
# parse_ata_smart
# --------------------------------------------------------------------------- #


def test_parse_ata_smart_known_values():
    blob = _synthetic_blob({5: 3, 187: 1, 197: 2, 198: 0})
    assert smart.parse_ata_smart(blob) == {"5": 3, "187": 1, "197": 2, "198": 0}


def test_parse_ata_smart_applies_decode_mask():
    blob = _synthetic_blob({194: 0x1_0001, 9: (2**32) + 42})  # 16-bit / 32-bit masks
    attrs = smart.parse_ata_smart(blob)
    assert attrs["194"] == 1
    assert attrs["9"] == 42


def test_parse_ata_smart_ignores_non_whitelisted_id():
    assert smart.parse_ata_smart(_synthetic_blob({240: 99})) == {}


def test_parse_ata_smart_truncated_blob_returns_empty():
    assert smart.parse_ata_smart(b"\x00" * 100) == {}


def test_parse_ata_smart_zero_id_slot_skipped():
    assert smart.parse_ata_smart(_synthetic_blob({0: 111, 5: 7})) == {"5": 7}


# --------------------------------------------------------------------------- #
# read_nvme_health (Tier B, ctypes transport injected)
# --------------------------------------------------------------------------- #


def _fake_nvme_response(**fields: float) -> bytes:
    """Fake IOCTL response buffer: header + ProtocolDataOffset=0 + 512B log."""
    buf = bytearray(smart._QUERY_OFF + smart._SPSD_LEN + smart._DATA_LEN)
    struct.pack_into("<L", buf, smart._QUERY_OFF + 16, 0)  # ProtocolDataOffset == 0
    log = bytearray(smart._DATA_LEN)
    log[0] = int(fields.get("critical_warning", 0))
    log[1:3] = int(fields.get("temp_k", 300)).to_bytes(2, "little")
    log[3] = int(fields.get("spare_pct", 100))
    log[4] = int(fields.get("spare_threshold_pct", 10))
    log[5] = int(fields.get("percentage_used", 0))
    log[128:144] = int(fields.get("power_on_hours", 10)).to_bytes(16, "little")
    log[144:160] = int(fields.get("unsafe_shutdowns", 0)).to_bytes(16, "little")
    log[160:176] = int(fields.get("media_errors", 0)).to_bytes(16, "little")
    buf[smart._QUERY_OFF : smart._QUERY_OFF + smart._DATA_LEN] = log
    return bytes(buf)


def test_read_nvme_health_parses_injected_response():
    response = _fake_nvme_response(spare_pct=42, media_errors=3, temp_k=310)
    result = smart.read_nvme_health(0, ioctl_fn=lambda idx, buf: response)
    assert result == {
        "nvme_critical_warning": 0,
        "nvme_spare_pct": 42,
        "nvme_spare_threshold_pct": 10,
        "nvme_percentage_used": 0,
        "nvme_data_units_written": 0,
        "nvme_power_cycles": 0,
        "power_on_hours": 10,
        "nvme_unsafe_shutdowns": 0,
        "nvme_media_errors": 3,
        "nvme_error_log_entries": 0,
        "temperature_c": 310 - 273,
    }


def test_read_nvme_health_zero_temp_omits_field():
    result = smart.read_nvme_health(0, ioctl_fn=lambda idx, buf: _fake_nvme_response(temp_k=0))
    assert result is not None
    assert "temperature_c" not in result


def test_read_nvme_health_transport_failure_returns_none():
    assert smart.read_nvme_health(0, ioctl_fn=lambda idx, buf: None) is None


def test_read_nvme_health_transport_raises_returns_none():
    def _boom(idx, buf):
        raise OSError("no such device")

    assert smart.read_nvme_health(0, ioctl_fn=_boom) is None


def test_read_nvme_health_short_response_returns_none():
    assert smart.read_nvme_health(0, ioctl_fn=lambda idx, buf: b"\x00" * 4) is None


def test_read_nvme_health_hung_transport_times_out(monkeypatch):
    monkeypatch.setattr(smart, "_IOCTL_JOIN_TIMEOUT_SEC", 0.05)

    def _hang(idx, buf):
        import time

        time.sleep(0.3)
        return _fake_nvme_response()

    assert smart.read_nvme_health(0, ioctl_fn=_hang) is None


# --------------------------------------------------------------------------- #
# collect_smart: merge onto base rows, per-tier failure isolation, status
# --------------------------------------------------------------------------- #


def _ps_ok(disks=(), status=(), data=()) -> PsResult:
    return PsResult("ok", {"disks": list(disks), "status": list(status), "data": list(data)})


def test_collect_smart_merges_tier_a_by_serial_hash(monkeypatch):
    base = [{"disk": "Samsung 980", "serial_hash": smart.hash_serial("SN123")}]
    blob = base64.b64encode(_synthetic_blob({197: 2})).decode("ascii")
    monkeypatch.setattr(
        smart,
        "run_ps",
        lambda *a, **k: _ps_ok(
            disks=[{"pnp": "SCSI\\...\\1", "index": 0, "serial": "SN123"}],
            status=[{"inst": "SCSI\\...\\1_0", "predict": False}],
            data=[{"inst": "SCSI\\...\\1_0", "blob": blob}],
        ),
    )
    monkeypatch.setattr(smart, "read_nvme_health", lambda index, ioctl_fn=None: None)

    merged, status = smart.collect_smart(base)
    assert status == "ok"
    assert merged[0]["smart_predict_fail"] is False
    assert merged[0]["smart_attrs"] == {"197": 2}
    assert merged[0]["disk"] == "Samsung 980"  # base fields preserved


def test_collect_smart_falls_back_to_positional_index_without_serial(monkeypatch):
    base = [{"disk": "Only Disk", "serial_hash": None}]
    monkeypatch.setattr(
        smart,
        "run_ps",
        lambda *a, **k: _ps_ok(disks=[{"pnp": "SCSI\\1", "index": 0, "serial": ""}]),
    )
    monkeypatch.setattr(
        smart, "read_nvme_health", lambda index, ioctl_fn=None: {"nvme_media_errors": 0}
    )
    merged, status = smart.collect_smart(base)
    assert status == "ok"
    assert merged[0]["nvme_media_errors"] == 0


def test_collect_smart_script_failure_returns_base_unchanged(monkeypatch):
    base = [{"disk": "X", "serial_hash": "abc"}]
    monkeypatch.setattr(smart, "run_ps", lambda *a, **k: PsResult("timeout"))
    merged, status = smart.collect_smart(base)
    assert merged == base
    assert status == "timeout"


def test_collect_smart_no_tier_hits_is_partial_not_exception(monkeypatch):
    base = [{"disk": "X", "serial_hash": "abc"}]
    monkeypatch.setattr(
        smart, "run_ps", lambda *a, **k: _ps_ok(disks=[{"pnp": "p", "index": 0, "serial": "abc"}])
    )
    monkeypatch.setattr(smart, "read_nvme_health", lambda index, ioctl_fn=None: None)
    merged, status = smart.collect_smart(base)
    assert status == "partial"
    assert merged == base


def test_collect_smart_bad_base64_blob_does_not_raise(monkeypatch):
    base = [{"disk": "X", "serial_hash": smart.hash_serial("SN1")}]
    monkeypatch.setattr(
        smart,
        "run_ps",
        lambda *a, **k: _ps_ok(
            disks=[{"pnp": "p", "index": 0, "serial": "SN1"}],
            data=[{"inst": "p", "blob": "%%%not-base64%%%"}],
        ),
    )
    monkeypatch.setattr(smart, "read_nvme_health", lambda index, ioctl_fn=None: None)
    merged, status = smart.collect_smart(base)
    assert status == "partial"
    assert "smart_attrs" not in merged[0]


def test_collect_smart_privacy_no_raw_serial_or_blob_leaks(monkeypatch):
    raw_serial = "SUPER-SECRET-SERIAL-42"
    blob_bytes = _synthetic_blob({5: 1})
    blob_b64 = base64.b64encode(blob_bytes).decode("ascii")
    base = [{"disk": "X", "serial_hash": smart.hash_serial(raw_serial)}]
    monkeypatch.setattr(
        smart,
        "run_ps",
        lambda *a, **k: _ps_ok(
            disks=[{"pnp": "p", "index": 0, "serial": raw_serial}],
            status=[{"inst": "p", "predict": True}],
            data=[{"inst": "p", "blob": blob_b64}],
        ),
    )
    monkeypatch.setattr(
        smart, "read_nvme_health", lambda index, ioctl_fn=None: {"nvme_media_errors": 1}
    )
    merged, _ = smart.collect_smart(base)
    dumped = json.dumps(merged)
    assert raw_serial not in dumped
    assert blob_b64 not in dumped
    assert "serial" not in merged[0]  # only serial_hash, never the raw field


def test_collect_smart_russian_locale_serial_hashes_without_crashing(monkeypatch):
    # Rare OEM disks stamp non-ASCII serials; hash_serial encodes utf-8/replace.
    raw_serial = "Диск-СЕРИЙНЫЙ-01"
    base = [{"disk": "X", "serial_hash": smart.hash_serial(raw_serial)}]
    monkeypatch.setattr(
        smart,
        "run_ps",
        lambda *a, **k: _ps_ok(
            disks=[{"pnp": "p", "index": 0, "serial": raw_serial}],
            status=[{"inst": "p", "predict": False}],
        ),
    )
    monkeypatch.setattr(smart, "read_nvme_health", lambda index, ioctl_fn=None: None)
    merged, status = smart.collect_smart(base)
    assert status == "ok"
    assert merged[0]["smart_predict_fail"] is False
