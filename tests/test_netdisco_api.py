"""Phase 3: read-only netdisco inventory API.

Uses the TestClient fixture (fresh tmp DB per test); devices are seeded directly
through db.upsert_net_device, which writes to the same DB the app reads.
"""

from __future__ import annotations

import server.db as db
from fastapi.testclient import TestClient


def test_netdisco_devices_endpoint_lists_inventory(client: TestClient) -> None:
    db.upsert_net_device({"device_nid": "nd-mac-AA", "dev_type": "switch", "ip": "10.0.0.1"})
    db.upsert_net_device({"device_nid": "nd-mac-BB", "dev_type": "endpoint", "ip": "10.0.0.2"})
    resp = client.get("/api/v1/netdisco/devices")
    assert resp.status_code == 200
    assert {d["device_nid"] for d in resp.json()["devices"]} == {"nd-mac-AA", "nd-mac-BB"}


def test_netdisco_devices_endpoint_filters_by_type(client: TestClient) -> None:
    db.upsert_net_device({"device_nid": "nd-mac-AA", "dev_type": "switch", "ip": "10.0.0.1"})
    db.upsert_net_device({"device_nid": "nd-mac-BB", "dev_type": "endpoint", "ip": "10.0.0.2"})
    resp = client.get("/api/v1/netdisco/devices?dev_type=switch")
    assert resp.status_code == 200
    assert [d["device_nid"] for d in resp.json()["devices"]] == ["nd-mac-AA"]


def test_netdisco_devices_endpoint_filters_by_site(client: TestClient) -> None:
    db.upsert_net_device({"device_nid": "nd-mac-AA", "dev_type": "switch", "site_code": "HQ"})
    db.upsert_net_device({"device_nid": "nd-mac-BB", "dev_type": "switch", "site_code": "BR"})
    resp = client.get("/api/v1/netdisco/devices?site=HQ")
    assert resp.status_code == 200
    assert [d["device_nid"] for d in resp.json()["devices"]] == ["nd-mac-AA"]


def test_netdisco_devices_endpoint_empty_when_no_inventory(client: TestClient) -> None:
    resp = client.get("/api/v1/netdisco/devices")
    assert resp.status_code == 200
    assert resp.json()["devices"] == []


def test_topology_graph_endpoint_serves_unified_graph(client: TestClient) -> None:
    db.upsert_net_device({"device_nid": "nd-mac-AA", "dev_type": "switch", "ip": "10.0.0.1"})
    resp = client.get("/api/v1/topology/graph")
    assert resp.status_code == 200
    g = resp.json()  # Ф3: deprecated alias of /network-map/graph -> unified graph
    assert "nd-mac-AA" in {n["nid"] for n in g["nodes"]}
    # the legacy shape is gone: no {graph:{...}} wrapper, no received_at
    assert "graph" not in g and "received_at" not in g


def test_topology_graph_endpoint_empty_when_no_inventory(client: TestClient) -> None:
    resp = client.get("/api/v1/topology/graph")
    assert resp.status_code == 200
    g = resp.json()
    assert g["nodes"] == [] and g["links"] == []


def test_topology_changes_endpoint_returns_journal_and_clamps_days(client: TestClient) -> None:
    db.store_net_change("appeared", "nd-x", {"k": "v"})
    resp = client.get("/api/v1/topology/changes?days=5")
    assert resp.status_code == 200
    assert any(c["kind"] == "appeared" for c in resp.json()["changes"])
    assert client.get("/api/v1/topology/changes?days=999999").status_code == 200  # clamped, not 500


def test_netdisco_device_detail_surfaces_status_and_404s(client: TestClient) -> None:
    db.upsert_net_device(
        {"device_nid": "nd-mac-AA", "dev_type": "switch", "ip": "10.0.0.1", "status": "unreachable"}
    )
    resp = client.get("/api/v1/netdisco/devices/nd-mac-AA")
    assert resp.status_code == 200
    body = resp.json()["device"]
    assert body["status"] == "unreachable"  # reachability annotation visible on the device
    assert "interfaces" in body and "links" in body
    assert client.get("/api/v1/netdisco/devices/nd-mac-NOPE").status_code == 404


def test_netdisco_stats_endpoint_returns_counter_dict(client: TestClient) -> None:
    resp = client.get("/api/v1/netdisco/stats")
    assert resp.status_code == 200
    assert isinstance(resp.json()["stats"], dict)


def test_discovery_poll_runs_a_cycle(client: TestClient) -> None:
    resp = client.post("/api/v1/discovery/poll")
    assert resp.status_code == 200
    body = resp.json()
    assert body["busy"] == 0
    assert body["persisted"] >= 0  # empty fleet -> 0 devices, still a clean cycle


def test_discovery_poll_returns_busy_when_a_cycle_is_running(client: TestClient) -> None:
    from server.netdisco import scheduler

    scheduler._poll_lock.acquire()  # simulate a cycle already in flight
    try:
        resp = client.post("/api/v1/discovery/poll")
        assert resp.status_code == 200
        assert resp.json()["busy"] == 1  # anti-DoS: no second concurrent pass
    finally:
        scheduler._poll_lock.release()


def test_discovery_poll_is_rate_limited_after_a_burst(client: TestClient) -> None:
    # P4 carry-forward: the force button is unauthenticated, so it must be rate-
    # limited (before P5's active scan can ever sit behind its lock).
    assert client.post("/api/v1/discovery/poll").status_code == 200  # within budget
    statuses = {client.post("/api/v1/discovery/poll").status_code for _ in range(40)}
    assert 429 in statuses  # the flood is throttled


def test_topology_poll_runs_a_cycle(client: TestClient) -> None:
    # The "собрать топологию сейчас" button forces one reconcile now. With netdisco
    # gated off (test default) the cycle is a clean no-op, still a well-formed reply.
    resp = client.post("/api/v1/topology/poll")
    assert resp.status_code == 200
    body = resp.json()
    assert body["busy"] == 0
    assert "links" in body and body["links"] >= 0


def test_topology_poll_invalidates_graph_cache(client: TestClient) -> None:
    # Prime the read-through cache over the (empty) backbone, then add a device straight
    # into the DB. Without invalidation the cache would keep serving empty within its
    # TTL; the force button must clear it so the new node shows in the graph at once.
    assert client.get("/api/v1/topology/graph").json()["nodes"] == []
    db.upsert_net_device({"device_nid": "nd-mac-sw1", "dev_type": "switch", "ip": "10.0.0.2"})
    assert client.post("/api/v1/topology/poll").status_code == 200
    assert "nd-mac-sw1" in {n["nid"] for n in client.get("/api/v1/topology/graph").json()["nodes"]}


def test_topology_poll_returns_busy_when_a_cycle_is_running(tmp_path) -> None:
    from server.config import ServerConfig
    from server.main import create_app
    from server.netdisco import reconcile

    # netdisco config enabled so the cycle is not gated off; netdisco_enabled left
    # False so no background loop competes for the lock; empty inventory -> no SNMP.
    app = create_app(ServerConfig(db_path=str(tmp_path / "t.db"), netdisco={"enabled": True}))
    with TestClient(app) as c:
        reconcile._poll_lock.acquire()  # simulate a cycle already in flight
        try:
            resp = c.post("/api/v1/topology/poll")
            assert resp.status_code == 200
            assert resp.json()["busy"] == 1  # anti-DoS: no second concurrent pass
        finally:
            reconcile._poll_lock.release()


def test_topology_poll_is_rate_limited_after_a_burst(client: TestClient) -> None:
    # Unauthenticated force button that can trigger SNMP probes -> must be throttled.
    assert client.post("/api/v1/topology/poll").status_code == 200  # within budget
    statuses = {client.post("/api/v1/topology/poll").status_code for _ in range(40)}
    assert 429 in statuses  # the flood is throttled
