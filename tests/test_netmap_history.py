"""Ф5: time machine -- topology-snapshot readers + the ``?at=<id>`` historical frame.

The unified graph is live (ICMP quality / subnet anomaly are derived per request, so
they were never persisted). The time machine reads the snapshot rows the topology
cycle already appends (retain 500): a list for the slider, and one frame by id,
normalised into the unified shape with empty overlays. A historical frame must never
poison the live GraphCache (it bypasses it), and a bogus/missing id fails closed.
"""

from __future__ import annotations

import pytest
import server.db as db
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _store_snapshot(nodes: int = 3, links: int = 2) -> int:
    """Append a synthetic snapshot row and return its id (mirrors reconcile._graph).

    Needs an initialised db -- callers depend on the ``client`` fixture (lifespan
    runs ``init_db``)."""
    graph = {
        "nodes": [{"nid": f"nd-{i}", "dev_type": "router", "ip": f"10.0.0.{i}"} for i in range(nodes)],
        "links": [
            {"a": "nd-0", "b": f"nd-{i}", "via_source": "lldp", "confidence": "high"}
            for i in range(1, links + 1)
        ],
    }
    db.store_topology_snapshot(graph, received_at="2026-06-20T00:00:00+00:00")
    return db.list_topology_snapshots(limit=1)[0]["id"]


# --------------------------------------------------------------------------- #
# db readers (parameterised, clamped, fail-closed)
# --------------------------------------------------------------------------- #
def test_list_topology_snapshots_newest_first_capped(client: TestClient) -> None:
    for _ in range(3):
        _store_snapshot()
    rows = db.list_topology_snapshots(limit=10)
    assert rows
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)  # newest first
    # only id/received_at/counts -- never the heavy graph blob
    assert all(set(r) == {"id", "received_at", "node_count", "link_count"} for r in rows)


@pytest.mark.parametrize("bad", [0, -5, 5000])
def test_list_topology_snapshots_clamps_limit(client: TestClient, bad: int) -> None:
    _store_snapshot()
    rows = db.list_topology_snapshots(limit=bad)
    # clamp to 1..500, never raises, never returns more than the cap
    assert 1 <= len(rows) <= 500


def test_get_topology_snapshot_returns_parsed_graph(client: TestClient) -> None:
    sid = _store_snapshot(nodes=4, links=3)
    snap = db.get_topology_snapshot(sid)
    assert snap is not None
    assert snap["id"] == sid
    assert snap["node_count"] == 4 and snap["link_count"] == 3
    # the stored graph is a subset (nodes/links only), parsed back to dicts
    assert len(snap["graph"]["nodes"]) == 4
    assert snap["graph"]["links"][0]["via_source"] == "lldp"


def test_get_topology_snapshot_missing_id_is_none(client: TestClient) -> None:
    assert db.get_topology_snapshot(987654321) is None


@pytest.mark.parametrize("bad", [None, "abc", -1, 0, "1.5"])
def test_get_topology_snapshot_rejects_bad_id(client: TestClient, bad) -> None:
    # fail-closed: a non-positive / unparseable id -> None, never a query
    assert db.get_topology_snapshot(bad) is None


# --------------------------------------------------------------------------- #
# API: /network-map/snapshots + ?at=<id>
# --------------------------------------------------------------------------- #
def test_api_snapshots_list(client: TestClient) -> None:
    sid = _store_snapshot()
    r = client.get("/api/v1/network-map/snapshots")
    assert r.status_code == 200
    rows = r.json()["snapshots"]
    assert any(s["id"] == sid for s in rows)
    assert all("graph" not in s for s in rows)  # counts only


def test_api_graph_at_returns_historical_frame_unified_shape(client: TestClient) -> None:
    sid = _store_snapshot(nodes=5, links=2)
    r = client.get(f"/api/v1/network-map/graph?at={sid}")
    assert r.status_code == 200
    g = r.json()
    # unified contract
    assert set(g) >= {"nodes", "links", "subnets", "totals"}
    assert g["subnets"] == []  # live overlays absent by design (D5)
    assert g["totals"]["nodes"] == 5 and g["totals"]["links"] == 2
    assert g["totals"]["agents"] == 0  # historical totals can't recompute identity
    # the frame marks itself historical for the UI plaque
    assert g["history_at"] == sid
    assert g["received_at"]


def test_api_graph_at_bypasses_live_cache(client: TestClient) -> None:
    """A historical read must not poison the live GraphCache: it builds straight from
    the snapshot store, so a later cache-less read still reflects the live graph."""
    sid = _store_snapshot(nodes=2)
    cache = getattr(client.app.state, "network_map_cache", None)
    if cache is not None:
        cache.invalidate()
        cache.get()  # warm the cache with the live graph
    hist = client.get(f"/api/v1/network-map/graph?at={sid}").json()
    assert hist["history_at"] == sid
    live = client.get("/api/v1/network-map/graph").json()
    if cache is not None:
        assert "history_at" not in live  # live frame, not the cached historical one
        # the cache object is unchanged identity (no new instance created)
        assert getattr(client.app.state, "network_map_cache", None) is cache


@pytest.mark.parametrize("bad", ["abc", "-1", "0"])
def test_api_graph_at_bad_id_404(client: TestClient, bad: str) -> None:
    r = client.get(f"/api/v1/network-map/graph?at={bad}")
    assert r.status_code == 404


def test_api_graph_at_missing_snapshot_404(client: TestClient) -> None:
    r = client.get("/api/v1/network-map/graph?at=987654321")
    assert r.status_code == 404


def test_api_graph_empty_at_falls_back_to_live(client: TestClient) -> None:
    """``?at=`` (empty) is treated as 'no at' -> the live unified graph (graceful)."""
    _store_snapshot()
    r = client.get("/api/v1/network-map/graph?at=")
    assert r.status_code == 200
    g = r.json()
    assert "history_at" not in g


def test_api_graph_without_at_is_the_live_unified_graph(client: TestClient) -> None:
    _store_snapshot()
    r = client.get("/api/v1/network-map/graph")
    assert r.status_code == 200
    g = r.json()
    assert "history_at" not in g  # live frame carries no historical marker
    assert set(g) >= {"nodes", "links", "subnets", "totals"}

