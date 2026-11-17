"""Phase 11 -- §3.16 scanner telemetry counters (RED first).

Thread-safe in-memory counters the discovery cycles bump as they run (cycles,
probed, links, deltas, down/unreachable). ``/netdisco/stats`` exposes the snapshot so
an operator can see the scanner is actually working and how hard.
"""

from __future__ import annotations

import threading

from server.netdisco.metrics import ScannerMetrics, map_coverage


def test_observe_cycle_counts_cycles_and_accumulates_counters():
    m = ScannerMetrics()
    m.observe_cycle("topology", probed=2, links=3)
    m.observe_cycle("topology", probed=1, deltas=4)
    s = m.snapshot()
    assert s["topology_cycles"] == 2
    assert s["probed"] == 3 and s["links"] == 3 and s["deltas"] == 4


def test_snapshot_is_a_defensive_copy():
    m = ScannerMetrics()
    m.observe_cycle("topology", probed=1)
    snap = m.snapshot()
    snap["probed"] = 999
    assert m.snapshot()["probed"] == 1


def test_counters_are_thread_safe():
    m = ScannerMetrics()

    def worker():
        for _ in range(1000):
            m.observe_cycle("reach", down=1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = m.snapshot()
    assert s["reach_cycles"] == 8000 and s["down"] == 8000


# --------------------------------------------------------------------------- #
# map_coverage: how many present nodes are attached to a port / segment / gateway
# --------------------------------------------------------------------------- #
def _n(nid, status="up", subnet=None, device_id=None):
    return {"nid": nid, "status": status, "subnet": subnet, "device_id": device_id}


def _l(a, b, kind, src="x"):
    return {"a": a, "b": b, "link_kind": kind, "via_source": src}


def test_map_coverage_empty_graph():
    c = map_coverage([], [])
    assert c == {
        "nodes": {"present": 0, "port": 0, "segment": 0, "uplink": 0, "none": 0},
        "links": {},
        "hints": [],
    }


def test_map_coverage_classes_by_best_link_and_link_counts():
    nodes = [_n("a"), _n("b"), _n("c"), _n("d"), _n("e")]
    links = [
        _l("a", "b", "l2-edge", "lldp"),
        _l("b", "c", "l2-segment", "arp"),  # b has port link too -> port wins
        _l("c", "d", "agent-uplink", "agent"),
        _l("d", "a", "l3-route", "route"),
    ]
    c = map_coverage(nodes, links)
    assert c["nodes"] == {"present": 5, "port": 2, "segment": 1, "uplink": 1, "none": 1}
    assert c["links"] == {"lldp": 1, "arp": 1, "agent": 1, "route": 1}
    assert {"code": "no_port_evidence"} not in c["hints"]


def test_map_coverage_excludes_missing_nodes():
    nodes = [_n("a"), _n("b", "missing"), _n("c", "eligible_purge")]
    c = map_coverage(nodes, [_l("a", "b", "l2-edge")])
    assert c["nodes"]["present"] == 1 and c["nodes"]["port"] == 1 and c["nodes"]["none"] == 0


def test_map_coverage_hint_no_port_evidence():
    c = map_coverage([_n("a"), _n("b")], [_l("a", "b", "l2-segment")])
    assert {"code": "no_port_evidence"} in c["hints"]


def test_map_coverage_hint_subnet_without_vantage():
    nodes = [
        _n("a", subnet="10.0.1.0/24"),
        _n("b", subnet="10.0.1.0/24"),
        _n("c", subnet="10.0.2.0/24"),
        _n("d", subnet="10.0.3.0/24", device_id="dev"),  # agent in own subnet
        _n("e", subnet="10.0.3.0/24"),
        _n("f"),  # no subnet: not counted
    ]
    c = map_coverage(nodes, [_l("d", "e", "l2-edge")])
    assert {"code": "subnet_no_vantage", "count": 2} in c["hints"]


def test_map_coverage_no_subnet_hint_when_all_have_vantage():
    c = map_coverage([_n("a", subnet="s", device_id="d")], [])
    assert all(h["code"] != "subnet_no_vantage" for h in c["hints"])
