"""Update-status конверт: контракт, ingest (touch_device + set_update_status),
no-trust/no-rescore, version_changed_at tracking, миграция старых БД devices."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from server import db, pipeline
from shared.schema import Envelope, parse_payload
from tests.conftest import envelope

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
def test_contract_accepts_update_status_msg_type() -> None:
    env = Envelope(device_id="dev-1", msg_type="update_status", payload={"state": "ok"})
    assert env.msg_type == "update_status"
    parsed = parse_payload("update_status", {"state": "ok"})
    assert parsed.state == "ok"  # type: ignore[attr-defined]


def test_update_status_rejects_bad_state() -> None:
    with pytest.raises(ValueError):
        parse_payload("update_status", {"state": "bogus"})


def test_update_status_error_over_500_chars_rejected() -> None:
    with pytest.raises(ValueError):
        parse_payload("update_status", {"state": "failed", "error": "x" * 501})


def test_update_status_error_at_500_chars_accepted() -> None:
    parsed = parse_payload("update_status", {"state": "failed", "error": "x" * 500})
    assert parsed.error == "x" * 500  # type: ignore[attr-defined]


def test_update_status_available_version_max_length_32() -> None:
    with pytest.raises(ValueError):
        parse_payload("update_status", {"state": "ok", "available_version": "x" * 33})


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
def test_ingest_update_status_accepted_and_stores_fields_without_scoring(client) -> None:
    r = client.post(
        "/api/v1/ingest",
        json=envelope(
            "dev-upd",
            "update_status",
            {"state": "ok", "checked_at": "2026-07-03T10:00:00+00:00"},
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["scores_updated"] is False
    d = db.get_device("dev-upd")
    assert d is not None
    assert d["update_state"] == "ok"
    assert d["update_error"] is None
    assert d["update_checked_at"] == "2026-07-03T10:00:00+00:00"


def test_ingest_update_status_stores_error_on_failed(client) -> None:
    r = client.post(
        "/api/v1/ingest",
        json=envelope(
            "dev-upd-fail",
            "update_status",
            {"state": "failed", "error": "не удалось скачать пакет"},
        ),
    )
    assert r.status_code == 200, r.text
    d = db.get_device("dev-upd-fail")
    assert d["update_state"] == "failed"
    assert d["update_error"] == "не удалось скачать пакет"


def test_ingest_update_status_creates_device_with_last_seen(client) -> None:
    r = client.post(
        "/api/v1/ingest", json=envelope("dev-upd-new", "update_status", {"state": "updating"})
    )
    assert r.status_code == 200
    d = db.get_device("dev-upd-new")
    assert d is not None and d["last_seen"]
    assert d["latest_heartbeat"] is None  # no telemetry rows written


def test_update_status_envelope_cannot_smuggle_fake_trust_reading(client) -> None:
    """Same class of finding as the liveness B2 regression: a forged update_status
    envelope with a non-empty source_health must never reach evaluate_trust."""
    payload = {
        "device_id": "dev-forge-upd",
        "msg_type": "update_status",
        "payload": {"state": "ok", "free_space_pct": 97.5},
        "source_health": {"free_space": {"status": "ok"}},
    }
    r = client.post("/api/v1/ingest", json=payload)
    assert r.status_code == 200
    assert db.get_source_trusts("dev-forge-upd") == {}


# --------------------------------------------------------------------------- #
# version_changed_at (upsert_device / touch_device)
# --------------------------------------------------------------------------- #
def test_version_changed_at_set_on_first_sighting(client) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-ver1", "heartbeat", {"cpu_pct": 5.0}))
    d = db.get_device("dev-ver1")
    assert d["version_changed_at"] is not None


def _install_increasing_clock(monkeypatch) -> None:
    """Replace pipeline._now_iso with a strictly-increasing ISO clock.

    A hardcoded 2-value iterator is not enough: scoring msg_types (heartbeat,
    inventory) call _now_iso() a second time inside recompute_scores/store_scores,
    so one ingest can consume more than one tick. This clock never runs out and
    every call is guaranteed distinct, so tests only need to assert relations
    (first != second), not exact tick counts.
    """
    base = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
    state = {"n": 0}

    def _next() -> str:
        state["n"] += 1
        return (base + timedelta(seconds=state["n"])).isoformat()

    monkeypatch.setattr(pipeline, "_now_iso", _next)


def test_version_changed_at_unchanged_when_version_same(client) -> None:
    env = envelope("dev-ver2", "heartbeat", {"cpu_pct": 5.0})
    client.post("/api/v1/ingest", json=env)
    first = db.get_device("dev-ver2")["version_changed_at"]

    client.post("/api/v1/ingest", json=env)  # identical agent_version "0.1.0"
    second = db.get_device("dev-ver2")["version_changed_at"]
    assert second == first


def test_version_changed_at_updates_and_agent_version_refreshes_on_new_version(
    client, monkeypatch
) -> None:
    """T1 deliberate behavior change: touch_device (heartbeat/events/print_jobs/
    liveness/update_status path) now refreshes agent_version on ON CONFLICT too,
    not only on the rare inventory (upsert_device) cadence."""
    _install_increasing_clock(monkeypatch)

    env1 = envelope("dev-ver3", "heartbeat", {"cpu_pct": 5.0})
    client.post("/api/v1/ingest", json=env1)
    first = db.get_device("dev-ver3")["version_changed_at"]
    assert first is not None

    env2 = envelope("dev-ver3", "heartbeat", {"cpu_pct": 5.0})
    env2["agent_version"] = "9.9.9"
    client.post("/api/v1/ingest", json=env2)
    d = db.get_device("dev-ver3")
    assert d["agent_version"] == "9.9.9"
    assert d["version_changed_at"] != first


def test_update_status_new_version_refreshes_agent_version_and_version_changed_at(
    client, monkeypatch
) -> None:
    """Same behavior via the update_status path specifically (touch_device call
    in the update_status pipeline branch)."""
    _install_increasing_clock(monkeypatch)

    env1 = envelope("dev-ver4", "heartbeat", {"cpu_pct": 5.0})
    client.post("/api/v1/ingest", json=env1)
    first = db.get_device("dev-ver4")["version_changed_at"]

    env2 = envelope("dev-ver4", "update_status", {"state": "ok"})
    env2["agent_version"] = "9.9.9"
    client.post("/api/v1/ingest", json=env2)

    d = db.get_device("dev-ver4")
    assert d["agent_version"] == "9.9.9"
    assert d["version_changed_at"] != first


def test_upsert_device_inventory_path_also_refreshes_version_changed_at(
    client, monkeypatch
) -> None:
    """upsert_device (inventory path) carries the same CASE logic as touch_device."""
    _install_increasing_clock(monkeypatch)

    env1 = envelope("dev-ver5", "inventory", {"hostname": "H1"})
    client.post("/api/v1/ingest", json=env1)
    first = db.get_device("dev-ver5")["version_changed_at"]
    assert first is not None

    env2 = envelope("dev-ver5", "inventory", {"hostname": "H1"})
    env2["agent_version"] = "9.9.9"
    client.post("/api/v1/ingest", json=env2)
    d = db.get_device("dev-ver5")
    assert d["agent_version"] == "9.9.9"
    assert d["version_changed_at"] != first


# --------------------------------------------------------------------------- #
# Migration: pre-T1 devices table gains the 4 new columns idempotently
# --------------------------------------------------------------------------- #
def _columns(path: Path, table: str) -> set:
    con = sqlite3.connect(str(path))
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_legacy_devices_table_migrates_update_status_columns(tmp_path: Path) -> None:
    p = tmp_path / "legacy.db"
    db.init_db(p)  # fresh DB: already has the new columns

    # Emulate a pre-T1 devices table by rebuilding it without the 4 new columns
    # (SQLite can't DROP COLUMN portably on old engines -- rebuild instead, same
    # technique as tests/test_netdisco_db_p7.py).
    con = sqlite3.connect(str(p))
    con.executescript(
        """
        CREATE TABLE legacy_devices AS SELECT
          device_id, hostname, manufacturer, model, chassis, agent_version,
          first_seen, last_seen, site_code, site_name, org_code, dept_code,
          comment, last_reported_ts, clock_drift_sec, department
          FROM devices;
        DROP TABLE devices;
        CREATE TABLE devices (
          device_id       TEXT PRIMARY KEY,
          hostname        TEXT,
          manufacturer    TEXT,
          model           TEXT,
          chassis         TEXT,
          agent_version   TEXT,
          first_seen      TEXT,
          last_seen       TEXT,
          site_code       TEXT,
          site_name       TEXT,
          org_code        TEXT,
          dept_code       TEXT,
          comment         TEXT,
          last_reported_ts TEXT,
          clock_drift_sec REAL,
          department      TEXT
        );
        INSERT INTO devices SELECT * FROM legacy_devices;
        DROP TABLE legacy_devices;
        """
    )
    con.commit()
    con.close()

    before = _columns(p, "devices")
    for col in ("update_state", "update_error", "update_checked_at", "version_changed_at"):
        assert col not in before

    db.init_db(p)  # re-init must migrate the legacy DB idempotently

    after = _columns(p, "devices")
    for col in ("update_state", "update_error", "update_checked_at", "version_changed_at"):
        assert col in after

    # idempotent: a second re-init on an already-migrated DB must not blow up
    db.init_db(p)
    assert _columns(p, "devices") == after
