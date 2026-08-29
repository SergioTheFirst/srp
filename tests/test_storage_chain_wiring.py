"""ssd3 Ф3 (T3.4): end-to-end wiring of a REAL errchain.ErrChain (not the
_FakeChain duck-type test_storage_engine_v3.py used while Ф3 didn't exist
yet) into compute_storage_risk, driven by realistic agent-shaped event
fixtures (source/event_id/received_at, including the new T3.1 ids).

The per-rule math (exact deltas/multipliers for chain_stage2/3, burstiness,
early_events, synergy) is already pinned against a duck-typed fake chain in
test_storage_engine_v3.py; this file only proves a *real* ErrChain plugs into
the same rules identically, via the real analyze_events() pipeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from server.analytics.errchain import analyze_events
from server.analytics.storage import compute_storage_risk

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def _event(source, event_id, days_ago):
    ts = _NOW - timedelta(days=days_ago)
    return {"source": source, "event_id": event_id, "received_at": ts.isoformat(), "level": "Error"}


def _disk(**kw):
    base = {"disk": "PhysicalDisk0", "media_type": "SSD", "serial_hash": "diskhash1"}
    base.update(kw)
    return base


def _hist(disks):
    return {"storage": disks}


def _coords(score):
    return score.source_lineage["coords"]


def test_chain_alone_never_overrides_full_unknown_gate():
    """A device with genuinely zero SMART fields anywhere stays UNKNOWN even
    with a real stage-3 chain -- errchain is R-side evidence for a reporting
    disk, never a replacement for missing telemetry (К5)."""
    events = [_event("disk", 51, 10), _event("System", 41, 5)]
    chain = analyze_events(events, now=_NOW)
    assert chain.stage == 3
    no_smart_disk = {"disk": "PhysicalDisk0", "media_type": "SSD"}
    s = compute_storage_risk(_hist([no_smart_disk]), None, chain=chain)
    assert s.band == "unknown"
    assert s.value is None


def test_chain_stage3_with_clean_smart_caps_axis_at_watch():
    """SMART reports (has_smart passes) but nothing on it is actually bad;
    stage-3 chain evidence alone must still cap the axis at watch, never bad
    (a pure event-log signal is a suspicion, not a verdict -- T2.2)."""
    events = [_event("disk", 51, 10), _event("System", 41, 5)]
    chain = analyze_events(events, now=_NOW)
    assert chain.stage == 3
    clean_disk = _disk(temperature_c=20.0)  # has_smart=True, but no rule fires on 20C
    s = compute_storage_risk(_hist([clean_disk]), None, chain=chain)
    assert s.band == "watch"
    assert s.value == 25.0  # chain_stage3 flat addend only, nothing else fired


def test_chain_stage3_with_real_smart_defect_matches_fake_chain_pin():
    """Must reproduce test_storage_engine_v3.py's _FakeChain(stage=3) pin
    (value == 70.0) exactly when driven by a real analyzed chain instead."""
    events = [_event("disk", 51, 10), _event("System", 41, 5)]
    chain = analyze_events(events, now=_NOW)
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    coords = _coords(s)
    assert "chain_stage3" in coords["flags"]
    assert coords["resilience_loss"] >= 45.0
    assert s.value == 70.0  # media_errors (D45, axis+45) + stage3 (axis+25) flat, no multiplier


def test_early_only_chain_flags_early_events_rule():
    events = [_event("disk", 153, 3), _event("storahci", 129, 2)]
    chain = analyze_events(events, now=_NOW)
    assert chain.counts == {"early": 2, "damage": 0, "crash": 0, "app_hang": 0}
    assert chain.stage == 1
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    assert "early_events" in _coords(s)["flags"]


def test_damage_alongside_early_does_not_fire_early_events_rule():
    events = [_event("disk", 153, 3), _event("Ntfs", 55, 2)]
    chain = analyze_events(events, now=_NOW)
    assert chain.stage == 2
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    assert "early_events" not in _coords(s)["flags"]


def test_agent_shaped_fixture_with_new_t31_ids_wires_end_to_end():
    """Fixture shaped like real collector output: storahci/stornvme retries,
    disk/157 (collected per T3.1, not yet classified into any coordinate --
    К8), and Application Hang, mixed with an unrelated id that must be
    ignored entirely."""
    events = [
        _event("storahci", 129, 3),
        _event("stornvme", 129, 6),
        _event("disk", 157, 1),
        _event("Application", 1002, 2),
        _event("Application", 1002, 10),
        _event("Microsoft-Windows-WHEA-Logger", 17, 1),
    ]
    chain = analyze_events(events, now=_NOW)
    assert chain.counts["early"] == 2
    assert chain.counts["app_hang"] == 2
    assert chain.stage == 1
    s = compute_storage_risk(_hist([_disk(wear_pct=90.0)]), None, chain=chain)
    assert s.value > 0
    assert s.band != "unknown"


def test_no_chain_reproduces_pre_f3_behavior():
    """chain=None (the Ф2 forward-reference default) must still work exactly
    as before -- Ф3 wiring is additive, not a required argument."""
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=None)
    assert "chain_stage3" not in _coords(s)["flags"]
    assert s.value == 45.0
