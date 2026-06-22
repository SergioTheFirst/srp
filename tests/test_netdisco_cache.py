"""Phase 11 -- §3.15 read-through TTL graph cache (RED first).

The topology graph changes about once an hour but the API may be hit often, so the
graph is cached with a short TTL: a second read inside the window returns the cached
snapshot without re-querying the DB; once the TTL lapses (or on explicit invalidate)
the next read reloads. Thread-safe; the clock is injected so the test is not timing-
dependent.
"""

from __future__ import annotations

from server.netdisco.cache import GraphCache


def _counting_loader():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"received_at": "t", "graph": {"nodes": [], "links": []}}

    return loader, calls


def test_second_read_within_ttl_uses_cache():
    loader, calls = _counting_loader()
    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    cache.get()
    cache.get()
    assert calls["n"] == 1  # only one DB load


def test_read_after_ttl_reloads():
    loader, calls = _counting_loader()
    now = {"v": 100.0}
    cache = GraphCache(ttl_sec=30, loader=loader, clock=lambda: now["v"])
    cache.get()
    now["v"] = 131.0  # past the 30s TTL
    cache.get()
    assert calls["n"] == 2


def test_invalidate_forces_reload_even_within_ttl():
    loader, calls = _counting_loader()
    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    cache.get()
    cache.invalidate()
    cache.get()
    assert calls["n"] == 2


def test_get_returns_loaded_snapshot():
    loader, _ = _counting_loader()
    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    assert cache.get()["graph"] == {"nodes": [], "links": []}
