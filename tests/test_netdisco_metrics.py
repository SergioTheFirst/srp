"""Phase 11 -- §3.16 scanner telemetry counters (RED first).

Thread-safe in-memory counters the discovery cycles bump as they run (cycles,
probed, links, deltas, down/unreachable). ``/netdisco/stats`` exposes the snapshot so
an operator can see the scanner is actually working and how hard.
"""

from __future__ import annotations

import threading

from server.netdisco.metrics import ScannerMetrics


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
