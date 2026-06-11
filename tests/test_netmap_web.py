"""Phase-2 integration: network snapshots, /netmap API + pages."""

from __future__ import annotations

import pytest
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _net_payload(ip="192.168.1.10", mac="AA-BB-CC-00-00-01", gw="192.168.1.1", loss=0.0):
    p = healthy("historical")
    p["network_adapters"] = [
        {
            "name": "Ethernet",
            "kind": "ethernet",
            "mac": mac,
            "up": True,
            "ipv4": [ip],
            "gateway": gw,
        }
    ]
    p["network_neighbors"] = [
        {"ip": "192.168.1.50", "mac": "00-50-56-00-00-09", "state": "Reachable"}
    ]
    p["network_quality"] = [
        {"target_kind": "gateway", "target": gw, "latency_ms": 1.5, "loss_pct": loss, "samples": 3}
    ]
    return p


def _ingest(client, did, payload):
    env = {
        "device_id": did,
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": payload,
        "source_health": {"network": {"status": "ok", "collected_at": "2026-06-10T00:00:00+00:00"}},
    }
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 200, r.text


def test_get_network_snapshots_skips_networkless(client):
    from server import db

    _ingest(client, "map-01", _net_payload())
    _ingest(client, "map-02", healthy("historical"))  # no network fields
    snaps = db.get_network_snapshots()
    assert [s["device_id"] for s in snaps] == ["map-01"]
    s = snaps[0]
    assert s["adapters"][0]["gateway"] == "192.168.1.1"
    assert s["neighbors"] and s["quality"]


def test_netmap_api_and_page(client):
    _ingest(client, "map-11", _net_payload(loss=30.0))
    _ingest(client, "map-12", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0))
    api = client.get("/api/v1/netmap")
    assert api.status_code == 200
    m = api.json()
    assert m["totals"]["clusters"] == 1 and m["totals"]["agents"] == 2
    assert m["clusters"][0]["anomaly"] is True

    page = client.get("/netmap")
    assert page.status_code == 200
    body = page.text
    assert "Карта сети" in body and "map-11" in body and "инфраструктур" in body


def test_device_page_shows_axis_and_subnet_note(client):
    _ingest(client, "map-21", _net_payload(loss=30.0))
    _ingest(client, "map-22", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0))
    page = client.get("/device/map-21")
    assert page.status_code == 200
    body = page.text
    assert "Здоровье сети" in body  # axis card
    assert "инфраструктур" in body  # subnet annotation (D8)
    assert "Качество связи" in body  # probes table


def test_diagnostics_exposes_network_risk(client):
    _ingest(client, "map-31", _net_payload())
    d = client.get("/api/v1/diagnostics/map-31")
    assert d.status_code == 200
    assert d.json()["network_risk"]["value"] is not None
