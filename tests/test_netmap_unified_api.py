"""Ф3: unified network-map API + GraphCache loader→assembler + deprecated aliases.

One contract: ``GET /api/v1/network-map/graph`` returns the unified superset graph
(nodes/links/subnets/totals) assembled by ``build_network_map`` from the backbone
tables + agent snapshots + printers. The cache serves it within a short TTL without
re-querying the DB; a force-poll invalidates it. ``/api/v1/netmap`` and
``/api/v1/topology/graph`` are kept as deprecated aliases that return the SAME graph.
"""

from __future__ import annotations

import pytest
import server.db as db
from fastapi.testclient import TestClient
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
    p["network_neighbors"] = []
    p["network_quality"] = [
        {"target_kind": "gateway", "target": gw, "latency_ms": 1.5, "loss_pct": loss, "samples": 3}
    ]
    return p


def _ingest(client: TestClient, did: str, payload) -> None:
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": did,
            "agent_version": "0.1.0",
            "msg_type": "historical",
            "payload": payload,
            "source_health": {
                "network": {"status": "ok", "collected_at": "2026-06-10T00:00:00+00:00"}
            },
        },
    )
    assert r.status_code == 200, r.text


def _nodes(g: dict) -> set:
    return {n["nid"] for n in g["nodes"]}


def test_network_map_graph_serves_unified_shape(client: TestClient) -> None:
    db.upsert_net_device(
        {"device_nid": "nd-mac-DEADBEEF0001", "dev_type": "router", "ip": "192.168.1.1"}
    )
    _ingest(client, "map-01", _net_payload())
    resp = client.get("/api/v1/network-map/graph")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    g = resp.json()
    # the unified superset keys are all present (the single canvas reads these, Ф4)
    assert {"nodes", "links", "subnets", "totals"} <= set(g)
    assert g["totals"]["agents"] == 1
    # the backbone router + the synthesized agent node both show as nodes
    assert _nodes(g) >= {"nd-mac-DEADBEEF0001", "nd-mac-AA-BB-CC-00-00-01"}


def test_network_map_graph_empty_when_no_fleet(client: TestClient) -> None:
    resp = client.get("/api/v1/network-map/graph")
    assert resp.status_code == 200
    g = resp.json()
    assert g["nodes"] == [] and g["links"] == []
    assert g["totals"]["nodes"] == 0


def test_network_map_graph_cached_within_ttl_does_not_reload(
    client: TestClient, monkeypatch
) -> None:
    # Spy on the DB read layer: every cache reload fans out to get_net_devices, so a
    # second read served from the TTL cache must NOT call it again.
    calls = {"n": 0}
    real = db.get_net_devices

    def spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "get_net_devices", spy)
    client.get("/api/v1/network-map/graph")
    first = calls["n"]
    assert first >= 1
    client.get("/api/v1/network-map/graph")
    assert calls["n"] == first  # no reload inside the TTL window


def test_network_map_graph_reloads_after_ttl_lapses(client: TestClient) -> None:
    # Swap the app cache for a short-TTL one with an injectable clock, so a reload
    # after the window lapses is provable without real sleeping.
    from server.netdisco.cache import GraphCache

    now = {"v": 1000.0}
    client.app.state.network_map_cache = GraphCache(ttl_sec=5.0, clock=lambda: now["v"])
    db.upsert_net_device(
        {"device_nid": "nd-mac-DEADBEEF0002", "dev_type": "router", "ip": "10.0.0.2"}
    )
    g0 = client.get("/api/v1/network-map/graph").json()
    assert "nd-mac-DEADBEEF0002" in _nodes(g0)
    # add a device straight into the DB; inside the TTL it stays hidden
    db.upsert_net_device(
        {"device_nid": "nd-mac-DEADBEEF0003", "dev_type": "switch", "ip": "10.0.0.3"}
    )
    assert "nd-mac-DEADBEEF0003" not in _nodes(client.get("/api/v1/network-map/graph").json())
    # advance past the TTL -> the next read rebuilds and sees the new node
    now["v"] += 6.0
    assert "nd-mac-DEADBEEF0003" in _nodes(client.get("/api/v1/network-map/graph").json())


def test_aliases_return_the_same_unified_graph(client: TestClient) -> None:
    db.upsert_net_device(
        {"device_nid": "nd-mac-DEADBEEF0001", "dev_type": "switch", "ip": "10.0.0.1"}
    )
    _ingest(client, "map-02", _net_payload())
    canonical = client.get("/api/v1/network-map/graph").json()
    legacy_netmap = client.get("/api/v1/netmap").json()
    legacy_topology = client.get("/api/v1/topology/graph").json()
    # all three return the identical unified graph (nodes + links + subnets + totals)
    assert _nodes(canonical) == _nodes(legacy_netmap) == _nodes(legacy_topology)
    assert canonical["links"] == legacy_netmap["links"] == legacy_topology["links"]
    assert canonical["totals"] == legacy_netmap["totals"] == legacy_topology["totals"]
    assert canonical["subnets"] == legacy_netmap["subnets"] == legacy_topology["subnets"]


def test_discovery_poll_invalidates_network_map_cache(client: TestClient) -> None:
    # Prime the cache over an empty backbone, then add a device directly and force a
    # discovery cycle: the cache must drop so the next read shows the new node at once.
    assert client.get("/api/v1/network-map/graph").json()["totals"]["nodes"] == 0
    db.upsert_net_device(
        {"device_nid": "nd-mac-DEADBEEF0099", "dev_type": "switch", "ip": "10.0.0.99"}
    )
    assert client.post("/api/v1/discovery/poll").status_code == 200
    g = client.get("/api/v1/network-map/graph").json()
    assert "nd-mac-DEADBEEF0099" in _nodes(g)


def test_topology_poll_invalidates_network_map_cache(client: TestClient) -> None:
    assert client.get("/api/v1/network-map/graph").json()["totals"]["nodes"] == 0
    db.upsert_net_device(
        {"device_nid": "nd-mac-DEADBEEF0077", "dev_type": "switch", "ip": "10.0.0.77"}
    )
    assert client.post("/api/v1/topology/poll").status_code == 200
    g = client.get("/api/v1/network-map/graph").json()
    assert "nd-mac-DEADBEEF0077" in _nodes(g)


def test_printer_poll_invalidates_network_map_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unified graph includes printers, so the printer force button must also
    # invalidate the shared map cache. We use a direct DB insert as the visible
    # cache-staleness probe and stub the poller to avoid network work.
    assert client.get("/api/v1/network-map/graph").json()["totals"]["nodes"] == 0
    db.upsert_net_device(
        {"device_nid": "nd-mac-DEADBEEF0066", "dev_type": "printer", "ip": "10.0.0.66"}
    )
    import server.api as api

    monkeypatch.setattr(
        api.scheduler,
        "poll_now",
        lambda cfg: {"polled": 0, "online": 0, "unreachable": 0, "errors": 0, "skipped": 0},
    )
    assert client.post("/api/v1/printers/poll").status_code == 200
    g = client.get("/api/v1/network-map/graph").json()
    assert "nd-mac-DEADBEEF0066" in _nodes(g)


def test_network_map_graph_hostile_hostname_round_trips_intact(client: TestClient) -> None:
    # XSS invariant for the API layer: the response is application/json (never parsed
    # as HTML, so a "</script>" substring in the raw JSON body is inert), and a
    # hostile hostname survives as data so the Ф4 canvas can render it through its own
    # tojson/textContent sink without a breakout vector.
    inv = healthy("inventory")
    inv["hostname"] = "</script><script>alert(1)//"
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": "map-xss",
            "agent_version": "0.1.0",
            "msg_type": "inventory",
            "payload": inv,
        },
    )
    assert r.status_code == 200, r.text
    _ingest(client, "map-xss", _net_payload())
    resp = client.get("/api/v1/network-map/graph")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"  # never text/html
    hostnames = [n["hostname"] for n in resp.json()["nodes"]]
    assert "</script><script>alert(1)//" in hostnames  # value preserved intact
