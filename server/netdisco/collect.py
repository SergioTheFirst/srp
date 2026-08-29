"""D1 "Собрать карту сейчас": everything the server can do without agents, run
once as an ordered background job -- discovery -> inventory -> classify ->
topology -> reachability -> printers -- then a synchronous ``GraphCache``
rebuild so the very next dashboard read already sees the fresh graph instead
of the stale-while-revalidate window every other poll route leaves open.

The old "собрать топологию сейчас" button (``/api/v1/topology/poll``) only ran
ONE cycle and never waited for it; this module is the full pipeline the spec
(2026-08-23 D1) asks for, plus a state dict the dashboard polls for progress.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from server.netdisco import reconcile, scheduler
from server.netdisco.cache import GraphCache
from server.netdisco.config import NetdiscoConfig
from server.printers import scheduler as printer_scheduler
from server.printers.config import PrinterConfig

_log = logging.getLogger("srp.netdisco")

# "cache" is a 7th, result-less phase: the UI's "фаза (n/N)" counter would
# otherwise show "(0/6)" during the final synchronous GraphCache rebuild (an
# unknown/absent status.phase indexes to -1). It never appears as a key in
# _state["result"] -- only the 6 phases in _run_pipeline's ``calls`` do.
PHASES = ("discovery", "inventory", "classify", "topology", "reachability", "printers", "cache")

_BUSY_RETRY_SEC = 2.0
_BUSY_TOTAL_SEC = 60.0  # per-phase cap on "another cycle holds the lock" retries
# ponytail: одна ручка, без планировщика -- кнопка даёт свежую карту не чаще раза в 5 мин
_MIN_GAP_SEC = 300.0

# Module-level names so a test can monkeypatch a single phase without faking the
# whole pipeline -- same style as the injectable defaults in scheduler.py/reconcile.py.
run_discovery_cycle = scheduler.run_discovery_cycle
run_inventory_cycle = scheduler.run_inventory_cycle
run_classify_cycle = scheduler.run_classify_cycle
run_topology_cycle = reconcile.run_topology_cycle
run_reachability_cycle = reconcile.run_reachability_cycle
poll_printers_now = printer_scheduler.poll_now

_lock = threading.Lock()  # guards _state only, never a phase call
# ponytail: одно задание на сервер, очередь не нужна -- лок netdisco и так один
_state: dict[str, Any] = {
    "running": False,
    "phase": None,
    "started_at": None,
    "finished_at": None,
    "result": {},
    # time.monotonic() of the last finish, for the cooldown gate below -- never
    # exposed via collect_status() (a wall-clock caller has no use for it, and
    # monotonic() is not comparable across process restarts anyway).
    "finished_mono": None,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_busy_retry(name: str, call: Callable[[], dict]) -> dict[str, Any]:
    """Run one phase. A ``busy`` result (another cycle already holds the shared
    netdisco/printers poll lock) is retried with a short pause for up to ~60s,
    then recorded as ``skipped_busy`` -- visible to the operator, never silent.
    An exception is logged and recorded as ``error`` for THIS phase only; the
    remaining phases still run (one broken phase must not kill the rest)."""
    deadline = time.monotonic() + _BUSY_TOTAL_SEC
    while True:
        try:
            result = call()
        except Exception:  # noqa: BLE001 -- one bad phase must not kill the rest
            _log.exception("collect-now phase %r failed", name)
            return {"error": 1}
        if not (isinstance(result, dict) and result.get("busy")):
            return result
        if time.monotonic() >= deadline:
            return {**result, "skipped_busy": 1}
        time.sleep(_BUSY_RETRY_SEC)


def _run_pipeline(
    cfg: NetdiscoConfig, printer_cfg: Optional[PrinterConfig], cache: Optional[GraphCache]
) -> None:
    """Worker-thread body: run every phase in order, then refresh the cache.

    ``finally`` always flips ``running`` back off and stamps ``finished_at``,
    even if a phase raised past ``_run_busy_retry`` (it shouldn't) or the cache
    refresh itself fails."""
    calls: tuple[tuple[str, Callable[[], dict]], ...] = (
        ("discovery", lambda: run_discovery_cycle(cfg)),
        ("inventory", lambda: run_inventory_cycle()),
        ("classify", lambda: run_classify_cycle(cfg)),
        ("topology", lambda: run_topology_cycle(cfg)),
        ("reachability", lambda: run_reachability_cycle(cfg)),
        ("printers", lambda: _run_printers_phase(printer_cfg)),
    )
    result: dict[str, Any] = {}
    try:
        for name, call in calls:
            with _lock:
                _state["phase"] = name
            result[name] = _run_busy_retry(name, call)
    finally:
        if cache is not None:
            with _lock:
                _state["phase"] = "cache"  # 7th phase label -- see PHASES above
            try:
                cache.refresh()  # synchronous: the next dashboard read must see it
            except Exception:  # noqa: BLE001 -- worker thread boundary: log, never propagate
                _log.exception("collect-now: graph cache refresh failed")
        with _lock:
            _state["running"] = False
            _state["phase"] = None
            _state["finished_at"] = _iso_now()
            _state["finished_mono"] = time.monotonic()
            _state["result"] = result


def _run_printers_phase(printer_cfg: Optional[PrinterConfig]) -> dict[str, Any]:
    """``None`` means printer polling is OFF in config (main.py's own gate for
    starting the periodic loop) -- skip cleanly rather than probing with a
    config the operator disabled."""
    if printer_cfg is None:
        return {"skipped": 1}
    return poll_printers_now(printer_cfg)


def start_collect(
    cfg: NetdiscoConfig, printer_cfg: Optional[PrinterConfig], cache: Optional[GraphCache]
) -> dict[str, int]:
    """Kick off the whole "собрать карту сейчас" pipeline on a daemon thread.

    Returns ``{"busy": 1}`` if a job is already running (one job at a time per
    server, D1 -- no queue). Returns ``{"busy": 1, "cooldown": 1,
    "retry_after_sec": N}`` if the last job finished less than ``_MIN_GAP_SEC``
    ago -- the operator hitting the button repeatedly must not re-run the full
    server-side pass every time. Otherwise starts the thread and returns
    ``{"started": 1}`` at once so the HTTP handler never blocks on the pipeline."""
    with _lock:
        if _state["running"]:
            return {"busy": 1}
        finished_mono = _state["finished_mono"]
        if finished_mono is not None:
            elapsed = time.monotonic() - finished_mono
            if elapsed < _MIN_GAP_SEC:
                return {
                    "busy": 1,
                    "cooldown": 1,
                    "retry_after_sec": math.ceil(_MIN_GAP_SEC - elapsed),
                }
        _state["running"] = True
        _state["phase"] = None
        _state["started_at"] = _iso_now()
        _state["finished_at"] = None
        _state["result"] = {}
    try:
        threading.Thread(target=_run_pipeline, args=(cfg, printer_cfg, cache), daemon=True).start()
    except Exception:  # noqa: BLE001 -- thread creation failed: undo the reservation above
        with _lock:
            _state["running"] = False
        raise
    return {"started": 1}


def collect_status() -> dict[str, Any]:
    """A snapshot of the job state for the dashboard's status poll, plus the
    ordered phase list so the UI can show "фаза (n/7)". ``finished_mono`` is an
    internal cooldown-gate detail (monotonic clock, meaningless to a caller) --
    never included in the snapshot."""
    with _lock:
        state = dict(_state)
    state.pop("finished_mono", None)
    state["phases"] = list(PHASES)
    return state
