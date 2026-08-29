"""D1 "Собрать карту сейчас": one background job running every server-only phase
(discovery -> inventory -> classify -> topology -> reachability -> printers) in
order, then a synchronous ``GraphCache.refresh()`` -- see
docs/superpowers/specs/2026-08-23-netmap-actual-state-design.md.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from server.netdisco import collect
from server.netdisco.config import NetdiscoConfig


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _idle_state() -> dict:
    return {
        "running": False,
        "phase": None,
        "started_at": None,
        "finished_at": None,
        "result": {},
        "finished_mono": None,  # no cooldown active
    }


@pytest.fixture(autouse=True)
def _reset_collect_state():
    """The job state is a module-level singleton (D1: one job per server, no
    queue) -- reset it around every test so a leftover run (or an active
    cooldown from ``finished_mono``) never leaks into the next test."""
    with collect._lock:
        collect._state.update(_idle_state())
    yield
    with collect._lock:
        collect._state.update(_idle_state())


def _patch_phase(monkeypatch, name: str, fn):
    monkeypatch.setattr(collect, name, fn)


def _fast_ok(**extra):
    def fn(*_a, **_k):
        return {"busy": 0, **extra}

    return fn


def test_pipeline_runs_phases_in_the_documented_order(monkeypatch):
    order: list = []

    def make(name):
        def fn(*_a, **_k):
            order.append(name)
            return {"busy": 0}

        return fn

    _patch_phase(monkeypatch, "run_discovery_cycle", make("discovery"))
    _patch_phase(monkeypatch, "run_inventory_cycle", make("inventory"))
    _patch_phase(monkeypatch, "run_classify_cycle", make("classify"))
    _patch_phase(monkeypatch, "run_topology_cycle", make("topology"))
    _patch_phase(monkeypatch, "run_reachability_cycle", make("reachability"))
    _patch_phase(monkeypatch, "poll_printers_now", make("printers"))

    collect._run_pipeline(NetdiscoConfig(), None, None)  # printer_cfg=None -> phase skips
    assert order == ["discovery", "inventory", "classify", "topology", "reachability"]
    status = collect.collect_status()
    assert status["result"]["printers"] == {"skipped": 1}
    # "cache" is PHASES' 7th, result-less label (F4) -- it is never a result key.
    assert list(status["result"].keys()) == list(collect.PHASES[:-1])
    assert collect.PHASES[-1] == "cache"


def test_busy_phase_retries_then_is_marked_skipped_busy(monkeypatch):
    monkeypatch.setattr(collect, "_BUSY_TOTAL_SEC", 0.05)
    monkeypatch.setattr(collect, "_BUSY_RETRY_SEC", 0.01)
    calls = {"n": 0}

    def always_busy(*_a, **_k):
        calls["n"] += 1
        return {"busy": 1}

    for name in (
        "run_discovery_cycle",
        "run_inventory_cycle",
        "run_classify_cycle",
        "run_topology_cycle",
        "run_reachability_cycle",
    ):
        _patch_phase(monkeypatch, name, always_busy)
    _patch_phase(monkeypatch, "poll_printers_now", _fast_ok())

    collect._run_pipeline(NetdiscoConfig(), None, None)
    result = collect.collect_status()["result"]
    assert calls["n"] > 1  # retried at least once before giving up
    assert result["discovery"]["skipped_busy"] == 1
    assert result["discovery"]["busy"] == 1


def test_exception_in_one_phase_does_not_stop_the_others(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("classify exploded")

    _patch_phase(monkeypatch, "run_discovery_cycle", _fast_ok(discovered=0))
    _patch_phase(monkeypatch, "run_inventory_cycle", _fast_ok(persisted=0))
    _patch_phase(monkeypatch, "run_classify_cycle", boom)
    _patch_phase(monkeypatch, "run_topology_cycle", _fast_ok(links=0))
    _patch_phase(monkeypatch, "run_reachability_cycle", _fast_ok(down=0))
    _patch_phase(monkeypatch, "poll_printers_now", _fast_ok(polled=0))

    collect._run_pipeline(NetdiscoConfig(), None, None)
    result = collect.collect_status()["result"]
    assert result["classify"] == {"error": 1}
    assert result["discovery"] == {"busy": 0, "discovered": 0}
    assert result["topology"] == {"busy": 0, "links": 0}
    assert result["reachability"] == {"busy": 0, "down": 0}


def test_start_collect_returns_busy_while_a_job_is_running(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def blocking_discovery(*_a, **_k):
        started.set()
        release.wait(timeout=2)
        return {"busy": 0}

    for name in (
        "run_inventory_cycle",
        "run_classify_cycle",
        "run_topology_cycle",
        "run_reachability_cycle",
    ):
        _patch_phase(monkeypatch, name, _fast_ok())
    _patch_phase(monkeypatch, "run_discovery_cycle", blocking_discovery)
    _patch_phase(monkeypatch, "poll_printers_now", _fast_ok())

    first = collect.start_collect(NetdiscoConfig(), None, None)
    assert first == {"started": 1}
    assert started.wait(timeout=2)

    second = collect.start_collect(NetdiscoConfig(), None, None)
    assert second == {"busy": 1}

    release.set()
    assert _wait_until(lambda: not collect.collect_status()["running"])


def test_start_collect_refreshes_the_cache_exactly_once(monkeypatch):
    refreshes = {"n": 0}

    class FakeCache:
        def refresh(self):
            refreshes["n"] += 1
            return {}

    for name in (
        "run_discovery_cycle",
        "run_inventory_cycle",
        "run_classify_cycle",
        "run_topology_cycle",
        "run_reachability_cycle",
        "poll_printers_now",
    ):
        _patch_phase(monkeypatch, name, _fast_ok())

    result = collect.start_collect(NetdiscoConfig(), None, FakeCache())
    assert result == {"started": 1}
    assert _wait_until(lambda: not collect.collect_status()["running"])
    assert refreshes["n"] == 1


def test_start_collect_returns_cooldown_after_recent_finish(monkeypatch):
    with collect._lock:
        collect._state["finished_mono"] = time.monotonic()  # just finished

    def boom(*_a, **_k):
        raise AssertionError("a phase ran during the cooldown window")

    for name in (
        "run_discovery_cycle",
        "run_inventory_cycle",
        "run_classify_cycle",
        "run_topology_cycle",
        "run_reachability_cycle",
        "poll_printers_now",
    ):
        _patch_phase(monkeypatch, name, boom)

    result = collect.start_collect(NetdiscoConfig(), None, None)
    assert result["busy"] == 1
    assert result["cooldown"] == 1
    assert 0 < result["retry_after_sec"] <= collect._MIN_GAP_SEC
    assert collect.collect_status()["running"] is False  # nothing was started


def test_start_collect_runs_again_once_the_cooldown_gap_elapses(monkeypatch):
    with collect._lock:
        collect._state["finished_mono"] = time.monotonic() - collect._MIN_GAP_SEC - 1
    for name in (
        "run_discovery_cycle",
        "run_inventory_cycle",
        "run_classify_cycle",
        "run_topology_cycle",
        "run_reachability_cycle",
        "poll_printers_now",
    ):
        _patch_phase(monkeypatch, name, _fast_ok())

    result = collect.start_collect(NetdiscoConfig(), None, None)
    assert result == {"started": 1}
    assert _wait_until(lambda: not collect.collect_status()["running"])


def test_start_collect_resets_running_if_thread_start_fails(monkeypatch):
    class BoomThread:
        def __init__(self, *_a, **_k):
            pass

        def start(self):
            raise RuntimeError("no threads available")

    monkeypatch.setattr(collect.threading, "Thread", BoomThread)

    with pytest.raises(RuntimeError):
        collect.start_collect(NetdiscoConfig(), None, None)
    assert collect.collect_status()["running"] is False


def test_collect_status_shape():
    status = collect.collect_status()
    assert set(status.keys()) == {
        "running",
        "phase",
        "started_at",
        "finished_at",
        "result",
        "phases",
    }
    assert status["phases"] == list(collect.PHASES)
    assert status["running"] is False


@pytest.fixture
def netdisco_client(tmp_path) -> TestClient:
    """A client with netdisco ON end-to-end (both the periodic-loop gate AND the
    nested config's own ``enabled``) -- the default ``client`` fixture leaves
    netdisco off (F5 kill-switch), so the collect route answers ``skipped``."""
    from server.config import ServerConfig
    from server.main import create_app

    app = create_app(
        ServerConfig(
            db_path=str(tmp_path / "t.db"),
            netdisco_enabled=True,
            netdisco={"enabled": True},
        )
    )
    with TestClient(app) as c:
        yield c


def test_collect_route_returns_started_with_phases_mocked(netdisco_client: TestClient, monkeypatch):
    import server.api as api

    monkeypatch.setattr(api, "start_collect", lambda *_a, **_k: {"started": 1})
    resp = netdisco_client.post("/api/v1/network-map/collect")
    assert resp.status_code == 200
    assert resp.json() == {"started": 1}


def test_collect_route_skips_when_netdisco_disabled(client: TestClient):
    """F5: the kill-switch is honoured by the button too, not just the periodic
    loops -- the default ``client`` fixture has netdisco off."""
    resp = client.post("/api/v1/network-map/collect")
    assert resp.status_code == 200
    assert resp.json() == {"skipped": 1, "reason": "netdisco_disabled"}


def test_collect_status_route_returns_the_status_shape(client: TestClient):
    resp = client.get("/api/v1/network-map/collect/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "running" in body and "phases" in body and "result" in body


def test_collect_route_is_rate_limited_after_a_burst(netdisco_client: TestClient):
    assert netdisco_client.post("/api/v1/network-map/collect").status_code == 200  # in budget
    statuses = {netdisco_client.post("/api/v1/network-map/collect").status_code for _ in range(40)}
    assert 429 in statuses  # the flood is throttled
