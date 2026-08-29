"""Phase 3: read-only netdisco inventory API.

Uses the TestClient fixture (fresh tmp DB per test); devices are seeded directly
through db.upsert_net_device, which writes to the same DB the app reads.
"""

from __future__ import annotations

import threading
import time

import server.db as db
from fastapi.testclient import TestClient


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses (o5-B2: the topology-poll
    cycle now runs on a worker thread with no join/callback hook exposed to callers,
    so tests that need to observe its effect poll for it instead of asserting on the
    very next call)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


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


def test_network_map_graph_returns_etag_and_304(client: TestClient) -> None:
    # o5-B8: a dashboard polling more often than the graph changes should not pay for
    # re-serializing the whole graph on every request -- a weak ETag derived from the
    # cache's loaded_at lets the client short-circuit via If-None-Match.
    from server.netdisco.cache import _DEFAULT_TTL_SEC

    resp1 = client.get("/api/v1/network-map/graph")
    assert resp1.status_code == 200
    etag = resp1.headers.get("etag")
    assert etag
    assert resp1.headers.get("cache-control") == f"private, max-age={int(_DEFAULT_TTL_SEC)}"

    resp2 = client.get("/api/v1/network-map/graph", headers={"If-None-Match": etag})
    assert resp2.status_code == 304
    assert resp2.content == b""


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
    # o5-B2: the "собрать топологию сейчас" button now hands the reconcile cycle to a
    # worker thread and answers "started" at once -- it no longer waits for (or
    # echoes) the cycle's own result shape.
    resp = client.post("/api/v1/topology/poll")
    assert resp.status_code == 200
    assert resp.json() == {"started": 1}


def test_poll_topology_returns_immediately(client: TestClient, monkeypatch) -> None:
    """o5-B2 (d)3: the handler must not block the HTTP response on the reconcile
    cycle -- with the cycle mocked to block on an Event, the response has to come
    back well before the (bounded, 2s) block would ever release on its own."""
    import server.api as api

    release = threading.Event()

    def blocking_cycle(cfg):  # noqa: ARG001 -- signature must match run_topology_cycle
        release.wait(timeout=2)  # bounded: an unfixed (still-blocking) handler fails
        return {"links": 0, "probed": 0, "busy": 0}

    monkeypatch.setattr(api.netdisco_reconcile, "run_topology_cycle", blocking_cycle)
    started = time.monotonic()
    resp = client.post("/api/v1/topology/poll")
    elapsed = time.monotonic() - started
    release.set()  # let the background thread finish so it doesn't outlive the test
    assert resp.status_code == 200
    assert resp.json() == {"started": 1}
    assert elapsed < 1.0  # returned well before the 2s-bounded mock cycle finishes


def test_topology_poll_invalidates_graph_cache(client: TestClient) -> None:
    # Prime the read-through cache over the (empty) backbone, then add a device straight
    # into the DB. Without invalidation the cache would keep serving empty within its
    # TTL; the force button must clear it so the new node shows in the graph at once.
    # o5-B2: invalidation now happens on the same worker thread as the cycle, after the
    # handler has already answered -- poll for it instead of asserting on the very
    # next read.
    assert client.get("/api/v1/topology/graph").json()["nodes"] == []
    db.upsert_net_device({"device_nid": "nd-mac-sw1", "dev_type": "switch", "ip": "10.0.0.2"})
    assert client.post("/api/v1/topology/poll").status_code == 200

    def _node_present() -> bool:
        nodes = {n["nid"] for n in client.get("/api/v1/topology/graph").json()["nodes"]}
        return "nd-mac-sw1" in nodes

    assert _wait_until(_node_present)


def test_topology_poll_returns_busy_when_a_cycle_is_running(tmp_path, monkeypatch) -> None:
    from server.config import ServerConfig
    from server.main import create_app
    from server.netdisco import reconcile

    # netdisco config enabled so the cycle is not gated off; netdisco_enabled left
    # False so no background loop competes for the lock; empty inventory -> no SNMP.
    app = create_app(ServerConfig(db_path=str(tmp_path / "t.db"), netdisco={"enabled": True}))
    with TestClient(app) as c:
        reconcile._poll_lock.acquire()  # simulate a cycle already in flight
        # o5-B2: the endpoint no longer waits on the cycle, so it always answers
        # "started" right away regardless of the lock. The anti-DoS lock still blocks
        # the actual work; that now happens on the worker thread instead of inline,
        # so spy on the real call and poll for its own (still busy=1) result.
        seen = []
        real_run = reconcile.run_topology_cycle

        def spy(cfg):
            result = real_run(cfg)
            seen.append(result)
            return result

        monkeypatch.setattr(reconcile, "run_topology_cycle", spy)
        try:
            resp = c.post("/api/v1/topology/poll")
            assert resp.status_code == 200
            assert resp.json() == {"started": 1}
            assert _wait_until(lambda: len(seen) == 1)
            assert seen[0]["busy"] == 1  # anti-DoS: no second concurrent pass
        finally:
            reconcile._poll_lock.release()


def test_topology_poll_is_rate_limited_after_a_burst(client: TestClient) -> None:
    # Unauthenticated force button that can trigger SNMP probes -> must be throttled.
    assert client.post("/api/v1/topology/poll").status_code == 200  # within budget
    statuses = {client.post("/api/v1/topology/poll").status_code for _ in range(40)}
    assert 429 in statuses  # the flood is throttled
