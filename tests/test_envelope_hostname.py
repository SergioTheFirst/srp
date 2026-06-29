"""Device hostname rides on every envelope so the dashboard shows the real
machine name even right after ``srp.db`` is wiped.

Root cause pinned here: ``touch_device`` (the heartbeat/historical/events/
print_jobs path) used to ignore hostname, so a freshly-recreated device row sat
at ``hostname=NULL`` until the once-a-day inventory arrived -- the dashboard
showed the raw ``dev-<hex>`` id meanwhile. Now every envelope carries the live
hostname and ``touch_device`` persists it (COALESCE-preserve, like the
site/org fields), so the first contact of *any* type restores the real name.
"""

from __future__ import annotations

import json

import pytest
from client.config import ClientConfig, load_config
from client.transport import Transport
from shared.schema import Envelope

pytestmark = pytest.mark.unit


# ── contract: Envelope.hostname is an additive-optional field ─────────────── #
def test_envelope_declares_optional_hostname_field() -> None:
    assert "hostname" in Envelope.model_fields


def test_envelope_without_hostname_is_valid_and_none() -> None:
    # Old agents omit hostname -> None, never rejected (additive-optional).
    env = Envelope(device_id="d", msg_type="heartbeat")
    assert env.hostname is None


def test_envelope_round_trips_hostname() -> None:
    env = Envelope(device_id="d", msg_type="heartbeat", hostname="DESK-01")
    assert env.hostname == "DESK-01"


# ── client: live hostname resolved on every load + sent on every envelope ──── #
def test_load_config_sets_live_hostname(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("client.config._hostname", lambda: "LIVE-PC")
    cfg = load_config(tmp_path / "config.json")
    assert cfg.hostname == "LIVE-PC"


def test_load_config_refreshes_stale_persisted_hostname(tmp_path, monkeypatch) -> None:
    # A renamed machine must surface its CURRENT name, not a name cached on disk.
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"device_id": "d", "hostname": "OLD-NAME"}), encoding="utf-8")
    monkeypatch.setattr("client.config._hostname", lambda: "RENAMED-PC")
    cfg = load_config(path)
    assert cfg.hostname == "RENAMED-PC"


def _transport(tmp_path, hostname: str) -> Transport:
    cfg = ClientConfig(
        server_url="http://127.0.0.1:9/",  # nominal; network is not used here
        device_id="t-dev",
        buffer_path=str(tmp_path / "buffer.jsonl"),
        hostname=hostname,
    )
    return Transport(cfg)


def test_envelope_carries_hostname(tmp_path) -> None:
    env = _transport(tmp_path, "DESK-01")._envelope("heartbeat", {"cpu_pct": 1.0})
    assert env["hostname"] == "DESK-01"


def test_envelope_sends_none_when_hostname_empty(tmp_path) -> None:
    # Empty -> None so the server's COALESCE keeps any previously-stored name.
    env = _transport(tmp_path, "")._envelope("heartbeat", {"cpu_pct": 1.0})
    assert env["hostname"] is None


# ── server: touch_device persists hostname (COALESCE-preserve) ────────────── #
@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def test_touch_device_writes_hostname(db_init) -> None:
    db_init.touch_device("dev-1", "2026-01-01T00:00:00Z", "0.1.0", hostname="HB-PC")
    assert db_init.get_device("dev-1")["hostname"] == "HB-PC"


def test_touch_device_none_hostname_preserves_existing(db_init) -> None:
    db_init.touch_device("dev-1", "2026-01-01T00:00:00Z", "0.1.0", hostname="FIRST")
    db_init.touch_device("dev-1", "2026-01-02T00:00:00Z", "0.1.0", hostname=None)
    assert db_init.get_device("dev-1")["hostname"] == "FIRST"


def test_touch_device_new_hostname_updates(db_init) -> None:
    db_init.touch_device("dev-1", "2026-01-01T00:00:00Z", "0.1.0", hostname="FIRST")
    db_init.touch_device("dev-1", "2026-01-02T00:00:00Z", "0.1.0", hostname="SECOND")
    assert db_init.get_device("dev-1")["hostname"] == "SECOND"


# ── integration: a heartbeat alone (post-wipe) restores the real name ─────── #
@pytest.mark.integration
def test_heartbeat_after_wipe_restores_hostname(client) -> None:
    from server import db
    from tests.conftest import healthy

    env = {
        "device_id": "pm-wipe",
        "agent_version": "0.1.0",
        "msg_type": "heartbeat",
        "payload": healthy("heartbeat"),
        "hostname": "RECOVERED-PC",
    }
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 200, r.text
    assert db.get_device("pm-wipe")["hostname"] == "RECOVERED-PC"
