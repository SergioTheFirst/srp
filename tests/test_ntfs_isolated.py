"""Spec H6 (R4a): an isolated Ntfs 55 is file-system damage, not storage damage.

R4a: three of the fleet's 13 h3 came from an Ntfs 55 with clean SMART, two of them in
the same minute on two machines -- a site-level FS event. Isolated = it passed the
power-loss exception and has no confirmation: no 153/129, no 7/51 in the window, no
BugCheck within 1 h of it, none within 7 days after it. Then h2 «fs_corruption»
(dominant os, a chkdsk action) instead of the disk's h3. Every confirmed path stays.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from server.analytics.errchain import analyze_events
from server.analytics.health import compute_health
from server.analytics.health_dominant import storage_rung
from server.analytics.storage import compute_storage_risk
from server.pipeline import _storage_axis
from server.scoring.bayesian import compute_risk

_NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
_T = _NOW - timedelta(days=2)  # the Ntfs 55
OS = {"value": 5.0, "band": "good", "confidence": "high", "factors": []}
AGING = {"value": 0.0, "band": "good", "confidence": "high", "factors": [], "coords": {"flags": []}}
FS_ACTION = (
    "проверить файловую систему (chkdsk) и корректность выключения ПК; сделать копию важных данных"
)

CLEAN = {"smart_attrs": {"5": 0, "197": 0, "198": 0}}
PHANTOM: dict = {}  # wear / temperature only (R5)


def _e(source: str, eid: int, at: datetime) -> dict:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    received = (at + timedelta(minutes=30)).isoformat()
    return {
        "log": "System",
        "source": source,
        "event_id": eid,
        "level": 2,
        "ts": stamp,
        "received_at": received,
    }


NTFS = _e("Ntfs", 55, _T)


def _disk(**kw: object) -> dict:
    return {
        "disk": "SSD",
        "serial_hash": "s1",
        "media_type": "SSD",
        "bus_type": "SATA",
        "wear_pct": 2,
        "temperature_c": 40,
        **kw,
    }


def _verdict(events: list, disk: Optional[dict], *, gated: bool = False):
    chain = analyze_events(events, now=_NOW)
    axes: dict = {"os_degradation_risk": OS, "software_aging_risk": AGING}
    if gated:
        axes["storage_risk"] = {"value": None, "source_lineage": {"storage_domain": "unknown"}}
    elif disk is not None:
        risk = compute_storage_risk({"storage": [disk]}, None, chain=chain)
        axes["storage_risk"] = _storage_axis(risk, None)
    delivered = {**asdict(chain), "delivered": True}
    return chain, compute_health(axes, {}, {}, delivered, None, None, now=_NOW)


# --------------------------------------------------------------------------- #
# Guards: written before the change, green on it -- every confirmed path stays
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "smart",
    [
        {"smart_attrs": {"197": 1}},
        {"smart_attrs": {"5": 3}},
        {"read_errors_uncorrected": 2, "write_errors_uncorrected": 0},
    ],
)
def test_ntfs55_with_smart_damage_stays_h3(smart: dict) -> None:
    _, v = _verdict([NTFS], _disk(**smart))
    assert v.state in ("h3", "h4")


@pytest.mark.parametrize("smart", [CLEAN, PHANTOM])
@pytest.mark.parametrize(
    ("extra", "least"),
    [
        ([_e("disk", 153, _T - timedelta(days=1))], "h3"),  # early retries: stage 2
        ([_e("storahci", 129, _T - timedelta(days=1))], "h3"),
        ([_e("disk", 7, _T - timedelta(days=1))], "h3"),  # disk damage
        ([_e("disk", 51, _T + timedelta(hours=3))], "h3"),
        (
            [_e("Microsoft-Windows-WER-SystemErrorReporting", 1001, _T - timedelta(minutes=20))],
            "h3",
        ),
        ([_e("Microsoft-Windows-WER-SystemErrorReporting", 1001, _T + timedelta(days=2))], "h4"),
    ],
)
def test_confirmed_ntfs55_keeps_the_disk_path(smart: dict, extra: list, least: str) -> None:
    _, v = _verdict([NTFS, *extra], _disk(**smart))
    order = ["h0", "h1", "h2", "h3", "h4"]
    assert order.index(v.state) >= order.index(least), v.state


def test_disk_7_alone_is_still_stage_2_and_h3() -> None:
    chain, v = _verdict([_e("disk", 7, _T)], _disk(**CLEAN))
    assert chain.stage == 2 and v.state == "h3"


# --------------------------------------------------------------------------- #
# H6 itself
# --------------------------------------------------------------------------- #
def test_isolated_ntfs55_counts_for_stage_but_not_for_the_disk() -> None:
    chain = analyze_events([NTFS], now=_NOW)
    assert chain.stage == 2  # unchanged: Ntfs 55 is still "damage" in stage
    assert (chain.stage_disk_only, chain.fs_isolated) == (0, 1)
    assert chain.counts["damage"] == 1  # counts keys unchanged (C3 2026-10-09)


@pytest.mark.parametrize("smart", [CLEAN, PHANTOM])
def test_isolated_ntfs55_is_h2_fs_corruption_not_the_disks_h3(smart: dict) -> None:
    _, v = _verdict([NTFS], _disk(**smart))
    assert v.state == "h2" and v.dominant == "os" and v.action == FS_ACTION
    assert v.cause_class == "unknown"


def test_isolated_ntfs55_after_a_power_loss_names_power() -> None:
    # KP41 4 h before: past the 1 h unclean-shutdown exception, inside the 24 h cause window
    events = [_e("Microsoft-Windows-Kernel-Power", 41, _T - timedelta(hours=4)), NTFS]
    _, v = _verdict(events, _disk(**CLEAN))
    assert v.state == "h2" and v.cause_class == "power_likely"


def test_a_vm_without_smart_gets_fs_corruption_too() -> None:
    _, v = _verdict([NTFS], None)
    assert v.state == "h2" and v.dominant == "os"


def test_a_gated_axis_keeps_the_full_stage() -> None:
    # SMART damage may exist but be hidden: the Ntfs 55 still counts as damage
    _, v = _verdict([NTFS], None, gated=True)
    assert v.state in ("h3", "h4")


def test_dirty_shutdowns_name_power_too() -> None:
    assert analyze_events([NTFS], now=_NOW, dirty_shutdowns=1).fs_power_likely is True
    assert analyze_events([NTFS], now=_NOW, dirty_shutdowns=0).fs_power_likely is False
    assert analyze_events([], now=_NOW, dirty_shutdowns=3).fs_power_likely is False  # no Ntfs


# --------------------------------------------------------------------------- #
# Review 3f
# --------------------------------------------------------------------------- #
def _axis(flags: list, *, rloss: float = 0.0) -> dict:
    coords = {"damage": 0.0, "resilience_loss": rloss, "flags": flags}
    lineage = {
        "worst_disk_id": "s1",
        "disk_ids": ["s1"],
        "attr_disk_ids": ["s1"],
        "health_disk_ids": ["s1"],
        "inventory_disk_ids": ["s1"],
    }
    return {
        "value": 10.0,
        "band": "good",
        "confidence": "high",
        "factors": [],
        "source_lineage": lineage,
        "coords": coords,
    }


@pytest.mark.parametrize(
    ("flags", "rloss", "state"),
    [
        (["remap_masking", "fs_corruption"], 0.0, "h2"),  # a storage h2 mechanism at the same rung
        (["fs_corruption"], 80.0, "h3"),  # the R loss of the disk set h3 (trajectory / spare)
    ],
)
def test_another_mechanism_at_the_rung_keeps_its_dominant(
    flags: list, rloss: float, state: str
) -> None:
    axes = {
        "storage_risk": _axis(flags, rloss=rloss),
        "os_degradation_risk": OS,
        "software_aging_risk": AGING,
    }
    v = compute_health(
        axes, {}, {}, {"stage": 2, "counts": {"damage": 1}, "factors": []}, None, None, now=_NOW
    )
    assert v.state == state and v.dominant != "os" and v.action != FS_ACTION
    assert v.cause_class is None


def test_smart_damage_on_any_disk_confirms_the_ntfs55() -> None:
    # disk A: uncorrected errors (damage); disk B: worn NVMe without a damage flag that
    # outranks A on D+R -- the device-wide Ntfs 55 is confirmed by A for every disk
    # (B's critical warning is bit 1, temperature: no SMART damage flag of its own)
    chain = analyze_events([NTFS], now=_NOW)
    a = _disk(serial_hash="a", read_errors_uncorrected=2, write_errors_uncorrected=0)
    b = _disk(
        serial_hash="b",
        bus_type="NVMe",
        nvme_percentage_used=97,
        nvme_critical_warning=2,
        nvme_spare_pct=50,
        nvme_spare_threshold_pct=10,
        nvme_media_errors=0,
    )
    for disks in ([a, b], [b, a]):
        coords = compute_storage_risk({"storage": disks}, None, chain=chain).source_lineage[
            "coords"
        ]
        assert "fs_corruption" not in coords["flags"]


def test_a_smart_blind_cycle_keeps_the_full_stage() -> None:
    # SMART damage seen before; this cycle its attributes are gone (H1 attrs lost) while an
    # Ntfs 55 arrives -- the damage may be there but hidden: the full stage (h3), not h2
    _, before = _verdict([], _disk(smart_attrs={"197": 1}))
    chain = analyze_events([NTFS], now=_NOW)
    risk = compute_storage_risk({"storage": [_disk(wear_pct=2)]}, None, chain=chain)
    axes = {
        "storage_risk": _storage_axis(risk, None),
        "os_degradation_risk": OS,
        "software_aging_risk": AGING,
    }
    prev = asdict(before)
    v = compute_health(axes, {}, {}, {**asdict(chain), "delivered": True}, None, prev, now=_NOW)
    assert v.blind_cause == "loss" and v.state in ("h3", "h4")


def test_a_held_fs_level_keeps_its_action_and_cause() -> None:
    events = [_e("Microsoft-Windows-Kernel-Power", 41, _T - timedelta(hours=4)), NTFS]
    _, fresh = _verdict(events, _disk(**CLEAN))
    assert (fresh.state, fresh.cause_class) == ("h2", "power_likely")
    # 31 days later the Ntfs 55 left the window: raw h0, the h2 is held
    later = _NOW + timedelta(days=31)
    axes = {
        "storage_risk": _storage_axis(
            compute_storage_risk({"storage": [_disk(**CLEAN)]}, None), None
        ),
        "os_degradation_risk": OS,
        "software_aging_risk": AGING,
    }
    chain = {**asdict(analyze_events([], now=later)), "delivered": True}
    held = compute_health(axes, {}, {}, chain, None, asdict(fresh), now=later)
    assert (held.state, held.dominant, held.action) == ("h2", "os", FS_ACTION)
    assert held.cause_class == "power_likely"


def test_a_vm_fs_level_names_the_ntfs55() -> None:
    _, v = _verdict([NTFS], None)
    assert any("Ntfs 55" in (f.get("label") or "") for f in v.resilience.evidence)


# --------------------------------------------------------------------------- #
# Final stage-3 review
# --------------------------------------------------------------------------- #
def _storage_level(axis: dict, rung) -> str:
    r = compute_risk(
        None, None, None, domain_values={"storage_risk": axis["value"]}, storage_floor_state=rung
    )
    return next(c["level"] for c in r["classes"] if c["name"] == "storage")


@pytest.mark.parametrize("case", ["smart-blind", "no-smart-value"])
def test_the_bayes_floor_sees_the_chain_stage_the_verdict_keeps(case: str) -> None:
    """H10 x H6: the chain stage compute_health keeps on R itself (a SMART-blind isolated
    Ntfs axis, or no SMART value at all) floors the bayes storage class too."""
    if case == "smart-blind":
        prev: Optional[dict] = asdict(_verdict([NTFS], _disk(**CLEAN))[1])
        chain = analyze_events([NTFS], now=_NOW)
        axis = _storage_axis(compute_storage_risk({"storage": [_disk()]}, None, chain=chain), None)
    else:
        prev = None
        chain = analyze_events([_e("disk", 7, _NOW)], now=_NOW)
        axis = _storage_axis(compute_storage_risk({"storage": []}, None, chain=chain), None)
    errchain = {**asdict(chain), "delivered": True}
    axes = {"storage_risk": axis, "os_degradation_risk": OS, "software_aging_risk": AGING}
    v = compute_health(axes, {}, {}, errchain, None, prev, now=_NOW)
    assert (v.raw_state, v.dominant) == ("h3", "storage")
    rung = storage_rung(axis, {}, errchain, prev)
    assert rung == v.raw_state
    assert _storage_level(axis, rung) == "high"


def test_a_visible_isolated_ntfs55_still_has_no_storage_floor() -> None:
    chain = analyze_events([NTFS], now=_NOW)
    axis = _storage_axis(
        compute_storage_risk({"storage": [_disk(**CLEAN)]}, None, chain=chain), None
    )
    assert storage_rung(axis, {}, {**asdict(chain), "delivered": True}, None) == "h0"


INV = {"disks": [{"serial_hash": "hA", "model": "SSD", "media_type": "SSD", "bus_type": "SATA"}]}


def _trust(status: str, ran: bool) -> dict:
    other = {"collector_status": "ok" if ran else status}
    src = {"state": "unavailable", "collector_status": status}
    return {"sources": {"storage_reliability": src, "reliability": other, "boot_time": other}}


def _gated_cycles(status: str, ran: bool, n: int = 3) -> list:
    """The real engine path: a gate-failed storage domain with zero SMART rows."""
    prev, out = None, []
    for i in range(n):
        now = _NOW + timedelta(hours=i)
        chain = analyze_events([NTFS], now=now)
        risk = compute_storage_risk(None, None, chain=chain, domain_state="unknown")
        axis = _storage_axis(risk, INV, _trust(status, ran))
        axes = {"os_degradation_risk": OS, "software_aging_risk": AGING, "storage_risk": axis}
        v = compute_health(axes, {}, {}, {**asdict(chain), "delivered": True}, None, prev, now=now)
        prev = asdict(v)
        out.append(v)
    return out


@pytest.mark.parametrize(
    ("status", "ran"), [("timeout", False), ("blocked", False), ("absent", False), ("empty", False)]
)
def test_a_failed_read_with_zero_rows_keeps_the_full_stage(status: str, ran: bool) -> None:
    """H6: SMART may exist but be hidden -- the Ntfs 55 still counts as disk damage."""
    for v in _gated_cycles(status, ran):
        assert (v.state, v.dominant) == ("h3", "storage")
        assert "chain_stage2" in v.resilience.flags
        assert "fs_corruption" not in v.resilience.flags


def test_disks_that_answered_nothing_stay_a_machine_without_smart() -> None:
    for v in _gated_cycles("empty", True):  # the script ran: no SMART at all (a VM)
        assert (v.state, v.dominant) == ("h2", "os")
        assert v.resilience.flags == ["fs_corruption"]
