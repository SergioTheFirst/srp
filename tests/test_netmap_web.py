"""Ф4: unified network-map page + canvas engine (netmap-unification).

``/netmap`` now serves the ONE unified superset graph (Ф2 assembler via the Ф3
GraphCache) -- nodes/links/subnets/totals -- through the ``_netgraph.html`` canvas
engine. The old ephemeral cluster model (``build_netmap`` clusters) is retired.
"""

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


def _embedded_graph(body: str) -> dict:
    m = re.search(r'<script id="netgraph-data" type="application/json">(.*?)</script>', body, re.S)
    assert m, "embedded netgraph JSON island missing"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# API + page contract (Ф3 unified graph served to Ф4 canvas)
# --------------------------------------------------------------------------- #
def test_netmap_api_and_page(client):
    _ingest(client, "map-11", _net_payload(loss=30.0))
    _ingest(client, "map-12", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0))
    api = client.get("/api/v1/netmap")
    assert api.status_code == 200
    m = api.json()
    # Ф3: /api/v1/netmap is a deprecated alias of the unified graph.
    assert m["totals"]["agents"] == 2
    assert any(s["anomaly"] for s in m["subnets"])

    page = client.get("/netmap")
    assert page.status_code == 200
    body = page.text
    assert "Карта сети" in body and "map-11" in body


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
# Ф4: unified canvas engine (one graph, all types, wireless, quality, XSS)
# --------------------------------------------------------------------------- #
def test_netmap_page_embeds_canvas_and_unified_graph(client):
    _ingest(client, "map-41", _net_payload())
    body = client.get("/netmap").text
    assert 'id="netgraph-canvas"' in body
    g = _embedded_graph(body)
    # unified shape, not the retired cluster model
    assert set(g) == {"nodes", "links", "subnets", "totals"}
    assert g["totals"]["nodes"] >= 2  # agent + gateway
    types = {n["dev_type"] for n in g["nodes"]}
    assert "agent" in types and "router" in types
    # device_id survives the round-trip -> canvas click-through builds /device/{id}
    agent = next(n for n in g["nodes"] if n["device_id"] == "map-41")
    assert agent["card_url"] == "/device/map-41"


def test_netmap_inventory_table_renders_without_js(client):
    """SSR inventory table is available without JS: one row per graph node, each
    linking to its canonical card (card_url)."""
    _ingest(client, "map-42", _net_payload())
    body = client.get("/netmap").text
    assert 'class="net-inv"' in body  # the SSR inventory table
    assert "Инвентарь сети" in body
    # the agent row links to its canonical card, not a generic netdisco route
    assert 'href="/device/map-42"' in body


def test_netmap_has_poll_button_and_changelog(client):
    """Ф4 keeps the 'собрать сейчас' button + changelog from the topology page."""
    _ingest(client, "map-43", _net_payload())
    body = client.get("/netmap").text
    assert "Собрать карту сейчас" in body
    assert 'id="net-poll-btn"' in body
    assert "Журнал изменений" in body


def test_netmap_anomaly_overlay_lands_on_gateway(client):
    """The subnet anomaly overlay is computed by the Ф2 assembler and surfaced to the
    canvas: the degraded subnet is flagged and the gateway carries the overlay."""
    _ingest(client, "map-51", _net_payload(loss=30.0))
    _ingest(client, "map-52", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0))
    body = client.get("/netmap").text
    g = _embedded_graph(body)
    anom = [s for s in g["subnets"] if s["anomaly"]]
    assert anom, "degraded subnet must be flagged"
    # the gateway node exists and sits in that subnet
    gw = next(n for n in g["nodes"] if n["dev_type"] == "router")
    assert gw["subnet"] == anom[0]["subnet_hint"]
    assert g["totals"]["anomalies"] >= 1


def test_netmap_wireless_uplink_marked(client):
    """A Wi-Fi agent uplink is tagged medium=wireless (Ф2 heuristic) and reaches the
    canvas engine so it renders dashed."""
    p = _net_payload()
    p["network_adapters"][0]["kind"] = "wifi"
    _ingest(client, "map-61", p)
    body = client.get("/netmap").text
    g = _embedded_graph(body)
    up = next(edge for edge in g["links"] if edge["link_kind"] == "agent-uplink")
    assert up["medium"] == "wireless"


def test_netmap_hides_arp_only_nodes(client):
    # The agentless ARP neighbour from _net_payload (192.168.1.50) must not appear on
    # the map -- not in the SSR body, not in the canvas JSON island. Gateways and
    # agents still show (owner 2026-06-22; Ф4 carries the invariant forward).
    _ingest(client, "map-71", _net_payload())
    body = client.get("/netmap").text
    assert "192.168.1.50" not in body  # ARP-only IP gone from SSR + JSON island
    assert "без агента" not in body  # the ARP-only stat/legend label is gone
    g = _embedded_graph(body)
    assert not any(n.get("ip") == "192.168.1.50" for n in g["nodes"])
    assert g["nodes"]  # gateway + agent still present


def test_netmap_page_without_data_has_no_canvas(client):
    body = client.get("/netmap").text
    assert 'id="netgraph-canvas"' not in body
    assert "карта появится" in body  # empty state survives


def test_netmap_embedded_json_cannot_break_out_of_script(client):
    """A hostile hostname must not terminate the JSON <script> island (XSS pin)."""
    inv = healthy("inventory")
    inv["hostname"] = "</script><script>alert(1)//"
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": "map-81",
            "agent_version": "0.1.0",
            "msg_type": "inventory",
            "payload": inv,
        },
    )
    assert r.status_code == 200, r.text
    _ingest(client, "map-81", _net_payload())
    body = client.get("/netmap").text
    assert "</script><script>alert(1)" not in body
    g = _embedded_graph(body)
    # the value itself survives intact after JSON parsing
    agent = next(n for n in g["nodes"] if n["device_id"] == "map-81")
    assert agent["hostname"] == "</script><script>alert(1)//"


def test_netmap_page_inert_against_event_handler_payload(client):
    """An event-handler payload (img onerror / svg onload / javascript: href) must
    reach the page inert: autoescape neutralises the SSR table, the canvas engine
    never injects innerHTML, and click-through uses only the assembler card_url --
    so the raw payload string cannot execute. Hardens the XSS-pin against regressions
    beyond the </script> JSON-island breakout."""
    inv = healthy("inventory")
    inv["hostname"] = "<img src=x onerror=alert(1)><svg onload=alert(2)>"
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": "map-82",
            "agent_version": "0.1.0",
            "msg_type": "inventory",
            "payload": inv,
        },
    )
    assert r.status_code == 200, r.text
    _ingest(client, "map-82", _net_payload())
    body = client.get("/netmap").text
    # No live, executable HTML tag reaches the DOM: the SSR table escapes ``<`` to
    # ``&lt;`` (so the tag is inert text), and the canvas engine never injects
    # innerHTML. The inert ``onerror=alert`` substring survives only inside the JSON
    # data island (where it is a string value, not parsed as HTML) -- so we assert
    # the *executable* vector (an un-escaped tag) is absent, not the bare substring.
    assert "<img" not in body  # no un-escaped img tag anywhere
    assert "<svg onload" not in body
    assert "<script>alert" not in body
    assert "javascript:" not in body
    # the escaped form IS present (autoescape turned < into &lt;) -- proves it passed
    # through the SSR table safely rather than being stripped silently
    assert "&lt;img" in body


def test_netmap_renders_all_glyph_types(client):
    """Every glyph type the engine draws must render: router/switch/ap/agent/printer/
    server/phone/endpoint. We feed the assembler a mixed fleet so all dev_types show
    in the unified graph."""
    from server import db

    # net_devices carry every dev_type the engine must glyph.
    devices = [
        {"device_nid": "nd-router-1", "dev_type": "router", "ip": "10.0.0.1", "status": "up"},
        {"device_nid": "nd-switch-1", "dev_type": "switch", "ip": "10.0.0.2", "status": "up"},
        {"device_nid": "nd-ap-1", "dev_type": "ap", "ip": "10.0.0.3", "status": "up"},
        {"device_nid": "nd-srv-1", "dev_type": "server", "ip": "10.0.0.4", "status": "up"},
        {"device_nid": "nd-ph-1", "dev_type": "phone", "ip": "10.0.0.5", "status": "up"},
        {"device_nid": "nd-ep-1", "dev_type": "endpoint", "ip": "10.0.0.6", "status": "up"},
        {"device_nid": "nd-prn-1", "dev_type": "unknown", "ip": "10.0.0.7", "status": "up"},
    ]
    for d in devices:
        db.upsert_net_device(d)
    # a linked printer node (printer_id -> printer glyph) and an agent node. The
    # identity FK is bound by set_net_device_links (as the inventory cycle does).
    db.set_net_device_links("nd-prn-1", printer_id="prn-1")
    _ingest(client, "map-91", _net_payload(ip="10.0.0.9"))

    # invalidate the cache so the page rebuilds over the just-stored backbone
    cache = getattr(client.app.state, "network_map_cache", None)
    if cache is not None:
        cache.invalidate()

    body = client.get("/netmap").text
    g = _embedded_graph(body)
    types = {n["dev_type"] for n in g["nodes"]}
    for t in ("router", "switch", "ap", "agent", "printer", "server", "phone", "endpoint"):
        assert t in types, f"missing glyph type on the map: {t}"
