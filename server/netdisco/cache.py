"""Phase 11 -- §3.15 read-through TTL cache for the topology graph.

The persisted graph snapshot turns over roughly once a topology cycle (hourly), but
the ``/topology/graph`` endpoint may be polled by a dashboard far more often. A short
TTL read-through cache serves the cached snapshot without re-hitting the DB, and
reloads once the TTL lapses or ``invalidate`` is called (e.g. after a new snapshot).
Thread-safe; the loader and clock are injected so it is trivially testable.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from server import db

# A short window: long enough to absorb a dashboard's polling, short enough that a
# fresh topology snapshot shows up within a minute even without explicit invalidation.
_DEFAULT_TTL_SEC = 45.0


class GraphCache:
    def __init__(
        self,
        *,
        ttl_sec: float = _DEFAULT_TTL_SEC,
        loader: Callable[[], Optional[dict]] = db.get_latest_topology_snapshot,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_sec
        self._loader = loader
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded_at: Optional[float] = None
        self._value: Optional[dict] = None

    def get(self) -> Optional[dict[str, Any]]:
        """The latest topology snapshot, served from cache within the TTL."""
        with self._lock:
            now = self._clock()
            if self._loaded_at is not None and (now - self._loaded_at) < self._ttl:
                return self._value
            self._value = self._loader()
            self._loaded_at = now
            return self._value

    def invalidate(self) -> None:
        """Drop the cached snapshot; the next ``get`` reloads from the loader."""
        with self._lock:
            self._loaded_at = None
            self._value = None
