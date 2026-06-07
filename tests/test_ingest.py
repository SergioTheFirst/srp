"""Integration tests: the REST ingest pipeline + query API + dashboard render.

These drive the whole server through FastAPI's TestClient against a throwaway
SQLite DB (the ``client`` / ``seeded_client`` fixtures), mirroring what a real
agent does over the wire.
"""

from __future__ import annotations

import pytest
from server import pipeline
from tests.conftest import (
    DEGRADING_DEVICE,
    HEALTHY_DEVICE,
    degrading,
    envelope,
    healthy,
)

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Health + ingest happy path
# --------------------------------------------------------------------------- #
def test_health_ok(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}


@pytest.mark.parametrize("msg_type", ["inventory", "historical", "heartbeat", "events"])
def test_ingest_each_message_type_accepted(client, msg_type):
    resp = client.post(
        "/api/v1/ingest", json=envelope(DEGRADING_DEVICE, msg_type, degrading(msg_type))
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == DEGRADING_DEVICE
    assert body["msg_type"] == msg_type
    assert isinstance(body["scores_updated"], bool)


@pytest.mark.parametrize("msg_type", ["inventory", "historical", "heartbeat"])
def test_scoring_messages_update_scores(client, msg_type):
    """Inventory / historical / heartbeat each independently produce day-1 scores."""
    resp = client.post(
        "/api/v1/ingest", json=envelope(DEGRADING_DEVICE, msg_type, degrading(msg_type))
    )
    body = resp.json()
    assert body["scores_updated"] is True
    assert body["scores"] is not None


def test_events_alone_are_stored_without_scoring(client):
    """Events feed the log view, not the day-1 scores -- a device that has only
    sent events is registered and its events stored, but it has no scores yet."""
    resp = client.post(
        "/api/v1/ingest", json=envelope("events-only", "events", degrading("events"))
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scores_updated"] is False
    dev = client.get("/api/v1/devices/events-only").json()
    assert dev["scores"] is None
    assert len(dev["events"]) == 2


def test_events_on_scored_device_do_not_rescore(client, monkeypatch):
    """W4.0: events never feed scoring (recompute reads only inv/hist/hb), so an
    events message must not trigger a rescore -- even on a device that already has
    scores. We store the events and skip the (now O(n^2) trend) recompute."""
    # Give the device real scores first.
    client.post(
        "/api/v1/ingest", json=envelope("scored-evt", "historical", degrading("historical"))
    )

    real = pipeline.recompute_scores
    calls = {"n": 0}

    def _spy(device_id: str):
        calls["n"] += 1
        return real(device_id)

    monkeypatch.setattr(pipeline, "recompute_scores", _spy)

    resp = client.post("/api/v1/ingest", json=envelope("scored-evt", "events", degrading("events")))
    assert resp.status_code == 200, resp.text
    assert calls["n"] == 0  # events must not rescore
    assert resp.json()["scores_updated"] is False
    # the scores computed from the earlier historical message are still served.
    assert client.get("/api/v1/devices/scored-evt").json()["scores"] is not None


def test_ingest_creates_device_from_heartbeat_alone(client):
    client.post("/api/v1/ingest", json=envelope("hb-only", "heartbeat", healthy("heartbeat")))
    devices = client.get("/api/v1/devices").json()
    assert any(d["device_id"] == "hb-only" for d in devices)


def test_diagnostics_exposes_storage_risk(client):
    """W4.2: the storage engine runs in the pipeline and its verdict is surfaced
    through the diagnostics endpoint (a failing drive reads as high storage risk)."""
    payload = {
        "storage": [{"disk": "PhysicalDisk0", "media_type": "HDD", "reallocated_sectors": 200}]
    }
    client.post("/api/v1/ingest", json=envelope("stor-dev", "historical", payload))
    diag = client.get("/api/v1/diagnostics/stor-dev").json()
    assert diag["storage_risk"] is not None
    assert diag["storage_risk"]["value"] >= 60


def test_ingest_accepts_unknown_payload_field(client):
    """Forward compatibility survives the HTTP boundary, not just the model."""
    payload = healthy("heartbeat")
    payload["future_metric"] = 123
    resp = client.post("/api/v1/ingest", json=envelope("fwd-compat", "heartbeat", payload))
    assert resp.status_code == 200, resp.text


def test_ingest_rejects_unknown_msg_type(client):
    bad = {"device_id": "x", "agent_version": "0.1.0", "msg_type": "bogus", "payload": {}}
    resp = client.post("/api/v1/ingest", json=bad)
    assert resp.status_code == 422  # pydantic Literal on Envelope.msg_type


# --------------------------------------------------------------------------- #
# Query API
# --------------------------------------------------------------------------- #
def test_device_list_orders_riskiest_first(seeded_client):
    devices = seeded_client.get("/api/v1/devices").json()
    assert len(devices) == 2
    # COALESCE(risk_exposure,0) DESC -> the degrading laptop comes first.
    assert devices[0]["device_id"] == DEGRADING_DEVICE
    assert devices[0]["top_risk"]["name"] == "power_thermal"


def test_device_detail_has_full_scores(seeded_client):
    dev = seeded_client.get(f"/api/v1/devices/{DEGRADING_DEVICE}").json()
    scores = dev["scores"]
    for key in ("performance", "reliability", "wear", "risk_exposure"):
        assert scores[key] is not None
    assert scores["risk"]["top"] == "power_thermal"
    assert dev["hostname"] == "DEGRADE-LT-01"
    assert dev["inventory"]["chassis"] == "laptop"
    assert len(dev["events"]) == 2


def test_healthy_device_detail_low_risk(seeded_client):
    dev = seeded_client.get(f"/api/v1/devices/{HEALTHY_DEVICE}").json()
    assert dev["scores"]["risk_exposure"] == 0.0
    assert dev["scores"]["risk"]["overall"] < 0.10


def test_unknown_device_returns_404(client):
    assert client.get("/api/v1/devices/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Dashboard render (server-side Jinja2)
# --------------------------------------------------------------------------- #
def test_fleet_page_renders(seeded_client):
    resp = seeded_client.get("/")
    assert resp.status_code == 200
    assert "DEGRADE-LT-01" in resp.text
    assert "HEALTHY-DT-01" in resp.text


def test_device_detail_page_renders(seeded_client):
    resp = seeded_client.get(f"/device/{DEGRADING_DEVICE}")
    assert resp.status_code == 200
    assert "DEGRADE-LT-01" in resp.text


def test_latest_message_wins(client):
    """Re-ingesting historical with worse data must refresh the stored scores."""
    client.post("/api/v1/ingest", json=envelope("evolve", "historical", healthy("historical")))
    before = client.get("/api/v1/devices/evolve").json()["scores"]["reliability"]

    worse = healthy("historical")
    worse["bugchecks_30d"] = 3
    worse["reliability_stability_index"] = 2.0
    client.post("/api/v1/ingest", json=envelope("evolve", "historical", worse))
    after = client.get("/api/v1/devices/evolve").json()["scores"]["reliability"]

    assert after < before
