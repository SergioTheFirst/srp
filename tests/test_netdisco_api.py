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


def test_netdisco_devices_endpoint_empty_when_no_inventory(client: TestClient) -> None:
    resp = client.get("/api/v1/netdisco/devices")
    assert resp.status_code == 200
    assert resp.json()["devices"] == []
