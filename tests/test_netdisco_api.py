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
