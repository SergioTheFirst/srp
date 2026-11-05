"""Spec H5 + H12 (2026-10-30): honest SMART evidence and data sufficiency without SMART.

R5: 27-29 fleet PCs report only wear / temperature / flush ("phantom SMART") and read
h0 at high confidence with no health data at all. A phantom keeps its O (its flags are
real evidence) but its storage confidence is at most low, and a final h0 -- for it and
for a device that never had SMART -- stands only on the events channel plus the OS axis.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from server.analytics.health import compute_health
from server.analytics.storage import _has_health_data, compute_storage_risk
from server.pipeline import _storage_axis

_T0 = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
_ZERO = {"early": 0, "damage": 0, "crash": 0, "app_hang": 0}
DELIVERED = {
    "stage": 0,
    "burstiness": None,
    "recurrent_weeks": 0,
    "counts": _ZERO,
    "factors": [],
    "delivered": True,
}
DEAD = {**DELIVERED, "delivered": False}  # a failed events collector (prod shape)
OS = {"value": 5.0, "band": "good", "confidence": "high", "factors": []}
PHANTOM_SPOT = "SMART недоступен (только износ и температура)"
PHANTOM_MISSING = "SMART/NVMe-лог не получен — оценка только по износу и температуре"
AVAILABLE = "оценка по доступным данным: SMART недоступен"


def _storage(health_ids: Optional[list]) -> dict:
    lineage = {"worst_disk_id": "d1", "disk_ids": ["d1"], "attr_disk_ids": []}
    if health_ids is not None:  # absent key = a lineage from before H5
        lineage["health_disk_ids"] = health_ids
    coords = {"damage": 0.0, "resilience_loss": 0.0, "flags": []}
    return {
        "value": 3.0,
        "band": "good",
        "confidence": "high",
        "factors": [],
        "source_lineage": lineage,
        "coords": coords,
    }


def _verdict(axes: dict, errchain: Optional[dict] = DELIVERED, prev: Optional[dict] = None):
    return compute_health(axes, {}, {}, errchain, None, prev, now=_T0)


def _labels(items: list) -> list:
    return [i.get("label") for i in items]


# --------------------------------------------------------------------------- #
# H5: what counts as health data, and the phantom
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("disk", "has"),
    [
        ({"smart_attrs": {"5": 0}}, True),
        ({"nvme_spare_pct": 100}, True),
        ({"read_errors_uncorrected": 0, "write_errors_uncorrected": 0}, True),
        ({"smart_predict_fail": False}, True),
        ({"wear_pct": 1, "temperature_c": 44, "flush_latency_max_ms": 3}, False),  # phantom
        ({"smart_attrs": {}}, False),
    ],
)
def test_health_data_is_smart_nvme_uncorrected_or_predict(disk: dict, has: bool) -> None:
    assert _has_health_data(disk) is has


def test_phantom_with_events_and_os_stays_h0_at_low_confidence() -> None:
    v = _verdict({"storage_risk": _storage([]), "os_degradation_risk": OS})
    assert v.state == "h0"
    assert PHANTOM_SPOT in v.blind_spots and PHANTOM_MISSING in v.missing_evidence
    assert AVAILABLE in _labels(v.state_evidence)
    assert v.confidence == "low" and v.damage.confidence == "low"
    assert v.observability.value is not None and v.observability.value >= 40  # O unchanged


@pytest.mark.parametrize(
    "axes_errchain",
    [
        ({"os_degradation_risk": OS}, None),  # no events channel
        ({"os_degradation_risk": OS}, DEAD),  # events collector failed (prod shape)
        ({}, DELIVERED),  # no OS axis
    ],
)
def test_phantom_without_events_or_os_is_unknown(axes_errchain) -> None:
    extra, errchain = axes_errchain
    v = _verdict({"storage_risk": _storage([]), **extra}, errchain)
    assert v.state == "unknown"
    assert PHANTOM_SPOT in v.blind_spots and v.confidence == "low"
    assert any(m.startswith("недостаточно данных: SMART недоступен") for m in v.missing_evidence)


def test_old_lineage_without_health_ids_is_no_phantom() -> None:
    v = _verdict({"storage_risk": _storage(None)}, None)
    assert v.state == "h0" and PHANTOM_SPOT not in v.blind_spots


@pytest.mark.parametrize("prev", [{"health_seen_ids": ["d1"]}, {"attr_disk_ids": ["d1"]}])
def test_a_disk_that_had_health_data_is_no_phantom(prev: dict) -> None:
    prev = {"state": "h0", "last_known_state": "h0", "worst_disk_id": "d1", **prev}
    v = _verdict({"storage_risk": _storage([])}, None, prev)
    assert v.state == "h0" and PHANTOM_SPOT not in v.blind_spots
    assert v.health_seen_ids == ["d1"]  # sticky, per disk


def test_health_seen_latches_on_the_first_cycle_with_health_data() -> None:
    v = _verdict({"storage_risk": _storage(["d1"])}, None)
    assert v.health_seen_ids == ["d1"] and PHANTOM_SPOT not in v.blind_spots


# --------------------------------------------------------------------------- #
# H12: a device that never had SMART (VM)
# --------------------------------------------------------------------------- #
AGING = {"value": 0.0, "band": "good", "confidence": "high", "factors": [], "coords": {"flags": []}}
MATURE = {"disk_tail_ratio": {"n_points": 8, "direction": "flat"}}  # depth channel


def test_never_smart_device_needs_events_and_os_for_h0() -> None:
    # a VM: no storage axis, no disk ever -- O >= 40 from events + perf + axes
    ok = _verdict({"os_degradation_risk": OS, "software_aging_risk": AGING})
    assert ok.state == "h0" and AVAILABLE in _labels(ok.state_evidence)
    assert ok.confidence in ("low", "medium")
    # no events channel, O still >= 40 (depth instead): H12, not K5, makes it unknown
    axes = {"os_degradation_risk": OS, "software_aging_risk": AGING}
    for errchain in (None, DEAD):  # prod sends asdict(ErrChain), never None
        blind = compute_health(axes, {}, MATURE, errchain, None, None, now=_T0)
        assert blind.observability.value is not None and blind.observability.value >= 40
        assert blind.state == "unknown"
        assert any("нет анализа событий" in m for m in blind.missing_evidence)


def test_a_held_level_of_a_phantom_is_not_touched() -> None:
    # H12 only touches a FINAL h0: a phantom held h3 (disk 7 chain) stays h3
    prev = {
        "state": "h3",
        "below_h": 0.0,
        "below_seen_at": _T0.isoformat(),
        "worst_disk_id": "d1",
        "disk_ids": ["d1"],
        "had_disk": True,
    }
    v = compute_health(
        {"storage_risk": _storage([])}, {}, {}, None, None, prev, now=_T0 + timedelta(hours=4)
    )
    assert v.state == "h3"
    # H5 marks every phantom; only H12 is limited to a final h0
    assert PHANTOM_SPOT in v.blind_spots and PHANTOM_MISSING in v.missing_evidence
    assert v.confidence == "low" and v.damage.confidence == "low"


# --------------------------------------------------------------------------- #
# Review 3e: "never had health data" is per disk; the real engine's lineage
# --------------------------------------------------------------------------- #
def _listed(ids: list, health: list) -> dict:
    """Disks listed now, with the Get-PhysicalDisk inventory the pipeline adds (D3)."""
    axis = _storage(health)
    lineage = {**axis["source_lineage"], "disk_ids": ids, "worst_disk_id": ids[0]}
    axis["source_lineage"] = {**lineage, "inventory_disk_ids": ids}
    return axis


@pytest.mark.parametrize(
    ("prev_ids", "seen_before", "now_ids"),
    [
        (["old"], ["old"], ["new"]),  # the SMART disk swapped for a wear/temp-only NVMe
        (["hdd", "nvme"], ["hdd"], ["nvme"]),  # the SMART HDD pulled, the phantom NVMe left
    ],
)
def test_a_phantom_disk_that_replaced_a_smart_one_is_still_a_phantom(
    prev_ids: list, seen_before: list, now_ids: list
) -> None:
    prev = {
        "state": "h0",
        "last_known_state": "h0",
        "worst_disk_id": prev_ids[0],
        "disk_ids": prev_ids,
        "attr_disk_ids": seen_before,
        "health_seen_ids": seen_before,
        "had_disk": True,
    }
    v = _verdict({"storage_risk": _listed(now_ids, [])}, None, prev)
    assert PHANTOM_SPOT in v.blind_spots and v.confidence == "low"
    assert v.state == "unknown"  # no events, no OS axis: H12
    assert v.health_seen_ids == []


# the fleet's phantom (R5): only wear / temperature / flush -- through the real engine
_FLEET_PHANTOM = {
    "disk": "NVMe X",
    "serial_hash": "p1",
    "wear_pct": 3,
    "temperature_c": 41,
    "flush_latency_max_ms": 4,
}


def _engine_axis(disk: dict) -> dict:
    return _storage_axis(compute_storage_risk({"storage": [disk]}, None), None)


def test_the_engines_health_ids_reach_the_verdict() -> None:
    ghost = _verdict({"storage_risk": _engine_axis(_FLEET_PHANTOM), "os_degradation_risk": OS})
    assert PHANTOM_SPOT in ghost.blind_spots and ghost.confidence == "low"
    real = {**_FLEET_PHANTOM, "read_errors_uncorrected": 0, "write_errors_uncorrected": 0}
    healthy = _verdict({"storage_risk": _engine_axis(real), "os_degradation_risk": OS})
    assert PHANTOM_SPOT not in healthy.blind_spots and healthy.health_seen_ids == ["p1"]


# --------------------------------------------------------------------------- #
# Verification of review 3e: a SMART disk still installed is never a phantom (§5.16)
# --------------------------------------------------------------------------- #
def _disks(rows: list, inventory: list, worst: str) -> dict:
    axis = _storage([])  # the rows present carry no health data (the phantom NVMe)
    lineage = {**axis["source_lineage"], "disk_ids": rows, "worst_disk_id": worst}
    axis["source_lineage"] = {**lineage, "inventory_disk_ids": inventory}
    return axis


def _with_hdd(state: str, worst: str) -> dict:
    return {
        "state": state,
        "last_known_state": state,
        "worst_disk_id": worst,
        "disk_ids": ["hdd", "nvme"],
        "attr_disk_ids": ["hdd"],
        "health_seen_ids": ["hdd"],
        "had_disk": True,
        "below_h": 0.0,
        "below_seen_at": _T0.isoformat(),
    }


@pytest.mark.parametrize("inventory", [["hdd", "nvme"], []])  # still listed / no inventory
@pytest.mark.parametrize("state", ["h0", "h4"])  # a healthy PC, a held dying-disk alarm
def test_a_smart_disk_dropped_from_the_rows_is_loss_not_phantom(
    state: str, inventory: list
) -> None:
    axes = {"storage_risk": _disks(["nvme"], inventory, "nvme"), "os_degradation_risk": OS}
    v = _verdict(axes, None, _with_hdd(state, "hdd"))
    assert v.blind_cause == "loss" and v.state == state
    assert PHANTOM_SPOT not in v.blind_spots and v.damage.confidence == "high"


def test_a_non_worst_smart_disk_out_of_the_rows_keeps_its_history() -> None:
    # the phantom NVMe is the worst disk; the SMART HDD drops out while still installed
    out = _verdict(
        {"storage_risk": _disks(["nvme"], ["hdd", "nvme"], "nvme")}, None, _with_hdd("h0", "nvme")
    )
    assert PHANTOM_SPOT not in out.blind_spots and out.health_seen_ids == ["hdd"]
    # ...and comes back with its SMART script timed out: still itself, not a phantom
    prev = {**_with_hdd("h0", "nvme"), "health_seen_ids": out.health_seen_ids}
    back = _verdict({"storage_risk": _disks(["nvme", "hdd"], ["hdd", "nvme"], "nvme")}, None, prev)
    assert PHANTOM_SPOT not in back.blind_spots and back.state == "h0"
