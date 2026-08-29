"""Ф3 -- §3.15 read-through TTL cache for the unified network-map graph.

The unified graph (``build_network_map``) turns over roughly once a topology/
inventory cycle, but the map endpoints may be polled by a dashboard far more often.
A short TTL read-through cache serves the cached graph without re-querying the DB or
re-running the assembler on every request. Once the TTL lapses or ``invalidate`` is
called (e.g. after a poll forces a fresh build), the next ``get`` serves the last
known graph at once and rebuilds it on a background thread (o5-B2) -- readers never
block on a rebuild except on a cold cache. Thread-safe; the loader and clock are
injected so it is trivially testable.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from server import db
from server.netdisco.unified import build_network_map

# A short window: long enough to absorb a dashboard's polling, short enough that a
# fresh graph shows up within a minute even without explicit invalidation.
_DEFAULT_TTL_SEC = 45.0

# The background rebuild has no request to fail: it logs instead (review of block B).
_log = logging.getLogger("srp.netdisco")


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
        db.get_net_changes(days=7),
        db.get_net_device_status_series(),
        db.get_net_routes(),
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
        self._lock = threading.Lock()  # guards _loaded_at/_value only, never the loader call
        self._rebuild_lock = threading.Lock()  # held for the duration of one loader call
        # _loaded_at means exactly "when the value currently in _value was published",
        # nothing else: it is the ETag source (o5-B8), so it must identify the served
        # body. "Needs a rebuild" is a separate flag -- overloading _loaded_at with it
        # made every invalidated window share the tag W/"0" across DIFFERENT graphs.
        self._loaded_at: Optional[float] = None
        self._forced_stale = False
        self._value: Optional[dict] = None

    @property
    def loaded_at(self) -> Optional[float]:
        """When the cached graph was last (re)built (o5-B8: ETag source)."""
        with self._lock:
            return self._loaded_at

    def get(self) -> Optional[dict[str, Any]]:
        """The unified network-map graph, served from cache within the TTL."""
        return self.get_with_stamp()[0]

    def get_with_stamp(self) -> tuple[Optional[dict[str, Any]], Optional[float]]:
        """The graph AND the publish time of exactly that graph, read together.

        o5-B2: a stale-but-present value is served immediately and refreshed on a
        background thread, instead of blocking the caller for the whole rebuild --
        the "собрать топологию сейчас" button was making the dashboard stall on a
        full SNMP walk. ``_rebuild_lock`` still limits it to one rebuild at a time;
        a caller that loses that race just gets the same stale value back.

        The stamp comes back with the value because the ETag (o5-B8) must describe
        the body actually returned: reading ``loaded_at`` in a second call would let
        a background rebuild land in between and tag the OLD graph with the NEW
        stamp -- after which a client holding that tag gets a 304 forever.
        """
        with self._lock:
            now = self._clock()
            fresh = self._loaded_at is not None and (now - self._loaded_at) < self._ttl
            value, stamp = self._value, self._loaded_at
            if fresh and not self._forced_stale:
                return value, stamp

        if value is not None:
            # ponytail: the staleness window this opens is bounded by the duration
            # of one rebuild (single fleet, small graph) -- good enough without a
            # per-key staleness budget or a generation counter.
            if self._rebuild_lock.acquire(blocking=False):
                threading.Thread(target=self._background_rebuild, daemon=True).start()
            return value, stamp

        # Cold start: nothing cached yet -- callers must never see None, so block
        # and build synchronously (same as the very first read used to).
        self._rebuild_lock.acquire()
        with self._lock:
            warmed, warmed_stamp = self._value, self._loaded_at
        if warmed is not None:
            # Another cold-start caller won the lock and already published: reuse its
            # result instead of paying for a second identical load.
            self._rebuild_lock.release()
            return warmed, warmed_stamp
        return self._rebuild_and_release()

    def _rebuild_and_release(self) -> tuple[Optional[dict[str, Any]], Optional[float]]:
        """Run the loader, publish the result, and release ``_rebuild_lock``.

        Always called with ``_rebuild_lock`` already held by the caller (either the
        cold-start path or ``get``'s background thread); releases it in ``finally``
        so a loader exception never wedges the cache shut for good.
        """
        try:
            with self._lock:
                # Cleared BEFORE the load, not after: an invalidate arriving while the
                # loader runs must survive and trigger another rebuild, not be erased
                # by the rebuild it raced with.
                self._forced_stale = False
            value = self._loader()
            with self._lock:
                self._value = value
                self._loaded_at = self._clock()
                return value, self._loaded_at
        finally:
            self._rebuild_lock.release()

    def _background_rebuild(self) -> None:
        """Thread body for the serve-stale refresh: a loader failure here reaches no
        request, so it is logged instead of vanishing into the thread excepthook. The
        previous graph keeps being served and the next rebuild happens when the TTL
        lapses -- not on every read, which would spin a failing loader forever."""
        try:
            self._rebuild_and_release()
        except Exception:  # noqa: BLE001 -- thread boundary: log, never propagate
            _log.exception("network-map cache rebuild failed; keeping the previous graph")

    def invalidate(self) -> None:
        """Mark the cached snapshot stale; it is still served (immediately) by the
        next ``get`` while that call kicks off a background rebuild (o5-B2).

        ``_loaded_at`` is deliberately left alone -- it identifies the value still
        being served (the ETag, o5-B8), and zeroing it made two different graphs
        share one tag."""
        with self._lock:
            self._forced_stale = True

    def refresh(self) -> Optional[dict[str, Any]]:
        """D1 "собрать карту сейчас": a synchronous, BLOCKING rebuild -- unlike
        ``get()``'s stale-while-revalidate, the caller waits for the fresh graph
        before returning (the whole point of the button: the next dashboard read
        must already see it, not race a background thread for up to one TTL).

        Marks the cache stale first, then reuses the same ``_rebuild_lock`` /
        ``_rebuild_and_release`` every other rebuild path shares -- a concurrent
        background rebuild just makes this call wait its turn instead of racing
        it. A loader failure is logged here (never raised past this call, same
        contract as ``_background_rebuild``) and leaves the cache marked stale
        again, so the next read still tries a fresh rebuild instead of quietly
        serving the old graph forever."""
        with self._lock:
            self._forced_stale = True
        self._rebuild_lock.acquire()
        try:
            value, _stamp = self._rebuild_and_release()
            return value
        except Exception:  # noqa: BLE001 -- caller has no request to fail; log instead
            _log.exception("network-map cache refresh failed; keeping the previous graph")
            with self._lock:
                self._forced_stale = True
                return self._value
