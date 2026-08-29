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
from typing import Dict


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
