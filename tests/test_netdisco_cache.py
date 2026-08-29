"""Phase 11 -- §3.15 read-through TTL graph cache (RED first).

The topology graph changes about once an hour but the API may be hit often, so the
graph is cached with a short TTL: a second read inside the window returns the cached
snapshot without re-querying the DB; once the TTL lapses (or on explicit invalidate)
the next read reloads. Thread-safe; the clock is injected so the test is not timing-
dependent.

o5-B2: a stale/expired read no longer blocks on the rebuild -- it serves the old
graph at once and refreshes it on a background thread. Tests that provoke that path
poll for the background call to land instead of asserting on it synchronously.
"""

from __future__ import annotations

import logging
import threading
import time

from server.netdisco.cache import GraphCache


def _counting_loader():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"received_at": "t", "graph": {"nodes": [], "links": []}}

    return loader, calls


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses.

    o5-B2's background rebuild has no join/callback hook exposed to callers by
    design (fire-and-forget), so tests that need to observe its effect poll for it
    instead, bounded so a regression fails fast rather than hanging the suite.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


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
    cache.get()  # o5-B2: serves the stale value at once, reloads in the background
    assert _wait_until(lambda: calls["n"] == 2)


def test_invalidate_forces_reload_even_within_ttl():
    loader, calls = _counting_loader()
    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    cache.get()
    cache.invalidate()
    cache.get()  # o5-B2: serves the stale value at once, reloads in the background
    assert _wait_until(lambda: calls["n"] == 2)


def test_get_returns_loaded_snapshot():
    loader, _ = _counting_loader()
    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    assert cache.get()["graph"] == {"nodes": [], "links": []}


def test_invalidate_serves_stale_until_reload_completes():
    """o5-B2 (d)1: prime the cache, invalidate it, then call get() while the loader
    is blocked on a threading.Event -- get() must return the previous graph at once,
    not wait for the blocked rebuild."""
    calls = {"n": 0}
    rebuild_started = threading.Event()
    unblock = threading.Event()

    def loader():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"graph": "v1"}
        rebuild_started.set()
        assert unblock.wait(timeout=2), "background rebuild never got its turn"
        return {"graph": "v2"}

    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    assert cache.get() == {"graph": "v1"}  # cold start, synchronous
    cache.invalidate()

    stale = cache.get()  # must return immediately, not block on the loader
    assert stale == {"graph": "v1"}
    assert rebuild_started.wait(timeout=2)  # the background rebuild really started
    unblock.set()


def test_cold_get_builds_synchronously():
    """o5-B2 (d)2: on an empty cache, get() must return the loader's result (never
    None) -- there is nothing stale to serve, so the cold build is synchronous."""
    loader, calls = _counting_loader()
    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    result = cache.get()
    assert result is not None
    assert result["graph"] == {"nodes": [], "links": []}
    assert calls["n"] == 1


def test_invalidate_keeps_loaded_at_tied_to_the_served_value():
    """Ревью блока B, F1: `loaded_at` -- источник ETag, поэтому он обязан
    однозначно идентифицировать ОТДАННОЕ тело. Раньше invalidate() обнулял его,
    и каждое окно «сброшено, но ещё не перестроено» получало один и тот же тег
    W/"0" при РАЗНЫХ графах -- клиент со старым If-None-Match получал 304 на
    изменившемся теле и навсегда оставался на прошлой версии карты."""
    values = [{"graph": "v0"}, {"graph": "v1"}]
    calls = {"n": 0}
    clock = {"t": 100.0}

    def loader():
        calls["n"] += 1
        return values[min(calls["n"] - 1, len(values) - 1)]

    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: clock["t"])
    assert cache.get() == {"graph": "v0"}  # cold start

    cache.invalidate()
    clock["t"] = 200.0
    body_1, tag_1 = cache.get_with_stamp()  # окно 1: отдаётся v0
    assert body_1 == {"graph": "v0"}
    assert tag_1 is not None, 'loaded_at обнулён -> ETag схлопывается в W/"0"'
    assert _wait_until(lambda: calls["n"] == 2)

    cache.invalidate()
    clock["t"] = 300.0
    body_2, tag_2 = cache.get_with_stamp()  # окно 2: отдаётся уже v1
    assert body_2 == {"graph": "v1"}
    assert tag_2 != tag_1, "разные тела получили один и тот же ETag"


def test_background_rebuild_logs_loader_failure_and_keeps_stale(caplog):
    """Ревью блока B, F2: падение загрузчика в фоновом потоке не доходило до
    логов приложения (только дефолтный excepthook), а `_loaded_at` не двигался --
    каждое следующее чтение поднимало новый поток, который снова падал. Тихий
    самоповторяющийся сбой без сигнала оператору."""
    calls = {"n": 0}
    clock = {"t": 100.0}

    def loader():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"graph": "v0"}
        raise RuntimeError("db exploded")

    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: clock["t"])
    assert cache.get() == {"graph": "v0"}
    cache.invalidate()
    clock["t"] = 200.0

    with caplog.at_level(logging.ERROR, logger="srp.netdisco"):
        assert cache.get() == {"graph": "v0"}  # старое значение продолжает отдаваться
        assert _wait_until(lambda: calls["n"] == 2)
        assert _wait_until(lambda: any(r.levelno >= logging.ERROR for r in caplog.records))


def test_concurrent_cold_start_loads_once():
    """Ревью блока B, F3: два потока, одновременно попавшие на холодный кэш, оба
    уходили в блокирующий rebuild-замок, и ВТОРОЙ безусловно запускал загрузчик
    ещё раз вместо того, чтобы увидеть уже прогретый кэш."""
    calls = {"n": 0}
    started = threading.Event()

    def loader():
        calls["n"] += 1
        started.set()
        time.sleep(0.05)  # даём второму потоку встать в очередь за замком
        return {"graph": "v0"}

    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(cache.get())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)

    assert calls["n"] == 1, "холодный старт вызвал загрузчик дважды"
    assert results == [{"graph": "v0"}, {"graph": "v0"}]


def test_refresh_blocks_until_the_new_graph_is_published():
    """D1 "собрать карту сейчас": unlike ``get()``'s stale-while-revalidate,
    ``refresh()`` must return the FRESH value -- the whole point of the button
    is that the next read already sees it, with no TTL/background-thread race."""
    values = [{"graph": "v0"}, {"graph": "v1"}]
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return values[min(calls["n"] - 1, len(values) - 1)]

    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    assert cache.get() == {"graph": "v0"}  # cold start

    result = cache.refresh()
    assert result == {"graph": "v1"}
    assert cache.get() == {"graph": "v1"}  # the next read sees it at once, no TTL wait
    assert calls["n"] == 2


def test_refresh_logs_loader_failure_and_keeps_the_cache_stale(caplog):
    """A failed refresh must not wedge the lock shut or lose the stale flag --
    the next ``get()`` still has to try a rebuild instead of quietly serving the
    old graph forever."""
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"graph": "v0"}
        raise RuntimeError("db exploded")

    cache = GraphCache(ttl_sec=45, loader=loader, clock=lambda: 100.0)
    assert cache.get() == {"graph": "v0"}

    with caplog.at_level(logging.ERROR, logger="srp.netdisco"):
        result = cache.refresh()
    assert result == {"graph": "v0"}  # last-known value, not raised past the call
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert cache._rebuild_lock.acquire(blocking=False)  # never left held
    cache._rebuild_lock.release()
