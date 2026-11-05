"""Spec H14 (R4b): the cohort cause of disk errors.

KM-202103: 5 of 8 machines log disk 7 «bad block» -- a disk, firmware or batch problem
of the model, not of one machine. When this machine has disk errors and >= 3 machines /
>= 30 % of its model do too, the cause is fleet_likely and the action names the cohort
(the backup stays); the state never changes.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from server.analytics.errchain import analyze_events
from server.analytics.fleet_anomaly import compute_fleet_anomaly_risk

from tests.test_calib_health import NOW, _axis, _call, _st

FLEET_DISK = (
    "снять образ данных; проверить модель и прошивку накопителя на когорте — начать с машин "
    "с наибольшим числом ошибок"
)
CHAIN = {
    "stage": 2,
    "burstiness": None,
    "recurrent_weeks": 1,
    "counts": {"early": 0, "damage": 3, "crash": 0, "app_hang": 0},
    "factors": [],
    "delivered": True,
    "disk_errors": 3,  # H14: own 7/51/153/129
}
DISK_H3 = _st(40.0, damage=40.0, rloss=30.0, flags=["chain_stage2"])


def _fleet(n: int, size: int = 8) -> dict:
    lin = {"cohort_size": size, "cohort_disk_err_n": n, "cohort_disk_err_pct": round(n / size, 3)}
    return _axis(10.0, lineage=lin)


def _verdict(fleet: dict, chain: dict = CHAIN, prev=None, now=NOW):
    return _call(
        {"storage_risk": DISK_H3, "fleet_anomaly_risk": fleet}, errchain=chain, prev=prev, now=now
    )


def test_a_cohort_wide_disk_problem_names_the_cohort_never_the_state() -> None:
    plain = _verdict(_fleet(0))
    v = _verdict(_fleet(5))  # KM-202103: 5 of 8
    assert (v.state, v.dominant) == (plain.state, plain.dominant) == ("h3", "storage")
    assert (v.cause_class, v.action) == ("fleet_likely", FLEET_DISK)
    assert plain.cause_class is None


@pytest.mark.parametrize(
    ("fleet", "own"),
    [
        (_fleet(2), 3),  # fewer than 3 machines of the model
        (_fleet(3, size=12), 3),  # 25 % of the cohort
        (_fleet(5), 0),  # not this one
    ],
)
def test_no_cohort_cause_below_the_threshold(fleet: dict, own: int) -> None:
    v = _verdict(fleet, chain={**CHAIN, "disk_errors": own})
    assert v.cause_class is None and v.action != FLEET_DISK


def test_a_held_disk_level_keeps_the_cohort_cause() -> None:
    first = _verdict(_fleet(5), now=NOW)
    later = {"storage_risk": _st(10.0), "fleet_anomaly_risk": _fleet(5)}
    v = _call(later, errchain=CHAIN, prev=dataclasses.asdict(first), now=NOW + timedelta(hours=4))
    assert (v.state, v.rung_source) == ("h3", "hold")
    assert (v.cause_class, v.action) == ("fleet_likely", FLEET_DISK)


def test_the_fleet_axis_carries_the_cohort_disk_facts() -> None:
    stats = {"cohort_size": 8, "cohort_disk_err_n": 5}
    lin = compute_fleet_anomaly_risk(stats).source_lineage
    assert (lin["cohort_disk_err_n"], lin["cohort_disk_err_pct"]) == (5, 0.625)
    small = compute_fleet_anomaly_risk({"cohort_size": 1, "cohort_disk_err_n": 1}).source_lineage
    assert small["cohort_disk_err_n"] == 1  # the small-cohort path keeps them too


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _event(source: str, eid: int, received: datetime) -> dict:
    return {
        "log": "System",
        "source": source,
        "event_id": eid,
        "level": 2,
        "ts": received.isoformat(),
    }


def test_the_cohort_query_counts_machines_with_recent_disk_errors(db_init) -> None:
    now = datetime.now(timezone.utc)
    for did, model in (("a", "KM"), ("b", "KM"), ("c", "KM"), ("d", "KM"), ("x", "Other")):
        db_init.upsert_device(did, "2026-07-01T00:00:00Z", "1.0.0", model=model, site_code="s")
        db_init.store_historical(did, "2026-07-01T00:00:00Z", {"avg_boot_ms": 60_000})
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=40)).isoformat()
    db_init.store_events("a", [_event("disk", 7, now)], received_at=recent)
    db_init.store_events("a", [_event("disk", 153, now)], received_at=recent)  # one machine, once
    db_init.store_events("b", [_event("storahci", 129, now)], received_at=recent)
    db_init.store_events("c", [_event("disk", 7, now)], received_at=old)  # outside 30 days
    db_init.store_events("d", [_event("Ntfs", 55, now)], received_at=recent)  # the file system
    db_init.store_events("x", [_event("disk", 7, now)], received_at=recent)  # another model
    assert db_init.get_fleet_cohort_stats("KM", None)["cohort_disk_err_n"] == 2


def _chain_of(source: str, eid: int) -> dict:
    at = NOW - timedelta(hours=5)
    event = {**_event(source, eid, at), "received_at": at.isoformat()}
    return dataclasses.asdict(analyze_events([event], now=NOW))


@pytest.mark.parametrize(
    "storage",
    [
        _st(45.0, damage=45.0, rloss=30.0, flags=["damage_present", "chain_stage2"]),  # SMART h3
        _st(50.0, rloss=45.0, flags=["fs_corruption"]),  # the disk's own R loss, h2
    ],
)
def test_an_ntfs55_is_no_disk_error_of_this_machine(storage: dict) -> None:
    """The cohort counts 7/51/153/129 only -- a lone Ntfs 55 (the file system, H6) puts
    this machine in no disk cohort, whatever its own storage level."""
    axes = {"storage_risk": storage, "fleet_anomaly_risk": _fleet(5)}
    v = _call(axes, errchain=_chain_of("Ntfs", 55), now=NOW)
    assert v.dominant == "storage" and v.state in ("h2", "h3")
    assert v.cause_class is None and v.action != FLEET_DISK
    own = _call(axes, errchain=_chain_of("disk", 7), now=NOW)  # its own bad block: the cohort
    assert (own.cause_class, own.action) == ("fleet_likely", FLEET_DISK)


NO_ERRORS = {
    **CHAIN,
    "stage": 0,
    "disk_errors": 0,
    "counts": {"early": 0, "damage": 0, "crash": 0, "app_hang": 0},
}


@pytest.mark.parametrize(
    ("chain", "fleet"),
    [
        (NO_ERRORS, _fleet(5)),  # the machine's own errors aged out of the 30 days
        (CHAIN, _fleet(2)),  # the cohort count dipped below the threshold
    ],
)
def test_a_hold_keeps_the_cohort_cause_when_its_evidence_drops(chain, fleet) -> None:
    first = _verdict(_fleet(5), now=NOW)
    later = {"storage_risk": _st(10.0), "fleet_anomaly_risk": fleet}
    v = _call(later, errchain=chain, prev=dataclasses.asdict(first), now=NOW + timedelta(hours=4))
    assert (v.state, v.rung_source) == ("h3", "hold")
    assert (v.cause_class, v.action) == ("fleet_likely", FLEET_DISK)


def test_the_disk_share_counts_only_machines_of_the_cohort(db_init) -> None:
    """cohort_size = machines of the model with historical; a machine that sent only
    inventory + events is in neither count, so the share never exceeds the cohort."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    for i in range(10):
        db_init.upsert_device(f"h{i}", "2026-07-01T00:00:00Z", "1.0.0", model="KM", site_code="s")
        db_init.store_historical(f"h{i}", "2026-07-01T00:00:00Z", {"avg_boot_ms": 60_000})
    db_init.store_events("h0", [_event("disk", 7, now)], received_at=recent)
    for did in ("e0", "e1"):  # inventory + events, no historical
        db_init.upsert_device(did, "2026-07-01T00:00:00Z", "1.0.0", model="KM", site_code="s")
        db_init.store_events(did, [_event("disk", 7, now)], received_at=recent)
    stats = db_init.get_fleet_cohort_stats("KM", None)
    assert (stats["cohort_size"], stats["cohort_disk_err_n"]) == (10, 1)
