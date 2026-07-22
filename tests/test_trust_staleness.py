"""server/trust/staleness.py: periodic per-source trust staleness re-evaluation
(P2-2 Ch2). Design: docs/superpowers/specs/2026-07-22-trust-source-staleness-reeval-design.md

Two layers, matching the module's own split:
* reevaluate_staleness -- pure function, unit-tested with plain dicts, no DB/clock.
* run_staleness_cycle / db.get_source_trust_rows / db.apply_source_staleness --
  integration-tested through the real ingest path + a throwaway DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from server.trust.staleness import StaleUpdate, reevaluate_staleness, run_staleness_cycle
from tests.conftest import healthy

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
_THRESHOLD = 43200.0  # 12h


def _row(**over) -> dict:
    base = {
        "device_id": "dev-1",
        "source": "storage_reliability",
        "state": "ok",
        "collector_status": "ok",
        "semantic_status": "plausible",
        "evidence_seen_at": (_NOW - timedelta(hours=1)).isoformat(),
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# reevaluate_staleness -- pure function
# --------------------------------------------------------------------------- #
def test_stale_evidence_on_a_domain_source_flags_stale():
    """RED-proving case: today, nothing ever calls derive_state with a real
    age -- this is the bug P2-2 fixes."""
    row = _row(evidence_seen_at=(_NOW - timedelta(hours=13)).isoformat())  # > 12h threshold
    updates = reevaluate_staleness([row], _NOW, _THRESHOLD)
    assert len(updates) == 1
    u = updates[0]
    assert u.device_id == "dev-1"
    assert u.source == "storage_reliability"
    assert u.state == "stale"
    assert u.weight == 0.0


def test_fresh_evidence_produces_no_update():
    row = _row(evidence_seen_at=(_NOW - timedelta(hours=1)).isoformat())  # well under 12h
    assert reevaluate_staleness([row], _NOW, _THRESHOLD) == []


def test_non_domain_source_never_goes_stale():
    """print_jobs/events/identity/certificates have no reporting cadence -- an
    old evidence timestamp there means "nothing happened", not "gone silent"
    (design D1). Must never be flagged even with very old evidence."""
    row = _row(source="print_jobs", evidence_seen_at=(_NOW - timedelta(days=30)).isoformat())
    assert reevaluate_staleness([row], _NOW, _THRESHOLD) == []


@pytest.mark.parametrize("worse_state", ["unavailable", "suspect"])
def test_already_worse_than_stale_state_is_untouched(worse_state):
    """derive_state's own precedence ladder (SUSPECT/UNAVAILABLE outrank STALE)
    means these rows produce the same verdict again regardless of age -- no
    update, no revival, no churn (design D7). NOT_APPLICABLE is excluded here:
    it is gated by derive_state's separate `applicable` bool, never derivable
    from stored collector_status/semantic_status alone, and no production
    source currently sets it (server/trust/domains.py's own docstring)."""
    collector = "blocked" if worse_state == "unavailable" else "ok"
    semantic = "known_bad" if worse_state == "suspect" else "plausible"
    row = _row(
        state=worse_state,
        collector_status=collector,
        semantic_status=semantic,
        evidence_seen_at=(_NOW - timedelta(days=30)).isoformat(),
    )
    assert reevaluate_staleness([row], _NOW, _THRESHOLD) == []


def test_missing_evidence_timestamp_is_never_flagged():
    """Defensive: nothing to age a row against yet -- not an error, not a flag."""
    row = _row(evidence_seen_at=None)
    assert reevaluate_staleness([row], _NOW, _THRESHOLD) == []


def test_malformed_status_row_is_skipped_not_crashed():
    row = _row(collector_status="not-a-real-status", evidence_seen_at="2020-01-01T00:00:00+00:00")
    assert reevaluate_staleness([row], _NOW, _THRESHOLD) == []


def test_repeated_evaluation_carries_the_same_evidence_seen_at():
    """Anti-reset guard (P1-4 trap): the update itself always carries the
    evidence_seen_at it was computed FROM, unmodified -- never a fresh
    "now"-like stamp. db.apply_source_staleness (Ch2) relies on this being the
    original value for its optimistic-concurrency guard."""
    stale_evidence = (_NOW - timedelta(hours=13)).isoformat()
    row = _row(evidence_seen_at=stale_evidence)
    first = reevaluate_staleness([row], _NOW, _THRESHOLD)
    later = _NOW + timedelta(hours=5)
    second = reevaluate_staleness([row], later, _THRESHOLD)
    assert first[0].evidence_seen_at == stale_evidence
    assert second[0].evidence_seen_at == stale_evidence  # unchanged across repeated passes


# --------------------------------------------------------------------------- #
# run_staleness_cycle -- orchestrator (injectable deps, mirrors run_topology_cycle)
# --------------------------------------------------------------------------- #
def test_run_staleness_cycle_reports_checked_and_updated_counts():
    rows = [
        _row(
            source="storage_reliability", evidence_seen_at=(_NOW - timedelta(hours=13)).isoformat()
        ),
        _row(source="reliability", evidence_seen_at=(_NOW - timedelta(hours=1)).isoformat()),
    ]
    written = []
    result = run_staleness_cycle(
        _THRESHOLD,
        get_rows=lambda: rows,
        write=lambda updates: written.append(updates) or len(updates),
        now=_NOW,
    )
    assert result == {"checked": 2, "updated": 1}
    assert len(written[0]) == 1
    assert written[0][0].source == "storage_reliability"


def test_run_staleness_cycle_skips_write_when_nothing_changed():
    def boom(_updates):
        raise AssertionError("write must not be called with an empty update list")

    result = run_staleness_cycle(_THRESHOLD, get_rows=lambda: [_row()], write=boom, now=_NOW)
    assert result == {"checked": 1, "updated": 0}


def test_run_staleness_cycle_floors_a_misconfigured_zero_threshold():
    """design D5: stale_after_sec=0 (or negative) must not flag every fresh
    domain source in the fleet STALE on the very next cycle."""
    fresh_row = _row(evidence_seen_at=(_NOW - timedelta(seconds=5)).isoformat())
    result = run_staleness_cycle(0, get_rows=lambda: [fresh_row], write=lambda u: len(u), now=_NOW)
    assert result == {"checked": 1, "updated": 0}


# --------------------------------------------------------------------------- #
# db.get_source_trust_rows / db.apply_source_staleness -- integration (real DB)
# --------------------------------------------------------------------------- #
def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-05-30T00:00:00+00:00"}


def _env(device_id: str, msg_type: str, payload: dict, source_health: dict) -> dict:
    return {
        "device_id": device_id,
        "agent_version": "0.1.0",
        "msg_type": msg_type,
        "payload": payload,
        "source_health": source_health,
    }


@pytest.mark.integration
def test_apply_source_staleness_changes_only_state_weight_reason(client):
    from server import db

    sh = {"storage_reliability": _sh("ok"), "reliability": _sh("ok"), "boot_time": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-int", "historical", healthy("historical"), sh))

    with db._connect() as conn:
        before = dict(
            conn.execute(
                "SELECT * FROM device_source_trust WHERE device_id=? AND source=?",
                ("dev-int", "storage_reliability"),
            ).fetchone()
        )

    update = StaleUpdate(
        device_id="dev-int",
        source="storage_reliability",
        state="stale",
        weight=0.0,
        reason="источник молчит 13 ч (порог 12 ч)",
        evidence_seen_at=before["evidence_seen_at"],
    )
    applied = db.apply_source_staleness([update])
    assert applied == 1

    with db._connect() as conn:
        after = dict(
            conn.execute(
                "SELECT * FROM device_source_trust WHERE device_id=? AND source=?",
                ("dev-int", "storage_reliability"),
            ).fetchone()
        )
    assert after["state"] == "stale"
    assert after["weight"] == 0.0
    assert after["reason"] == "источник молчит 13 ч (порог 12 ч)"
    # untouched fields -- the whole point of this write path (P1-4 anti-reset)
    assert after["evidence_seen_at"] == before["evidence_seen_at"]
    assert after["collector_status"] == before["collector_status"]
    assert after["semantic_status"] == before["semantic_status"]
    assert after["ts"] == before["ts"]


@pytest.mark.integration
def test_apply_source_staleness_drops_write_when_evidence_moved(client):
    """Optimistic-concurrency guard: a real ingest between read and write wins
    over the periodic job's stale computation (design D3)."""
    from server import db

    sh = {"storage_reliability": _sh("ok"), "reliability": _sh("ok"), "boot_time": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-race", "historical", healthy("historical"), sh))

    with db._connect() as conn:
        stale_evidence = conn.execute(
            "SELECT evidence_seen_at FROM device_source_trust WHERE device_id=? AND source=?",
            ("dev-race", "storage_reliability"),
        ).fetchone()["evidence_seen_at"]

    # A real re-ingest moves the evidence clock forward before the stale write lands.
    client.post("/api/v1/ingest", json=_env("dev-race", "historical", healthy("historical"), sh))

    update = StaleUpdate(
        device_id="dev-race",
        source="storage_reliability",
        state="stale",
        weight=0.0,
        reason="источник молчит 13 ч (порог 12 ч)",
        evidence_seen_at=stale_evidence,  # the OLD value the job read
    )
    applied = db.apply_source_staleness([update])
    assert applied == 0  # guard drops it -- the row moved out from under it

    with db._connect() as conn:
        row = conn.execute(
            "SELECT state FROM device_source_trust WHERE device_id=? AND source=?",
            ("dev-race", "storage_reliability"),
        ).fetchone()
    assert row["state"] == "ok"  # untouched by the dropped stale write


@pytest.mark.integration
def test_staleness_cycle_end_to_end_then_domain_recovers_on_next_ingest(client):
    """The scenario named in the P2-2 finding: a device keeps sending SOME
    sources, one goes silent. Run the cycle with a far-future 'now' to force
    staleness, then ingest a DIFFERENT source -- the domain must read UNKNOWN
    on the very next ingest (design D6: the job marks rows only, domains
    re-aggregate on the next real ingest, not inside the job itself)."""
    from server import db

    sh = {"storage_reliability": _sh("ok"), "reliability": _sh("ok"), "boot_time": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-e2e", "historical", healthy("historical"), sh))
    assert db.get_trust("dev-e2e")["domains"]["storage"]["state"] == "trusted"

    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    result = run_staleness_cycle(_THRESHOLD, now=far_future)
    assert result["updated"] >= 1

    # Trigger domain re-aggregation via a DIFFERENT source's ingest (heartbeat
    # owns thermal/disk_fill, not storage) -- evaluate_trust reads ALL stored
    # source rows, including the freshly-marked stale one, on every call.
    hb_sh = {"free_space": _sh("ok"), "throttle": _sh("ok"), "disk_latency": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-e2e", "heartbeat", healthy("heartbeat"), hb_sh))

    trust = db.get_trust("dev-e2e")
    assert trust["sources"]["storage_reliability"]["state"] == "stale"
    assert trust["domains"]["storage"]["state"] == "unknown"


@pytest.mark.integration
def test_get_source_trust_rows_is_fleet_wide(client):
    from server import db

    sh = {"storage_reliability": _sh("ok"), "reliability": _sh("ok"), "boot_time": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-a", "historical", healthy("historical"), sh))
    client.post("/api/v1/ingest", json=_env("dev-b", "historical", healthy("historical"), sh))

    rows = db.get_source_trust_rows()
    device_ids = {r["device_id"] for r in rows}
    assert {"dev-a", "dev-b"} <= device_ids
    row = next(
        r for r in rows if r["device_id"] == "dev-a" and r["source"] == "storage_reliability"
    )
    assert row["evidence_seen_at"] is not None
