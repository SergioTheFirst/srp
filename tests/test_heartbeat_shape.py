"""ssd3 Ф4 (T4.1/T4.4): heartbeat tail-latency micro-series -- golden-parser pins.

Six new Optional heartbeat fields (disk_read_ms_p50/p95, disk_write_ms_p50/p95,
disk_lat_max_ms, disk_lat_samples) ride alongside the existing disk_read_sec/
disk_write_sec (untouched, K2/compat) -- an old agent that never sends them, or a
PS run that filtered every ΔBase=0 sample below the 4-point floor, must degrade
to None on every one of the six fields, never raise.
"""

from __future__ import annotations

from client.collectors import heartbeat
from client.collectors.ps import PsResult


def _ok(data):
    return PsResult("ok", data)


_HB_BASE = {
    "cpu_pct": 15,
    "cpu_perf_pct": 67,
    "mem_avail_mb": 8192,
    "committed_pct": 45,
    "pagefile_pct": 12,
    "disk_read_sec": 0,
    "disk_write_sec": 0,
    "disk_queue": 0,
    "free_space_pct": 42.3,
    "handle_count_total": 123_456,
    "nic_errors": 2,
    "user_present": True,
    "uptime_hours": 5.2,
}

_HB_WITH_TAIL = {
    **_HB_BASE,
    "disk_read_ms_p50": 1.234,
    "disk_read_ms_p95": 8.5,
    "disk_write_ms_p50": 2.1,
    "disk_write_ms_p95": 9.9,
    "disk_lat_max_ms": 12.3,
    "disk_lat_samples": 7,
}


def test_tail_latency_fields_parsed(monkeypatch):
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: _ok(_HB_WITH_TAIL))
    res = heartbeat.collect_heartbeat()
    p = res.payload
    assert p["disk_read_ms_p50"] == 1.234
    assert p["disk_read_ms_p95"] == 8.5
    assert p["disk_write_ms_p50"] == 2.1
    assert p["disk_write_ms_p95"] == 9.9
    assert p["disk_lat_max_ms"] == 12.3
    assert p["disk_lat_samples"] == 7
    assert isinstance(p["disk_lat_samples"], int)


def test_old_agent_without_tail_fields_is_none_not_crash(monkeypatch):
    """Pre-Ф4 PS output (no tail keys at all) must not raise -- six Nones."""
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: _ok(_HB_BASE))
    res = heartbeat.collect_heartbeat()
    p = res.payload
    for key in (
        "disk_read_ms_p50",
        "disk_read_ms_p95",
        "disk_write_ms_p50",
        "disk_write_ms_p95",
        "disk_lat_max_ms",
        "disk_lat_samples",
    ):
        assert p[key] is None
    # Untouched legacy fields still parse fine (K2/compat).
    assert p["disk_read_sec"] == 0.0


def test_below_4_valid_samples_nulls_percentiles(monkeypatch):
    """PS-side ΔBase=0 filtering left <4 valid deltas: percentiles null, raw
    sample count still reported (T4.2's extractor gates on it directly)."""
    data = {
        **_HB_BASE,
        "disk_read_ms_p50": None,
        "disk_read_ms_p95": None,
        "disk_write_ms_p50": None,
        "disk_write_ms_p95": None,
        "disk_lat_max_ms": None,
        "disk_lat_samples": 2,
    }
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: _ok(data))
    res = heartbeat.collect_heartbeat()
    p = res.payload
    assert p["disk_read_ms_p50"] is None
    assert p["disk_read_ms_p95"] is None
    assert p["disk_lat_samples"] == 2
