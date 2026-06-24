"""Phase-2 integration: network snapshots, /netmap API + pages."""

from __future__ import annotations

import json
import re

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
    # Ф3: /api/v1/netmap is a deprecated alias of the unified graph. The page model
    # still renders from build_netmap (cluster shape) until Ф4; the API now returns the
    # superset graph shape with the same fleet facts.
    assert m["totals"]["agents"] == 2
    assert any(s["anomaly"] for s in m["subnets"])

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


# --------------------------------------------------------------------------- #
# Visual map (canvas + embedded JSON island)
# --------------------------------------------------------------------------- #
def _embedded_json(body: str) -> dict:
    m = re.search(r'<script id="netmap-data" type="application/json">(.*?)</script>', body, re.S)
    assert m, "embedded netmap JSON island missing"
    return json.loads(m.group(1))


def test_netmap_page_embeds_canvas_and_data(client):
    _ingest(client, "map-41", _net_payload())
    body = client.get("/netmap").text
    assert 'id="netmap-canvas"' in body
    data = _embedded_json(body)
    assert data["totals"]["agents"] == 1
    cluster = data["clusters"][0]
    assert cluster["gateway"] == "192.168.1.1"
    # device_id must survive the round-trip: canvas click-through builds /device/{id}
    assert cluster["agents"][0]["device_id"] == "map-41"


def test_netmap_hides_arp_only_nodes(client):
    # The agentless ARP neighbour from _net_payload (192.168.1.50) must no longer
    # appear on the map -- not in the SSR body, not in the canvas JSON island
    # (owner 2026-06-22). Gateways and agents still show.
    _ingest(client, "map-61", _net_payload())
    body = client.get("/netmap").text
    assert "192.168.1.50" not in body  # ARP-only IP gone from SSR + JSON island
    assert "без агента" not in body  # the ARP-only stat/legend label is gone
    data = _embedded_json(body)
    assert data["clusters"][0]["others"] == []  # no ARP-only nodes in the model
    assert data["clusters"][0]["gateway"] == "192.168.1.1"  # gateway still present


def test_netmap_page_without_data_has_no_canvas(client):
    body = client.get("/netmap").text
    assert 'id="netmap-canvas"' not in body
    assert "карта появится" in body  # empty state survives


def test_netmap_embedded_json_cannot_break_out_of_script(client):
    """A hostile hostname must not terminate the JSON <script> island (XSS pin)."""
    inv = healthy("inventory")
    inv["hostname"] = "</script><script>alert(1)//"
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": "map-51",
            "agent_version": "0.1.0",
            "msg_type": "inventory",
            "payload": inv,
        },
    )
    assert r.status_code == 200, r.text
    _ingest(client, "map-51", _net_payload())
    body = client.get("/netmap").text
    assert "</script><script>alert(1)" not in body
    data = _embedded_json(body)
    # the value itself survives intact after JSON parsing
    assert data["clusters"][0]["agents"][0]["hostname"] == "</script><script>alert(1)//"
