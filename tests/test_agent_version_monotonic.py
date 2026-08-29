"""U2-a: agent_version must not regress from a buffered-replay envelope.

client/transport.py sends the fresh envelope first, then flush_buffer replays
older disk-buffered envelopes oldest-first -- those replays still carry the
PRE-update agent_version. server/db.py::_resolve_agent_version resolves the
version to persist so a replay after a real update can't look like a
rollback on the dashboard. Covers both write paths: upsert_device
(inventory) and touch_device (heartbeat/events/.../update_status).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from server import db

from tests.conftest import envelope

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Direct unit tests of the resolver
# --------------------------------------------------------------------------- #
def test_resolver_keeps_stored_on_replay_with_older_ts() -> None:
    # F6: stored_changed_at must be RECENT (well inside the 1h staleness
    # escape) so this test isolates the envelope_ts logic -- a stale
    # changed_at would let the F6 escape accept the lower incoming version
    # regardless of envelope_ts, which is a different code path (see
    # test_resolver_accepts_lower_version_after_staleness_escape below).
    changed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    resolved = db._resolve_agent_version(
        stored_version="0.3.0",
        stored_changed_at=changed_at.isoformat(),
        incoming_version="0.2.0",
        envelope_ts=(changed_at - timedelta(hours=2)).isoformat(),  # older than version_changed_at
    )
    assert resolved == "0.3.0"


def test_resolver_accepts_genuine_downgrade_with_newer_ts() -> None:
    resolved = db._resolve_agent_version(
        stored_version="0.3.0",
        stored_changed_at="2026-08-01T12:00:00+00:00",
        incoming_version="0.2.0",
        envelope_ts="2026-08-02T00:00:00+00:00",  # newer than version_changed_at
    )
    assert resolved == "0.2.0"


def test_resolver_no_change_on_equal_version() -> None:
    resolved = db._resolve_agent_version(
        stored_version="0.3.0",
        stored_changed_at="2026-08-01T12:00:00+00:00",
        incoming_version="0.3.0",
        envelope_ts="2026-08-01T09:00:00+00:00",
    )
    assert resolved == "0.3.0"


def test_resolver_keeps_stored_on_unparseable_incoming() -> None:
    resolved = db._resolve_agent_version(
        stored_version="0.3.0",
        stored_changed_at="2026-08-01T12:00:00+00:00",
        incoming_version="not-a-version",
        envelope_ts="2026-08-02T00:00:00+00:00",
    )
    assert resolved == "0.3.0"


def test_resolver_falls_back_to_incoming_on_unparseable_when_nothing_stored() -> None:
    """First-ever sighting of a device: nothing to fall back to but the raw
    incoming string -- matches today's unconditional-write behaviour."""
    resolved = db._resolve_agent_version(
        stored_version=None,
        stored_changed_at=None,
        incoming_version="garbage",
    )
    assert resolved == "garbage"


def test_resolver_accepts_incoming_when_stored_unparseable() -> None:
    resolved = db._resolve_agent_version(
        stored_version="not-valid",
        stored_changed_at="2026-08-01T12:00:00+00:00",
        incoming_version="0.2.0",
    )
    assert resolved == "0.2.0"


def test_resolver_no_envelope_ts_treats_lower_incoming_as_replay() -> None:
    """No envelope_ts to prove a genuine downgrade -> keep stored (safer
    default). F6: stored_changed_at kept RECENT (see comment above) so the
    staleness escape doesn't also fire here."""
    resolved = db._resolve_agent_version(
        stored_version="0.3.0",
        stored_changed_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        incoming_version="0.2.0",
        envelope_ts=None,
    )
    assert resolved == "0.3.0"


# --------------------------------------------------------------------------- #
# F6/LOW-1: staleness escape -- version_changed_at older than an hour (server
# clock) accepts a lower incoming version even without a newer envelope_ts.
# --------------------------------------------------------------------------- #
def _freeze_db_now(monkeypatch, frozen: datetime) -> None:
    """Monkeypatch db.py's ``datetime.now`` (used directly by
    _resolve_agent_version, not through an injectable _now_iso hook) to a
    fixed instant. Subclassing (not replacing with a bare fake) keeps
    ``datetime.fromisoformat`` -- used by db._parse_iso -- working normally."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 -- matches datetime.now's own signature
            return frozen

    monkeypatch.setattr(db, "datetime", _FrozenDatetime)


def test_resolver_accepts_lower_version_after_staleness_escape(monkeypatch) -> None:
    """A forged/stale higher version eventually gives way to a real lower one
    once version_changed_at is more than an hour old (server clock) -- a
    buffered replay lands within minutes, not hours later."""
    changed_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_db_now(monkeypatch, changed_at + timedelta(hours=1, minutes=1))

    resolved = db._resolve_agent_version(
        stored_version="9.9.9",  # forged/inflated version
        stored_changed_at=changed_at.isoformat(),
        incoming_version="0.3.0",  # real, lower
        envelope_ts=None,  # no proof via ts -- the staleness escape must carry this alone
    )
    assert resolved == "0.3.0"


def test_resolver_within_the_hour_lower_version_replay_keeps_stored(monkeypatch) -> None:
    """Same forged-then-real scenario, but still within the hour: the
    staleness escape must NOT fire yet -- stored wins (envelope_ts-less
    replay-guard default)."""
    changed_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_db_now(monkeypatch, changed_at + timedelta(minutes=30))

    resolved = db._resolve_agent_version(
        stored_version="9.9.9",
        stored_changed_at=changed_at.isoformat(),
        incoming_version="0.3.0",
        envelope_ts=None,
    )
    assert resolved == "9.9.9"


# --------------------------------------------------------------------------- #
# End-to-end through /api/v1/ingest (client fixture), both write paths
# --------------------------------------------------------------------------- #
def test_heartbeat_replay_after_update_does_not_roll_back_dashboard_version(client) -> None:
    """touch_device path: update 0.2.0 -> 0.3.0, then a replayed 0.2.0 envelope
    with an OLDER ts must not roll the stored version back."""
    device_id = "dev-replay-hb"

    env1 = envelope(device_id, "heartbeat", {"cpu_pct": 5.0})
    env1["agent_version"] = "0.2.0"
    env1["ts"] = "2026-08-01T10:00:00+00:00"
    r1 = client.post("/api/v1/ingest", json=env1)
    assert r1.status_code == 200, r1.text

    env2 = envelope(device_id, "heartbeat", {"cpu_pct": 5.0})
    env2["agent_version"] = "0.3.0"
    env2["ts"] = "2026-08-01T12:00:00+00:00"
    r2 = client.post("/api/v1/ingest", json=env2)
    assert r2.status_code == 200, r2.text

    d = db.get_device(device_id)
    assert d["agent_version"] == "0.3.0"
    changed_after_update = d["version_changed_at"]

    # Buffered replay: same 0.2.0 envelope as env1, sent again (older ts).
    r3 = client.post("/api/v1/ingest", json=env1)
    assert r3.status_code == 200, r3.text

    d = db.get_device(device_id)
    assert d["agent_version"] == "0.3.0"
    assert d["version_changed_at"] == changed_after_update


def test_heartbeat_genuine_downgrade_with_newer_ts_is_accepted(client, monkeypatch) -> None:
    """version_changed_at is stamped with the SERVER receipt time (W0.2), not the
    envelope ts -- so a "genuine downgrade" here means an envelope ts newer than
    that server-side receipt moment, not just newer than the other envelope."""
    from server import pipeline

    def _next_recv() -> str:
        _next_recv.n += 1  # type: ignore[attr-defined]
        base = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
        return (base + timedelta(seconds=_next_recv.n)).isoformat()  # type: ignore[attr-defined]

    _next_recv.n = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(pipeline, "_now_iso", _next_recv)

    device_id = "dev-downgrade-hb"

    env1 = envelope(device_id, "heartbeat", {"cpu_pct": 5.0})
    env1["agent_version"] = "0.3.0"
    env1["ts"] = "2026-08-01T00:00:01+00:00"
    client.post("/api/v1/ingest", json=env1)
    first_changed = db.get_device(device_id)["version_changed_at"]

    # A real reinstall to an older build, reported (client ts) well after the
    # server-side receipt moment recorded above as version_changed_at.
    env2 = envelope(device_id, "heartbeat", {"cpu_pct": 5.0})
    env2["agent_version"] = "0.2.0"
    env2["ts"] = "2027-01-01T00:00:00+00:00"
    r2 = client.post("/api/v1/ingest", json=env2)
    assert r2.status_code == 200, r2.text

    d = db.get_device(device_id)
    assert d["agent_version"] == "0.2.0"
    assert d["version_changed_at"] != first_changed


def test_inventory_replay_after_update_does_not_roll_back_dashboard_version(client) -> None:
    """upsert_device path (inventory)."""
    device_id = "dev-replay-inv"

    env1 = envelope(device_id, "inventory", {"hostname": "H1"})
    env1["agent_version"] = "0.2.0"
    env1["ts"] = "2026-08-01T10:00:00+00:00"
    client.post("/api/v1/ingest", json=env1)

    env2 = envelope(device_id, "inventory", {"hostname": "H1"})
    env2["agent_version"] = "0.3.0"
    env2["ts"] = "2026-08-01T12:00:00+00:00"
    client.post("/api/v1/ingest", json=env2)

    d = db.get_device(device_id)
    assert d["agent_version"] == "0.3.0"
    changed_after_update = d["version_changed_at"]

    # Buffered replay of the pre-update inventory envelope.
    client.post("/api/v1/ingest", json=env1)

    d = db.get_device(device_id)
    assert d["agent_version"] == "0.3.0"
    assert d["version_changed_at"] == changed_after_update
