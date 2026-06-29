"""Ф3 -- §3.15 read-through TTL cache for the unified network-map graph.

The unified graph (``build_network_map``) turns over roughly once a topology/
inventory cycle, but the map endpoints may be polled by a dashboard far more often.
A short TTL read-through cache serves the cached graph without re-querying the DB or
re-running the assembler on every request, and reloads once the TTL lapses or
``invalidate`` is called (e.g. after a poll forces a fresh build). Thread-safe; the
loader and clock are injected so it is trivially testable.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from server import db
from server.netdisco.unified import build_network_map

# A short window: long enough to absorb a dashboard's polling, short enough that a
# fresh graph shows up within a minute even without explicit invalidation.
_DEFAULT_TTL_SEC = 45.0


def load_network_map() -> dict[str, Any]:
    """Read every backbone table + agent snapshots + printers and assemble the one
    unified network-map graph (Ф3).

    This is the cache's default loader: the read-side DB fan-out lives here (the
    API/cache layer, D7) so the assembler in ``unified.py`` stays pure over already-
    read inputs. The result is never ``None`` -- an empty fleet yields a well-formed
    empty graph -- which keeps the cache contract simple (``get`` returns the graph).
    """
    return build_network_map(
        db.get_net_devices(),
        db.get_net_links(),
        db.get_network_snapshots(),
        db.get_printers(),
        db.get_net_interfaces(),
    )


class GraphCache:
    def __init__(
        self,
        *,
        ttl_sec: float = _DEFAULT_TTL_SEC,
        loader: Callable[[], Optional[dict]] = load_network_map,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_sec
        self._loader = loader
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded_at: Optional[float] = None
        self._value: Optional[dict] = None

    def get(self) -> Optional[dict[str, Any]]:
        """The unified network-map graph, served from cache within the TTL.

        A cold/expired rebuild holds the lock for the full loader call (one build
        per TTL window by design): concurrent readers block on it rather than each
        triggering their own build. Steady-state traffic is absorbed by the TTL; the
        force-poll buttons also invalidate, so a user mashing "собрать топологию" while
        the dashboard polls can stall map reads for the build duration. Acceptable
        now (single fleet, small graph); a serve-stale refresh is the later opt-in.
        """
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
