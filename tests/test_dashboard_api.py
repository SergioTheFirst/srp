"""W1.3a — enriched fleet API (alerts/staleness/cert) + device acknowledgements."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-05-31T00:00:00+00:00"}


def _env(device_id: str, msg_type: str, payload: dict, source_health: dict | None = None) -> dict:
    env = dict(envelope(device_id, msg_type, payload))
    if source_health is not None:
        env["source_health"] = source_health
    return env


def _row(client, device_id: str) -> dict:
    return next(d for d in client.get("/api/v1/devices").json() if d["device_id"] == device_id)


def test_fleet_row_has_alert_and_staleness_fields(client):
    hb_ok = {"free_space": _sh("ok"), "throttle": _sh("ok"), "disk_latency": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dA", "heartbeat", healthy("heartbeat"), hb_ok))
    hb_bad = {"free_space": _sh("blocked"), "throttle": _sh("blocked"), "disk_latency": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dA", "heartbeat", healthy("heartbeat"), hb_bad))
    row = _row(client, "dA")
    assert row["unknown_domains"] >= 1
    assert row["regressed_count"] >= 1
    assert "device_trust" in row
    assert row["stale"] is False
    assert row["last_seen_age_sec"] is not None


def test_stale_threshold_two_missed_liveness_pings() -> None:
    """«Offline» = два пропущенных liveness-пинга (2x300 с), а не часы молчания.

    Пинит дашборд-порог к liveness-каденсу агента: возраст в один пинг (+джиттер)
    ещё живой; старше порога -- offline. Fleet flag (db.get_devices) и страница
    устройства (dashboard) применяют одинаковое age>threshold сравнение.
    """
    from datetime import datetime, timedelta, timezone

    from server import db

    now = datetime.now(timezone.utc)
    one_ping = db.age_seconds((now - timedelta(seconds=360)).isoformat())
    dead = db.age_seconds((now - timedelta(seconds=db.STALE_AFTER_SEC + 300)).isoformat())
    assert one_ping is not None and dead is not None
    assert not (one_ping > db.STALE_AFTER_SEC)  # один пинг с джиттером -- ещё живой
    assert dead > db.STALE_AFTER_SEC  # 2+ пропущенных пинга -> offline


def test_fleet_row_flags_expiring_cert(client):
    soon = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    payload = {
        **healthy("historical"),
        "certificates": [
            {"subject": "CN=soon", "not_after": soon},
            {"subject": "CN=far", "not_after": far},
        ],
    }
    client.post("/api/v1/ingest", json=_env("dB", "historical", payload))
    row = _row(client, "dB")
    assert row["cert_expiring"] is True
    assert row["cert_min_days"] is not None and row["cert_min_days"] <= 11


def test_fleet_row_no_certs_not_expiring(client):
    client.post("/api/v1/ingest", json=_env("dC", "historical", healthy("historical")))
    row = _row(client, "dC")
    assert row["cert_expiring"] is False
    assert row["cert_min_days"] is None


def test_fleet_row_uses_personal_user_certificate(client):
    # A personal cert spooled by the tray (user_certificates) with NO machine cert
    # must still surface its expiry in the fleet "Сертификат" column, matching the
    # personal-cert block shown on the device card.
    soon = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    payload = {
        **healthy("historical"),
        "user_certificates": [
            {"subject": "CN=user", "owner": "CORP\\\\user", "not_after": soon},
        ],
    }
    client.post("/api/v1/ingest", json=_env("dE", "historical", payload))
    row = _row(client, "dE")
    assert row["cert_min_days"] is not None and row["cert_min_days"] <= 11
    assert row["cert_expiring"] is True


def test_fleet_row_cert_takes_soonest_of_machine_and_personal(client):
    # When both machine and personal certs exist, the column shows the soonest
    # expiry (early-warning: never hide the nearer risk behind the farther one).
    machine_far = (datetime.now(timezone.utc) + timedelta(days=300)).isoformat()
    personal_soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    payload = {
        **healthy("historical"),
        "certificates": [{"subject": "CN=machine", "not_after": machine_far}],
        "user_certificates": [
            {"subject": "CN=user", "owner": "CORP\\\\user", "not_after": personal_soon},
        ],
    }
    client.post("/api/v1/ingest", json=_env("dF", "historical", payload))
    row = _row(client, "dF")
    assert row["cert_min_days"] is not None and row["cert_min_days"] <= 6
    assert row["cert_expiring"] is True


def test_ack_endpoint_persists_and_appears(client):
    client.post("/api/v1/ingest", json=_env("dD", "inventory", healthy("inventory")))
    resp = client.post("/api/v1/devices/dD/ack", json={"note": "investigating"})
    assert resp.status_code == 200
    assert _row(client, "dD")["ack"]["note"] == "investigating"
    assert client.get("/api/v1/devices/dD").json()["ack"]["note"] == "investigating"


def test_ack_unknown_device_returns_404(client):
    assert client.post("/api/v1/devices/nope/ack", json={"note": "x"}).status_code == 404


def test_ack_is_none_when_not_acknowledged(client):
    client.post("/api/v1/ingest", json=_env("dE", "inventory", healthy("inventory")))
    assert _row(client, "dE")["ack"] is None
