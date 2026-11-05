"""calib D9/D12 (T3): event hygiene.

- One dedup point in the data layer: ``db.store_events`` skips re-sent copies, and
  ``db.get_recent_events`` collapses copies stored before the fix.
- errchain: the agent's 7-digit ts is parsed, plausibility-checked against its receipt,
  and orders the damage -> BugCheck 1001 pair. KP41/6008 never complete stage 3, the
  crash must be recent unless two such pairs fall in the 30-day window, and an Ntfs 55
  right after a power loss is not disk damage.
- validators: attr 197 is a gauge, NVMe Percentage Used and Wear go up to 255, and
  storage_reliability counter drops are checked only for the same serial_hash.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from server.analytics import errchain
from server.trust.states import SemanticStatus
from server.trust.validators import validate_smart_item, validate_storage_item

pytestmark = pytest.mark.unit

_BUGCHECK = "Microsoft-Windows-WER-SystemErrorReporting"
_KERNEL_POWER = "Microsoft-Windows-Kernel-Power"
_NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
_RECV = "2026-10-09T09:00:00+00:00"
_EV = {
    "ts": "2026-10-09T08:31:12.1234567Z",
    "log": "System",
    "source": "disk",
    "event_id": 7,
    "level": "Error",
    "message": "bad block",
}


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _stored(db, device_id):
    with db._connect() as conn:
        return conn.execute(
            "SELECT ts, received_at FROM events WHERE device_id=? ORDER BY id", (device_id,)
        ).fetchall()


def _rollup_n(db, device_id):
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT n FROM event_rollup_daily WHERE device_id=?", (device_id,)
        ).fetchall()
    return [r["n"] for r in rows]


def _ev(source, event_id, ts=None, received_at=_RECV):
    event = {"source": source, "event_id": event_id, "received_at": received_at}
    if ts is not None:
        event["ts"] = ts
    return event


def _ago(days):
    return (_NOW - timedelta(days=days)).isoformat()


def _labels(chain):
    return [f["label"] for f in chain.factors]


# --------------------------------------------------------------------------- #
# db.store_events: write-side dedup
# --------------------------------------------------------------------------- #


def test_store_events_skips_resent_copy(db_init):
    db_init.store_events("d", [dict(_EV)], received_at="2026-10-09T09:00:00+00:00")
    db_init.store_events("d", [dict(_EV)], received_at="2026-10-09T13:00:00+00:00")

    rows = _stored(db_init, "d")
    assert [r["received_at"] for r in rows] == ["2026-10-09T09:00:00+00:00"]
    assert _rollup_n(db_init, "d") == [1]


def test_store_events_skips_copy_repeated_in_one_batch(db_init):
    db_init.store_events("d", [dict(_EV), dict(_EV)], received_at=_RECV)
    assert len(_stored(db_init, "d")) == 1


def test_store_events_keeps_rows_without_ts(db_init):
    no_ts = {**_EV, "ts": None}
    db_init.store_events("d", [dict(no_ts), dict(no_ts)], received_at=_RECV)
    db_init.store_events("d", [dict(no_ts)], received_at="2026-10-09T13:00:00+00:00")
    assert len(_stored(db_init, "d")) == 3


def test_store_events_keeps_distinct_events_with_same_id(db_init):
    later = {**_EV, "ts": "2026-10-09T08:45:00.0000000Z"}
    db_init.store_events("d", [dict(_EV)], received_at=_RECV)
    db_init.store_events("d", [dict(_EV), dict(later)], received_at="2026-10-09T13:00:00+00:00")
    assert [r["ts"] for r in _stored(db_init, "d")] == [_EV["ts"], later["ts"]]


def test_store_events_dedup_lookback_is_bounded(db_init):
    """Only keys received within the last 48 h are looked up (2x the agent's 24 h window)."""
    db_init.store_events("d", [dict(_EV)], received_at="2026-10-06T09:00:00+00:00")
    db_init.store_events("d", [dict(_EV)], received_at="2026-10-09T09:00:00+00:00")
    assert len(_stored(db_init, "d")) == 2


def test_store_events_dedup_is_per_device(db_init):
    db_init.store_events("a", [dict(_EV)], received_at=_RECV)
    db_init.store_events("b", [dict(_EV)], received_at=_RECV)
    assert len(_stored(db_init, "b")) == 1


# --------------------------------------------------------------------------- #
# db.get_recent_events: read-side collapse of history stored before the fix
# --------------------------------------------------------------------------- #


def test_get_recent_events_collapses_stored_duplicates(db_init):
    rows = [  # pre-fix history: the same event re-sent 4 h later, stored twice
        ("d", "2026-10-09T08:31:12.1234567Z", "disk", 7, "2026-10-09T09:00:00+00:00"),
        ("d", None, "disk", 7, "2026-10-09T09:00:00+00:00"),
        ("d", "2026-10-09T08:31:12.1234567Z", "disk", 7, "2026-10-09T13:00:00+00:00"),
        ("d", None, "disk", 7, "2026-10-09T13:00:00+00:00"),
        ("d", "2026-10-09T12:00:00.0000000Z", "disk", 51, "2026-10-09T13:00:00+00:00"),
    ]
    with db_init._lock, db_init._connect() as conn:
        conn.executemany(
            "INSERT INTO events (device_id, ts, source, event_id, received_at, level)"
            " VALUES (?,?,?,?,?,?)",
            [(*row, "Error") for row in rows],
        )

    got = [(r["ts"], r["event_id"], r["received_at"]) for r in db_init.get_recent_events("d")]

    assert got == [  # newest-first, earliest copy kept, ts-less rows never collapsed
        ("2026-10-09T12:00:00.0000000Z", 51, "2026-10-09T13:00:00+00:00"),
        (None, 7, "2026-10-09T13:00:00+00:00"),
        (None, 7, "2026-10-09T09:00:00+00:00"),
        ("2026-10-09T08:31:12.1234567Z", 7, "2026-10-09T09:00:00+00:00"),
    ]


# --------------------------------------------------------------------------- #
# errchain: agent ts parsing
# --------------------------------------------------------------------------- #


def test_agent_seven_digit_ts_parses():
    parsed = errchain._parse_event_ts("2026-10-09T08:31:12.1234567Z")
    assert parsed == datetime(2026, 10, 9, 8, 31, 12, 123456, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 2026-10-09T08:31:12.5+03:00 ", datetime(2026, 10, 9, 5, 31, 12, 500000, timezone.utc)),
        ("2026-10-09T08:31:12", datetime(2026, 10, 9, 8, 31, 12, tzinfo=timezone.utc)),
        ("not a timestamp", None),
        ("", None),
        (None, None),
        (12345, None),
    ],
)
def test_event_ts_fraction_padded_naive_utc_garbage_none(raw, expected):
    assert errchain._parse_event_ts(raw) == expected


# --------------------------------------------------------------------------- #
# errchain: stage 3 = damage, then BugCheck 1001
# --------------------------------------------------------------------------- #


def test_same_batch_damage_then_bugcheck_is_stage_3():
    events = [
        _ev("disk", 51, "2026-10-09T08:31:12.1234567Z"),
        _ev(_BUGCHECK, 1001, "2026-10-09T08:36:40.7654321Z"),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.stage == 3
    assert _labels(chain)[0] == "повреждение диска, затем BSOD в течение 7 дней"


def test_crash_before_damage_by_event_ts_is_not_stage_3_in_one_batch():
    events = [
        _ev(_BUGCHECK, 1001, "2026-10-09T08:29:00.0000000Z"),
        _ev("disk", 7, "2026-10-09T08:31:00.0000000Z"),
    ]
    assert errchain.analyze_events(events, now=_NOW).stage == 2


def test_implausible_ts_falls_back_to_received_at():
    """A crash ts 3 days before its receipt is implausible (window is 24 h + drift):
    the pair is ordered by received_at, and one batch passes the inclusive rule."""
    events = [
        _ev(_BUGCHECK, 1001, "2026-10-06T09:00:00.0000000Z"),
        _ev("disk", 51, "2026-10-09T08:31:00.0000000Z"),
    ]
    assert errchain.analyze_events(events, now=_NOW).stage == 3


def test_ts_ahead_of_receipt_falls_back_to_received_at():
    """A damage ts 15 min after its receipt breaks the +10 min lead bound (clock ahead)."""
    events = [
        _ev("disk", 51, "2026-10-09T09:15:00.0000000Z"),
        _ev(_BUGCHECK, 1001, "2026-10-09T08:00:00.0000000Z"),
    ]
    assert errchain.analyze_events(events, now=_NOW).stage == 3


@pytest.mark.parametrize(
    ("crash_ts", "stage"),
    [
        ("2026-10-08T06:00:00.0000000Z", 3),  # receipt - 27 h: implausible, one batch orders it
        ("2026-10-08T08:00:00.0000000Z", 2),  # receipt - 25 h: plausible, before the damage
    ],
)
def test_ts_plausible_back_to_26_hours_before_receipt(crash_ts, stage):
    events = [_ev("disk", 51, "2026-10-09T08:00:00.0000000Z"), _ev(_BUGCHECK, 1001, crash_ts)]
    assert errchain.analyze_events(events, now=_NOW).stage == stage


def test_damage_and_bugcheck_with_equal_ts_is_not_stage_3():
    ts = "2026-10-09T08:31:00.0000000Z"
    events = [_ev("disk", 51, ts), _ev(_BUGCHECK, 1001, ts)]
    assert errchain.analyze_events(events, now=_NOW).stage == 2


def test_kp41_after_damage_is_stage_2_not_3():
    events = [
        _ev("disk", 51, "2026-10-09T08:31:00.0000000Z"),
        _ev(_KERNEL_POWER, 41, "2026-10-09T08:40:00.0000000Z"),
        _ev("EventLog", 6008, "2026-10-09T08:40:00.0000000Z"),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.stage == 2
    assert chain.counts["crash"] == 2


def test_bugcheck_after_damage_within_7_days_is_stage_3():
    events = [
        _ev("disk", 51, "2026-10-04T08:00:00.0000000Z", received_at=_ago(5)),
        _ev(_BUGCHECK, 1001, "2026-10-08T08:00:00.0000000Z", received_at=_ago(1)),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.stage == 3
    assert chain.counts == {"early": 0, "damage": 1, "crash": 1, "app_hang": 0}


def test_stage_3_expires_7_days_after_the_crash():
    events = [_ev("disk", 51, received_at=_ago(9)), _ev(_BUGCHECK, 1001, received_at=_ago(8))]
    assert errchain.analyze_events(events, now=_NOW).stage == 2


def _bsod(day):
    """A BSOD reboot: KP41 + 6008 at boot, then WER writes 1001 once the dump is saved."""
    return [(day, _KERNEL_POWER, 41), (day, "EventLog", 6008), (day + 0.005, _BUGCHECK, 1001)]


# Final review F2c (NVMe without SMART), in sim days: paging errors at d20.3, d21.5 and every
# 2 days from d23.5 to d39.5; a BSOD at d22.5 and a second one at d31.
_F2C = (
    [(day, "disk", 51) for day in (20.3, 21.5, *(23.5 + 2 * i for i in range(9)))]
    + _bsod(22.5)
    + _bsod(31.0)
)


def _sim(timeline, today):
    """(sim day, source, event_id) rows as seen on sim day ``today`` (= _NOW): ts is the
    event time, received_at one hour later (the agent's next run)."""
    at = [(_NOW - timedelta(days=today - day), src, eid) for day, src, eid in timeline]
    return [
        _ev(src, eid, t.isoformat(), (t + timedelta(hours=1)).isoformat()) for t, src, eid in at
    ]


def test_two_disk_bugchecks_in_30_days_hold_stage_3_past_7_days():
    """F2c at d40: the latest disk BSOD is 9 days old, yet two damage -> BugCheck pairs in the
    30-day window stay h4 evidence (two same-class crashes in 30 days: P(third) 53-59 %)."""
    chain = errchain.analyze_events(_sim(_F2C, 40.0), now=_NOW)
    assert chain.stage == 3
    assert chain.counts == {"early": 0, "damage": 11, "crash": 6, "app_hang": 0}


def test_repeat_crash_rule_ends_when_the_first_crash_leaves_the_30_day_window():
    assert errchain.analyze_events(_sim(_F2C, 53.0), now=_NOW).stage == 2


def test_single_old_disk_bugcheck_stays_stage_2_beside_unattributed_crashes():
    """One damage -> BugCheck pair 10 days old is past the 7-day recency, whatever else is in
    the window: its KP41/6008, a second damage before it, a BugCheck no damage preceded."""
    timeline = [(20.0, _BUGCHECK, 1001), (28.0, "disk", 7), (29.0, "disk", 51), *_bsod(30.0)]
    assert errchain.analyze_events(_sim(timeline, 40.0), now=_NOW).stage == 2


# --------------------------------------------------------------------------- #
# errchain: Ntfs 55 written by the unclean shutdown itself
# --------------------------------------------------------------------------- #

_NTFS_FACTOR = "ошибки NTFS сразу после потери питания не считаются повреждением диска: 1"


def test_ntfs55_right_after_power_loss_is_not_damage():
    events = [
        _ev(_KERNEL_POWER, 41, "2026-10-09T08:31:00.1234567Z"),
        _ev("Ntfs", 55, "2026-10-09T08:33:00.1234567Z"),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.stage == 0
    assert chain.counts == {"early": 0, "damage": 0, "crash": 1, "app_hang": 0}
    assert _NTFS_FACTOR in _labels(chain)


def test_ntfs55_before_power_loss_stays_damage():
    events = [
        _ev("Ntfs", 55, "2026-10-09T08:01:00.0000000Z"),
        _ev(_KERNEL_POWER, 41, "2026-10-09T08:31:00.0000000Z"),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.counts["damage"] == 1
    assert _NTFS_FACTOR not in _labels(chain)


def test_ntfs55_after_power_loss_kept_when_other_damage_nearby():
    events = [
        _ev("disk", 7, "2026-10-07T08:00:00.0000000Z", received_at=_ago(2)),
        _ev(_KERNEL_POWER, 41, "2026-10-09T08:31:00.1234567Z"),
        _ev("Ntfs", 55, "2026-10-09T08:33:00.1234567Z"),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.counts["damage"] == 2
    assert chain.stage == 2
    assert _NTFS_FACTOR not in _labels(chain)


def test_ntfs55_after_bugcheck_stays_damage():
    events = [
        _ev(_KERNEL_POWER, 41, "2026-10-09T08:31:00.0000000Z"),
        _ev(_BUGCHECK, 1001, "2026-10-09T08:32:00.0000000Z"),
        _ev("Ntfs", 55, "2026-10-09T08:33:00.0000000Z"),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.counts["damage"] == 1
    assert chain.stage == 2  # NTFS damage lands AFTER the BugCheck: not damage -> crash
    assert _NTFS_FACTOR not in _labels(chain)


def test_ntfs55_before_the_bugcheck_of_a_bsod_reboot_stays_damage():
    """A BSOD reboot logs KP41, then Ntfs 55, and the WER 1001 only once the dump is saved."""
    events = [
        _ev(_KERNEL_POWER, 41, "2026-10-09T08:31:00.0000000Z"),
        _ev("Ntfs", 55, "2026-10-09T08:31:20.0000000Z"),
        _ev(_BUGCHECK, 1001, "2026-10-09T08:32:05.0000000Z"),
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.counts["damage"] == 1
    assert chain.stage == 3
    assert _NTFS_FACTOR not in _labels(chain)


def test_ntfs55_stays_damage_with_same_batch_bugcheck_of_implausible_ts():
    events = [
        _ev(_KERNEL_POWER, 41, "2026-10-09T08:31:00.0000000Z"),
        _ev("Ntfs", 55, "2026-10-09T08:33:00.0000000Z"),
        _ev(_BUGCHECK, 1001, "2026-10-06T08:32:00.0000000Z"),  # 3 days before its receipt
    ]
    chain = errchain.analyze_events(events, now=_NOW)
    assert chain.counts["damage"] == 1
    assert chain.stage == 3  # the pair is ordered by the shared received_at


def test_ntfs55_long_after_power_loss_stays_damage():
    events = [
        _ev(_KERNEL_POWER, 41, "2026-10-09T07:00:00.0000000Z"),
        _ev("Ntfs", 55, "2026-10-09T08:33:00.0000000Z"),
    ]
    assert errchain.analyze_events(events, now=_NOW).counts["damage"] == 1


# --------------------------------------------------------------------------- #
# validators (D12)
# --------------------------------------------------------------------------- #


def test_attr197_drop_is_plausible():
    status, _ = validate_smart_item(
        {"serial_hash": "abc", "smart_attrs": {"197": 0}},
        last={"serial_hash": "abc", "smart_attrs": {"197": 12}},
    )
    assert status is SemanticStatus.PLAUSIBLE


def test_attr198_drop_still_inconsistent():
    status, reason = validate_smart_item(
        {"serial_hash": "abc", "smart_attrs": {"198": 1}},
        last={"serial_hash": "abc", "smart_attrs": {"198": 4}},
    )
    assert status is SemanticStatus.INCONSISTENT
    assert "198" in (reason or "")


def test_percentage_used_above_100_is_plausible():
    assert validate_smart_item({"nvme_percentage_used": 255}, last=None)[0] is (
        SemanticStatus.PLAUSIBLE
    )
    assert validate_smart_item({"nvme_percentage_used": 256}, last=None)[0] is (
        SemanticStatus.IMPLAUSIBLE
    )
    assert validate_smart_item({"nvme_spare_pct": 101}, last=None)[0] is (
        SemanticStatus.IMPLAUSIBLE
    )


def test_wear_above_255_is_implausible():
    assert validate_storage_item({"wear_pct": 255.0}, last=None)[0] is SemanticStatus.PLAUSIBLE
    status, reason = validate_storage_item({"wear_pct": 256.0}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE
    assert reason == "wear_pct=256.0"


def test_storage_counter_drop_on_new_serial_is_plausible():
    status, _ = validate_storage_item(
        {"serial_hash": "new-disk", "power_on_hours": 10, "reallocated_sectors": 0},
        last={"serial_hash": "old-disk", "power_on_hours": 5000, "reallocated_sectors": 40},
    )
    assert status is SemanticStatus.PLAUSIBLE


def test_storage_counter_drop_same_serial_still_inconsistent():
    status, reason = validate_storage_item(
        {"serial_hash": "abc", "power_on_hours": 10},
        last={"serial_hash": "abc", "power_on_hours": 5000},
    )
    assert status is SemanticStatus.INCONSISTENT
    assert "power_on_hours" in (reason or "")


def test_storage_negative_counter_on_new_serial_still_implausible():
    status, _ = validate_storage_item(
        {"serial_hash": "new-disk", "reallocated_sectors": -1},
        last={"serial_hash": "old-disk", "reallocated_sectors": 3},
    )
    assert status is SemanticStatus.IMPLAUSIBLE
