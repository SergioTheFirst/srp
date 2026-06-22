"""Phase-12 integration: /topology page (SSR inventory + canvas graph island)
and the /netdisco/device/{nid} card. Netdisco data is seeded directly via db
writers (it normally arrives from SNMP scans, not ingest)."""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.integration


def _seed_device(nid, ip, hostname, dev_type="switch", status="up"):
    from server import db

    db.upsert_net_device(
        {
            "device_nid": nid,
            "ip": ip,
            "hostname": hostname,
            "mac": "AA-BB-CC-00-00-01",
            "vendor": "Cisco",
            "dev_type": dev_type,
            "status": status,
        }
    )


def _seed_graph(nodes, links, received_at="2026-06-22T10:00:00+00:00"):
    from server import db

    db.store_topology_snapshot({"nodes": nodes, "links": links}, received_at=received_at)


def _island(body: str) -> dict:
    m = re.search(r'<script id="topo-data" type="application/json">(.*?)</script>', body, re.S)
    assert m, "embedded topology JSON island missing"
    return json.loads(m.group(1))


def test_topology_page_renders_inventory(client):
    _seed_device("nd-router", "192.168.1.1", "core-gw", dev_type="router")
    _seed_device("nd-sw1", "192.168.1.2", "floor-switch", dev_type="switch")
    body = client.get("/topology").text
    assert "Топология" in body
    # SSR inventory table is usable without JS
    assert "core-gw" in body and "floor-switch" in body
    assert "192.168.1.1" in body
    # RU type labels render
    assert "маршрутизатор" in body and "коммутатор" in body


def test_topology_embeds_graph_island(client):
    _seed_device("nd-a", "192.168.1.1", "gw-a", dev_type="router")
    _seed_device("nd-b", "192.168.1.2", "sw-b", dev_type="switch")
    _seed_graph(
        nodes=[
            {"nid": "nd-a", "dev_type": "router", "ip": "192.168.1.1", "hostname": "gw-a"},
            {"nid": "nd-b", "dev_type": "switch", "ip": "192.168.1.2", "hostname": "sw-b"},
        ],
        links=[
            {
                "a": "nd-a",
                "b": "nd-b",
                "via_source": "lldp",
                "confidence": "high",
                "link_kind": "ethernet",
                "ambiguous": False,
            }
        ],
    )
    body = client.get("/topology").text
    assert 'id="topo-canvas"' in body
    data = _island(body)
    assert {n["nid"] for n in data["nodes"]} == {"nd-a", "nd-b"}
    link = data["links"][0]
    assert link["a"] == "nd-a" and link["b"] == "nd-b"
    assert link["confidence"] == "high" and link["link_kind"] == "ethernet"


def test_topology_empty_state_has_no_canvas(client):
    body = client.get("/topology").text
    assert 'id="topo-canvas"' not in body  # no canvas element when graph empty
    assert "ещё не собран" in body  # empty-state survives


def test_topology_island_cannot_break_out_of_script(client):
    """A hostile SNMP-supplied hostname must not terminate the JSON island (XSS pin)."""
    hostile = "</script><script>alert(1)//"
    _seed_device("nd-x", "192.168.1.9", hostile, dev_type="switch")
    _seed_graph(
        nodes=[{"nid": "nd-x", "dev_type": "switch", "ip": "192.168.1.9", "hostname": hostile}],
        links=[],
    )
    body = client.get("/topology").text
    assert "</script><script>alert(1)" not in body
    # positive proof the SSR table autoescaped the hostname (not just absence)
    assert "&lt;/script&gt;" in body
    data = _island(body)
    assert data["nodes"][0]["hostname"] == hostile  # value survives intact


def test_net_device_card_shows_interfaces_and_links(client):
    from server import db

    _seed_device("nd-sw", "192.168.1.2", "floor-switch", dev_type="switch")
    _seed_device("nd-gw", "192.168.1.1", "core-gw", dev_type="router")
    db.store_net_interfaces(
        "nd-sw",
        [{"if_index": 1, "name": "GigabitEthernet0/1", "if_type": 6, "oper_up": 1}],
    )
    db.upsert_net_link(
        {
            "a_nid": "nd-gw",
            "b_nid": "nd-sw",
            "link_kind": "ethernet",
            "via_source": "lldp",
            "confidence": "high",
        }
    )
    db.store_net_change("device_new", device_nid="nd-sw", detail={"dev_type": "switch"})
    body = client.get("/netdisco/device/nd-sw").text
    assert "floor-switch" in body
    assert "GigabitEthernet0/1" in body  # interface row
    assert "nd-gw" in body or "core-gw" in body  # incident link
    assert "device_new" in body or "появилось" in body  # change journal


def test_net_device_card_404(client):
    assert client.get("/netdisco/device/nope").status_code == 404
