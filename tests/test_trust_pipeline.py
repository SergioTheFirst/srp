"""Integration: trust evaluation on ingest (server/pipeline.evaluate_trust).

Drives the real ingest path through TestClient against a throwaway DB, posting
envelopes that carry `source_health`, then asserting the per-domain trust the
server stored. Pins: required-source failure -> domain UNKNOWN; a source name the
server does not know (a retired source a stale agent still sends, or a forged
name) -> silently skipped, no trust row, no exception; and accumulation of trust
across message types.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.conftest import healthy

pytestmark = pytest.mark.integration


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


def test_failed_inventory_keeps_the_last_good_inventory(client):
    """2026-10-14: a failed inventory (the agent sends payload {} with an
    identity timeout) overwrote the stored inventory with {} -- the device card
    went blank and scoring lost its disks until the next good run. The stored
    inventory must survive, while the identity row still records the failure."""
    from server import db

    good = healthy("inventory")
    resp = client.post(
        "/api/v1/ingest", json=_env("dinv", "inventory", good, {"identity": _sh("ok")})
    )
    assert resp.status_code == 200, resp.text
    failed = _env("dinv", "inventory", {}, {"identity": _sh("timeout")})
    resp = client.post("/api/v1/ingest", json=failed)
    assert resp.status_code == 200, resp.text

    stored = db.get_inventory("dinv")
    assert stored is not None
    assert stored["model"] == good["model"]
    assert stored["hostname"] == good["hostname"]
    identity = db.get_source_trusts("dinv")["identity"]
    assert (identity["state"], identity["collector_status"]) == ("unavailable", "timeout")


def test_heartbeat_throttle_timeout_makes_thermal_unknown(client):
    from server import db

    sh = {"free_space": _sh("ok"), "throttle": _sh("timeout"), "disk_latency": _sh("ok")}
    resp = client.post("/api/v1/ingest", json=_env("dev-hb", "heartbeat", healthy("heartbeat"), sh))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("dev-hb")
    assert trust is not None
    assert trust["domains"]["disk_fill"]["state"] == "trusted"
    assert trust["domains"]["thermal"]["state"] == "unknown"  # throttle required + timeout
    # lineage records the failing source's derived state
    assert trust["sources"]["throttle"]["state"] == "unavailable"


def test_storage_blocked_makes_storage_unknown(client):
    from server import db

    sh = {
        "storage_reliability": _sh("blocked"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("dev-st", "historical", healthy("historical"), sh))
    trust = db.get_trust("dev-st")
    assert trust["domains"]["storage"]["state"] == "unknown"
    assert trust["sources"]["storage_reliability"]["state"] == "unavailable"


def test_smart_optional_source_contributes_to_storage_domain(client):
    from server import db

    payload = healthy("historical")
    payload["storage"][0]["serial_hash"] = "diskhash1"
    payload["storage"][0]["nvme_media_errors"] = 0
    sh = {
        "storage_reliability": _sh("ok"),
        "smart": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    resp = client.post("/api/v1/ingest", json=_env("dev-smart", "historical", payload, sh))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("dev-smart")
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert "smart" in trust["domains"]["storage"]["contributing"]
    assert trust["sources"]["smart"]["state"] == "ok"


def test_smart_blocked_optional_source_is_dropped_not_fatal(client):
    from server import db

    sh = {
        "storage_reliability": _sh("ok"),
        "smart": _sh("blocked"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    resp = client.post(
        "/api/v1/ingest", json=_env("dev-smart2", "historical", healthy("historical"), sh)
    )
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("dev-smart2")
    # required source alone still trusts the domain; optional smart is only dropped
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert "smart" in trust["domains"]["storage"]["dropped"]


# 0.3.4: removable disks (USB/1394) never feed the storage verdict -- the gate included.
_USB = {"disk": "Kingston DataTraveler 3.0", "bus_type": "USB", "serial_hash": "usb01"}
_NVME = {"disk": "Samsung SSD 980", "bus_type": "NVMe", "serial_hash": "nvme01", "wear_pct": 1.0}
_SH_STORAGE = {"storage_reliability": _sh("ok"), "reliability": _sh("ok"), "boot_time": _sh("ok")}


def test_trust_gate_validates_the_first_fixed_disk_not_a_usb_stick(client):
    """The agent does not sort Get-PhysicalDisk: an implausible USB stick listed first
    failed the gate and hid the internal disk's whole storage axis."""
    from server import db
    from server.pipeline import _extract_reading

    payload = {**healthy("historical"), "storage": [{**_USB, "wear_pct": 300.0}, _NVME]}
    assert _extract_reading("storage_reliability", payload) == _NVME
    assert _extract_reading("smart", payload)["serial_hash"] == "nvme01"
    sh = {**_SH_STORAGE, "smart": _sh("ok")}
    resp = client.post("/api/v1/ingest", json=_env("dev-usbgate", "historical", payload, sh))
    assert resp.status_code == 200, resp.text
    assert db.get_trust("dev-usbgate")["domains"]["storage"]["state"] == "trusted"


def test_usb_only_storage_reads_empty_never_a_confident_zero(client):
    from server import db
    from server.pipeline import _extract_reading

    payload = {**healthy("historical"), "storage": [{**_USB, "wear_pct": 90.0}]}
    assert _extract_reading("storage_reliability", payload) == {}
    assert _extract_reading("smart", payload)["serial_hash"] is None
    client.post("/api/v1/ingest", json=_env("dev-usbonly", "historical", payload, _SH_STORAGE))
    trust = db.get_trust("dev-usbonly")
    assert trust["sources"]["storage_reliability"]["collector_status"] == "empty"
    assert trust["domains"]["storage"]["state"] == "unknown"


def test_trust_accumulates_across_message_types(client):
    from server import db

    client.post(
        "/api/v1/ingest",
        json=_env(
            "dev-acc",
            "heartbeat",
            healthy("heartbeat"),
            {"free_space": _sh("ok"), "throttle": _sh("ok"), "disk_latency": _sh("ok")},
        ),
    )
    client.post(
        "/api/v1/ingest",
        json=_env(
            "dev-acc",
            "historical",
            healthy("historical"),
            {
                "storage_reliability": _sh("ok"),
                "reliability": _sh("ok"),
                "boot_time": _sh("ok"),
            },
        ),
    )
    trust = db.get_trust("dev-acc")
    # heartbeat-owned domains survive the later historical ingest (accumulation, not overwrite)
    assert trust["domains"]["disk_fill"]["state"] == "trusted"
    assert trust["domains"]["thermal"]["state"] == "trusted"
    # historical-owned domains present too
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert trust["domains"]["os_stability"]["state"] == "trusted"
    assert trust["domains"]["boot"]["state"] == "trusted"


def test_unknown_source_and_payload_key_are_ignored_not_fatal(client):
    """A stale/legacy agent may still report a source_health key (or a payload
    field) this server no longer knows -- e.g. a retired source, or a forged
    name. Ingest must accept the envelope (200), silently drop the unknown
    source (no trust row, no exception), and keep processing every known
    source exactly as if the unknown one were never there.
    """
    from server import db

    payload = healthy("historical")
    payload["some_retired_field_z"] = {"whatever": 1}  # extra="allow" tolerance
    sh = {
        "storage_reliability": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
        "legacy_source_x": _sh("ok"),  # unknown to this server
    }
    resp = client.post("/api/v1/ingest", json=_env("dev-legacy", "historical", payload, sh))
    assert resp.status_code == 200, resp.text

    trust = db.get_trust("dev-legacy")
    assert "legacy_source_x" not in trust["sources"]  # no trust row for the unknown source
    # known sources in the same envelope are unaffected
    assert trust["sources"]["storage_reliability"]["state"] == "ok"
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert trust["domains"]["os_stability"]["state"] == "trusted"
    assert trust["domains"]["boot"]["state"] == "trusted"


# --------------------------------------------------------------------------- #
# P2-2 Ch1: evidence_seen_at -- the server-stamped clock the staleness re-eval
# job (Ch2/Ch3) will compute source age from, never the client-controlled ts.
# --------------------------------------------------------------------------- #
def test_evidence_seen_at_is_server_stamped_not_client_ts(client):
    from server import db

    sh = {"storage_reliability": _sh("ok"), "reliability": _sh("ok"), "boot_time": _sh("ok")}
    env = _env("dev-evid", "historical", healthy("historical"), sh)
    # A client ts far in the past: if evidence_seen_at ever mirrored ts instead
    # of the server clock, it would be trivially forgeable / stale-forever.
    env["ts"] = "2000-01-01T00:00:00+00:00"
    resp = client.post("/api/v1/ingest", json=env)
    assert resp.status_code == 200, resp.text

    with db._connect() as conn:
        row = conn.execute(
            "SELECT ts, evidence_seen_at FROM device_source_trust WHERE device_id=? AND source=?",
            ("dev-evid", "storage_reliability"),
        ).fetchone()
    assert row["ts"] == "2000-01-01T00:00:00+00:00"  # client ts preserved as-is elsewhere
    assert row["evidence_seen_at"] is not None
    assert row["evidence_seen_at"] != row["ts"]  # server-stamped, not the client's clock
    assert not row["evidence_seen_at"].startswith("2000-01-01")


def test_evidence_seen_at_advances_on_a_real_re_ingest(client):
    """A genuine second envelope for the same source moves evidence_seen_at
    forward -- contrast with the periodic staleness job (Ch2/Ch3), which must
    NEVER be able to do this (the P1-4-style reset trap)."""
    from server import db

    sh = {"storage_reliability": _sh("ok"), "reliability": _sh("ok"), "boot_time": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-evid2", "historical", healthy("historical"), sh))
    with db._connect() as conn:
        first = conn.execute(
            "SELECT evidence_seen_at FROM device_source_trust WHERE device_id=? AND source=?",
            ("dev-evid2", "storage_reliability"),
        ).fetchone()["evidence_seen_at"]

    client.post("/api/v1/ingest", json=_env("dev-evid2", "historical", healthy("historical"), sh))
    with db._connect() as conn:
        second = conn.execute(
            "SELECT evidence_seen_at FROM device_source_trust WHERE device_id=? AND source=?",
            ("dev-evid2", "storage_reliability"),
        ).fetchone()["evidence_seen_at"]

    assert second >= first  # a real re-ingest is allowed to advance the clock


def test_legacy_db_migrates_evidence_seen_at_column(tmp_path: Path) -> None:
    from server import db

    p = tmp_path / "srp.db"
    db.init_db(p)
    con = sqlite3.connect(str(p))
    # Simulate a pre-P2-2 DB: rebuild device_source_trust without the new column
    # (same technique as test_netdisco_db_p7.py's Ф7-column migration tests).
    con.executescript(
        """
        CREATE TABLE legacy_dst AS SELECT
          device_id, source, state, weight, collector_status, semantic_status, reason, ts
          FROM device_source_trust;
        DROP TABLE device_source_trust;
        CREATE TABLE device_source_trust (
          device_id TEXT, source TEXT, state TEXT, weight REAL,
          collector_status TEXT, semantic_status TEXT, reason TEXT, ts TEXT,
          PRIMARY KEY (device_id, source));
        INSERT INTO device_source_trust SELECT * FROM legacy_dst;
        DROP TABLE legacy_dst;
        INSERT INTO device_source_trust
          (device_id, source, state, weight, collector_status, semantic_status, reason, ts)
        VALUES ('dev-legacy-row', 'storage_reliability', 'ok', 1.0, 'ok', 'plausible', '',
                '2020-01-01T00:00:00+00:00');
        """
    )
    con.commit()
    con.close()
    cols = {r[1] for r in sqlite3.connect(str(p)).execute("PRAGMA table_info(device_source_trust)")}
    assert "evidence_seen_at" not in cols

    db.init_db(p)  # re-init migrates the legacy DB

    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(device_source_trust)")}
    assert "evidence_seen_at" in cols
    row = con.execute(
        "SELECT evidence_seen_at FROM device_source_trust WHERE device_id=?",
        ("dev-legacy-row",),
    ).fetchone()
    assert row["evidence_seen_at"] == "2020-01-01T00:00:00+00:00"  # backfilled from legacy ts


def test_adding_another_column_does_not_restamp_evidence(client, monkeypatch) -> None:
    """Ревью 2026-10-24: бэкфилл evidence_seen_at бежит, только когда добавлена ИМЕННО
    эта колонка. Новая колонка device_source_trust не должна штамповать строку, которая
    никогда не была OK (partial), иначе staleness мигает её в STALE."""
    from server import db

    db.upsert_source_trust(
        "dev-bf2",
        "reliability",
        "degraded",
        0.5,
        "partial",
        "plausible",
        "",
        "2026-10-01T00:00:00+00:00",
        None,  # partial -- не доказательство: evidence_seen_at не ставится
    )
    with db._connect() as conn:
        row = conn.execute(
            "SELECT evidence_seen_at FROM device_source_trust WHERE device_id='dev-bf2'"
        ).fetchone()
        assert row["evidence_seen_at"] is None
    extra = {**db._ADD_COLUMNS, "device_source_trust": (*db._ADD_COLUMNS["device_source_trust"], ("zz_probe", "TEXT"))}
    monkeypatch.setattr(db, "_ADD_COLUMNS", extra)
    db.init_db(db._db_path)
    with db._connect() as conn:
        row = conn.execute(
            "SELECT evidence_seen_at FROM device_source_trust WHERE device_id='dev-bf2'"
        ).fetchone()
    assert row["evidence_seen_at"] is None
