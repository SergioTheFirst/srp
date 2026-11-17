"""Phase 11 -- §3.16 scanner telemetry: thread-safe in-memory counters.

The discovery/topology/reachability cycles bump these as they run so an operator can
see, via ``/netdisco/stats``, that the scanner is alive and how much work each cycle
does (cycles, devices probed, links resolved, deltas detected, outages found). Pure
in-memory and process-local -- not persisted, not a metrics backend, just a cheap
liveness/throughput window (scope ceiling: no new observability platform).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional


class ScannerMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = defaultdict(int)

    def observe_cycle(self, cycle: str, **counts: int) -> None:
        """Record one completed cycle of ``cycle`` plus its per-run counters."""
        with self._lock:
            self._counters[f"{cycle}_cycles"] += 1
            for key, value in counts.items():
                self._counters[key] += int(value)

    def snapshot(self) -> Dict[str, int]:
        """A defensive copy of the current counters."""
        with self._lock:
            return dict(self._counters)


# Process-wide singleton the cycles record into and the API reads.
METRICS = ScannerMetrics()


_ABSENT = ("missing", "eligible_purge")
_PORT_KINDS = ("l2-edge", "l2-trunk", "adapter")


def map_coverage(
    nodes: Iterable[Dict[str, Any]], links: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """How well the map is attached: present nodes by best link class + hints (pure)."""
    nodes = list(nodes)
    links = list(links)
    present = [n for n in nodes if n.get("status") not in _ABSENT]
    port: set = set()
    seg: set = set()
    anyl: set = set()
    by_src: Dict[Optional[str], int] = defaultdict(int)
    for e in links:
        ends = (e.get("a"), e.get("b"))
        kind = e.get("link_kind")
        by_src[e.get("via_source")] += 1
        anyl.update(ends)
        if kind in _PORT_KINDS:
            port.update(ends)
        elif kind == "l2-segment":
            seg.update(ends)
    cls = {"port": 0, "segment": 0, "uplink": 0, "none": 0}
    lonely = []
    for n in present:
        nid = n.get("nid")
        key = (
            "port"
            if nid in port
            else "segment"
            if nid in seg
            else "uplink"
            if nid in anyl
            else "none"
        )
        cls[key] += 1
        if key == "none":
            lonely.append(n)
    hints: List[Dict[str, Any]] = []
    if present and not cls["port"]:
        hints.append({"code": "no_port_evidence"})
    agent_subnets = {n.get("subnet") for n in nodes if n.get("device_id")}
    blind = {n["subnet"] for n in lonely if n.get("subnet")} - agent_subnets
    if blind:
        hints.append({"code": "subnet_no_vantage", "count": len(blind)})
    return {"nodes": {"present": len(present), **cls}, "links": dict(by_src), "hints": hints}
