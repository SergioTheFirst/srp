"""SRP server entrypoint: assemble the FastAPI app and run it.

    python -m server.main

Binds host/port from ``server/config.json`` (default 0.0.0.0:8000 so the whole
fleet can reach it). The DB is initialized once on startup from the same config.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from server import db, org_directory
from server.api import router as api_router
from server.config import ServerConfig, load_config
from server.ingest_guards import count_reject
from server.netdisco import reconcile as netdisco_reconcile
from server.netdisco import scheduler as netdisco_scheduler
from server.netdisco.cache import GraphCache
from server.printers import scheduler
from server.web.dashboard import router as web_router

# Reject ingest bodies larger than this to prevent a single agent from
# consuming unbounded memory during synchronous pydantic parsing.
_MAX_INGEST_BODY_BYTES: int = 512 * 1024  # 512 KB; typical envelope << 10 KB


class _IngestBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path == "/api/v1/ingest":
            cl = request.headers.get("content-length")
            if cl and int(cl) > _MAX_INGEST_BODY_BYTES:
                count_reject("too_large")
                return Response("Request body too large", status_code=413)
            # Also guard chunked TE (no Content-Length header): read and cache
            # the body so pydantic can still parse it from the Starlette cache.
            if cl is None:
                body = await request.body()
                if len(body) > _MAX_INGEST_BODY_BYTES:
                    count_reject("too_large")
                    return Response("Request body too large", status_code=413)
        return await call_next(request)


_log = logging.getLogger("srp.retention")
_authlog = logging.getLogger("srp.auth")


def _run_retention_sweep(cfg: ServerConfig) -> None:
    """Delete devices silent past the retention window (server-stamped last_seen).

    Transient errors (e.g. a momentarily locked DB) are swallowed and logged: a
    failed sweep must never crash startup or kill the periodic loop -- the next
    run retries.
    """
    if cfg.device_retention_days <= 0:
        return
    try:
        result = db.purge_devices_silent_for(cfg.device_retention_days)
    except Exception:  # never let a transient sweep error crash the caller
        _log.exception("retention sweep failed")
        return
    if result["count"]:
        _log.info(
            "retention sweep deleted %d silent device(s) (>%d days): %s",
            result["count"],
            cfg.device_retention_days,
            ", ".join(result["device_ids"]),
        )


def _run_maintenance_sweep(cfg: ServerConfig) -> None:
    """Roll up, age-prune, and vacuum the DB (ssd3 Ф5, T5.3).

    One guarded pass, steps in a fixed order: a failed rollup must skip prune
    (never delete raw rows a day never got folded into a rollup row) -- so
    this is a single try/except around the whole sequence, not one per step,
    the same self-guard shape as _run_retention_sweep beside it.
    """
    try:
        db.run_daily_rollup()
        db.run_rulestats_scan()
        db.prune_aged(
            heartbeat_raw_days=cfg.heartbeat_raw_days,
            events_raw_days=cfg.events_raw_days,
            rollup_days=cfg.rollup_days,
        )
        db.run_maintenance()
    except Exception:  # never let a transient maintenance error crash the caller
        _log.exception("maintenance sweep failed")


def _run_disk_readings_backfill() -> None:
    """One-time seed of disk_readings from existing historical rows (ssd3 Ф2).

    Idempotent (db.backfill_disk_readings no-ops once the table is non-empty)
    and never fatal: a transient DB error here must not crash startup.
    """
    try:
        inserted = db.backfill_disk_readings()
    except Exception:  # never let a transient backfill error crash startup
        _log.exception("disk_readings backfill failed")
        return
    if inserted:
        _log.info("disk_readings backfill inserted %d row(s)", inserted)


async def _retention_loop(cfg: ServerConfig) -> None:
    """Re-run the retention + maintenance sweeps every purge_interval_hours
    until cancelled. Maintenance runs via to_thread -- VACUUM can take a
    while and must not block the event loop (netdisco loops use the same
    to_thread-the-whole-cycle pattern)."""
    interval_sec = cfg.purge_interval_hours * 3600
    while True:
        await asyncio.sleep(interval_sec)
        _run_retention_sweep(cfg)  # self-guards transient errors (see above)
        await asyncio.to_thread(_run_maintenance_sweep, cfg)


_plog = logging.getLogger("srp.printers")


def _run_printer_poll(cfg: ServerConfig) -> None:
    """Run one printer poll cycle (blocking SNMP fan-out; called via to_thread).

    Self-guards: a transient network/DB error must never crash startup or kill the
    periodic loop (the retention-sweep lesson).
    """
    try:
        result = scheduler.poll_now(cfg.printer_config())
    except Exception:  # never let a transient cycle error crash the caller
        _plog.exception("printer poll cycle failed")
        return
    if result["polled"]:
        _plog.info(
            "printer poll: %d polled, %d online, %d unreachable, %d errors",
            result["polled"],
            result["online"],
            result["unreachable"],
            result["errors"],
        )


async def _printer_poll_loop(cfg: ServerConfig) -> None:
    """Poll printers at startup, then every poll_interval_sec, until cancelled."""
    interval_sec = max(60, cfg.printer_config().poll_interval_sec)
    while True:
        await asyncio.to_thread(_run_printer_poll, cfg)
        await asyncio.sleep(interval_sec)


_ndlog = logging.getLogger("srp.netdisco")


def _run_netdisco_cycle() -> None:
    """Run one netdisco inventory cycle (cheap rebuild from snapshots; via to_thread).

    Self-guards like the printer/retention loops: a transient error must never
    crash startup or kill the periodic loop.
    """
    try:
        result = netdisco_scheduler.poll_now()
    except Exception:  # never let a transient cycle error crash the caller
        _ndlog.exception("netdisco inventory cycle failed")
        return
    if result.get("persisted"):
        _ndlog.info("netdisco inventory: %d devices", result["persisted"])


async def _netdisco_loop(cfg: ServerConfig) -> None:
    """Rebuild the network inventory at startup, then every interval (+jitter)."""
    nd = cfg.netdisco_config()
    interval_sec = max(60, nd.inventory_interval_sec)
    while True:
        await asyncio.to_thread(_run_netdisco_cycle)
        # jitter de-phases this loop from the other poll loops (anti-thundering-herd)
        await asyncio.sleep(interval_sec + random.uniform(0, nd.jitter_sec))  # nosec B311


def _run_netdisco_discovery_cycle(cfg: ServerConfig) -> None:
    """Run one active-scan discovery cycle (via to_thread). Self-guarded so a
    transient scan error never crashes startup or kills the loop."""
    try:
        result = netdisco_scheduler.run_discovery_cycle(cfg.netdisco_config())
    except Exception:  # never let a transient cycle error crash the caller
        _ndlog.exception("netdisco discovery cycle failed")
        return
    if result.get("discovered"):
        _ndlog.info(
            "netdisco discovery: %d new device(s) from %d scanned host(s)",
            result["discovered"],
            result.get("scanned", 0),
        )


async def _netdisco_discovery_loop(cfg: ServerConfig) -> None:
    """Actively scan the segment for new hosts every interval (+jitter). Started
    only when netdisco AND active_scan are both enabled (the stop-gate)."""
    nd = cfg.netdisco_config()
    interval_sec = max(60, nd.discovery_interval_sec)
    while True:
        await asyncio.to_thread(_run_netdisco_discovery_cycle, cfg)
        await asyncio.sleep(interval_sec + random.uniform(0, nd.jitter_sec))  # nosec B311


def _run_netdisco_classify_cycle(cfg: ServerConfig) -> None:
    """Run one SNMP classify cycle (via to_thread). Self-guarded so a transient
    probe error never crashes startup or kills the loop."""
    try:
        result = netdisco_scheduler.run_classify_cycle(cfg.netdisco_config())
    except Exception:  # never let a transient cycle error crash the caller
        _ndlog.exception("netdisco classify cycle failed")
        return
    if result.get("classified"):
        _ndlog.info(
            "netdisco classify: %d device(s) typed from %d probed",
            result["classified"],
            result.get("probed", 0),
        )


async def _netdisco_classify_loop(cfg: ServerConfig) -> None:
    """SNMP-probe and classify the known hosts every interval (+jitter). Started
    when netdisco is enabled (unicast probes of known hosts, not a range scan)."""
    nd = cfg.netdisco_config()
    interval_sec = max(60, nd.classify_interval_sec)
    while True:
        await asyncio.to_thread(_run_netdisco_classify_cycle, cfg)
        await asyncio.sleep(interval_sec + random.uniform(0, nd.jitter_sec))  # nosec B311


def _run_netdisco_topology_cycle(cfg: ServerConfig) -> None:
    """Run one L2 topology reconcile cycle (via to_thread). Self-guarded so a
    transient SNMP/DB error never crashes startup or kills the loop."""
    try:
        result = netdisco_reconcile.run_topology_cycle(cfg.netdisco_config())
    except Exception:  # never let a transient cycle error crash the caller
        _ndlog.exception("netdisco topology cycle failed")
        return
    if result.get("links"):
        _ndlog.info(
            "netdisco topology: %d link(s) from %d probed device(s)",
            result["links"],
            result.get("probed", 0),
        )


async def _netdisco_topology_loop(cfg: ServerConfig) -> None:
    """Collect LLDP/CDP/FDB evidence off known infra and rebuild the L2 graph every
    interval (+jitter). Started when netdisco is enabled (unicast SNMP, not a scan)."""
    nd = cfg.netdisco_config()
    interval_sec = max(60, nd.topology_interval_sec)
    while True:
        await asyncio.to_thread(_run_netdisco_topology_cycle, cfg)
        await asyncio.sleep(interval_sec + random.uniform(0, nd.jitter_sec))  # nosec B311


def _run_netdisco_reachability_cycle(cfg: ServerConfig) -> None:
    """Run one reachability-correlation cycle (via to_thread). Self-guarded so a
    transient probe error never crashes startup or kills the loop."""
    try:
        result = netdisco_reconcile.run_reachability_cycle(cfg.netdisco_config())
    except Exception:  # never let a transient cycle error crash the caller
        _ndlog.exception("netdisco reachability cycle failed")
        return
    if result.get("down") or result.get("unreachable"):
        _ndlog.info(
            "netdisco reachability: %d down, %d unreachable (suppressed)",
            result.get("down", 0),
            result.get("unreachable", 0),
        )


async def _netdisco_reachability_loop(cfg: ServerConfig) -> None:
    """Ping known RFC1918 devices and correlate outages (down vs unreachable) every
    interval (+jitter). Started when netdisco is enabled (unicast liveness, no scan)."""
    nd = cfg.netdisco_config()
    interval_sec = max(60, nd.reachability_interval_sec)
    while True:
        await asyncio.to_thread(_run_netdisco_reachability_cycle, cfg)
        await asyncio.sleep(interval_sec + random.uniform(0, nd.jitter_sec))  # nosec B311


def _run_netdisco_passive_cycle(cfg: ServerConfig) -> None:
    """Run one passive de-anon cycle (via to_thread). Self-guarded so a transient
    socket/DB error never crashes startup or kills the loop."""
    try:
        result = netdisco_scheduler.run_passive_cycle(cfg.netdisco_config())
    except Exception:  # never let a transient cycle error crash the caller
        _ndlog.exception("netdisco passive cycle failed")
        return
    if result.get("enriched"):
        _ndlog.info("netdisco passive: %d node(s) de-anonymised", result["enriched"])


async def _netdisco_passive_loop(cfg: ServerConfig) -> None:
    """De-anonymise nameless nodes (cross-MAC / reverse-DNS / mDNS / SSDP / NetBIOS /
    WSD / banner) every interval (+jitter). Started when netdisco AND passive are both
    enabled (local-segment multicast + bounded RFC1918 unicast, never a range scan)."""
    nd = cfg.netdisco_config()
    interval_sec = max(60, nd.passive_interval_sec)
    while True:
        await asyncio.to_thread(_run_netdisco_passive_cycle, cfg)
        await asyncio.sleep(interval_sec + random.uniform(0, nd.jitter_sec))  # nosec B311


def _run_netdisco_adapter_cycle(cfg: ServerConfig) -> None:
    """Run one optional-adapter cycle (via to_thread). Self-guarded so a transient
    controller/credential error never crashes startup or kills the loop."""
    try:
        result = netdisco_scheduler.run_adapter_cycle(cfg.netdisco_config())
    except Exception:  # never let a transient cycle error crash the caller
        _ndlog.exception("netdisco adapter cycle failed")
        return
    if result.get("enriched") or result.get("added") or result.get("links"):
        _ndlog.info(
            "netdisco adapters: %d enriched, %d added, %d link(s) from %d adapter(s)",
            result.get("enriched", 0),
            result.get("added", 0),
            result.get("links", 0),
            result.get("adapters", 0),
        )


async def _netdisco_adapter_loop(cfg: ServerConfig) -> None:
    """Pull identity/topology from the operator's optional controllers (MikroTik,
    ...) every interval (+jitter). Started only when at least one adapter is
    configured (read-only, RFC1918, isolated per adapter)."""
    nd = cfg.netdisco_config()
    interval_sec = max(60, nd.adapter_interval_sec)
    while True:
        await asyncio.to_thread(_run_netdisco_adapter_cycle, cfg)
        await asyncio.sleep(interval_sec + random.uniform(0, nd.jitter_sec))  # nosec B311


def create_app(cfg: ServerConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db.set_stale_threshold(cfg.stale_after_sec)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_db(
            cfg.resolved_db_path(),
            retain_heartbeats=cfg.retain_heartbeats,
            retain_events=cfg.retain_events,
            retain_printer_readings=cfg.retain_printer_readings,
            retain_net_readings=cfg.retain_net_readings,
            retain_net_snapshots=cfg.retain_net_snapshots,
            retain_disk_readings=cfg.retain_disk_readings,
        )
        org_directory.init_directory(cfg.resolved_org_directory_path())
        _run_disk_readings_backfill()  # seed disk_readings before any scoring reads it
        _run_retention_sweep(cfg)  # clear long-silent ghosts at startup
        await asyncio.to_thread(_run_maintenance_sweep, cfg)  # ssd3 Ф5: rollup+prune+vacuum
        tasks: list[asyncio.Task[None]] = []
        # NB: gated on purge_interval_hours alone (not device_retention_days too) --
        # _run_retention_sweep already no-ops when device_retention_days<=0, but the
        # same loop also carries Ф5 maintenance, which must keep running even if an
        # operator disables ghost auto-purge specifically.
        if cfg.purge_interval_hours > 0:
            tasks.append(asyncio.create_task(_retention_loop(cfg)))
        if cfg.printer_poll_enabled:
            tasks.append(asyncio.create_task(_printer_poll_loop(cfg)))
        if cfg.netdisco_enabled:
            tasks.append(asyncio.create_task(_netdisco_loop(cfg)))
            tasks.append(asyncio.create_task(_netdisco_classify_loop(cfg)))
            tasks.append(asyncio.create_task(_netdisco_topology_loop(cfg)))
            tasks.append(asyncio.create_task(_netdisco_reachability_loop(cfg)))
            # active scan is double-gated: netdisco on AND the active_scan stop-gate
            if cfg.netdisco_config().active_scan:
                tasks.append(asyncio.create_task(_netdisco_discovery_loop(cfg)))
            # passive de-anon is double-gated: netdisco on AND passive_enabled
            if cfg.netdisco_config().passive_enabled:
                tasks.append(asyncio.create_task(_netdisco_passive_loop(cfg)))
            # optional adapters run only when the operator has configured at least one
            if cfg.netdisco_config().optional_adapters:
                tasks.append(asyncio.create_task(_netdisco_adapter_loop(cfg)))
        # W4.0: RescoreQueue is a threading.Thread worker, not an asyncio.Task --
        # it is started/stopped on its own lifecycle below, never added to `tasks`
        # (which the finally block cancels+awaits as asyncio tasks).
        if cfg.async_rescore:
            from server.pipeline import recompute_scores, set_rescore_queue
            from server.rescore_queue import RescoreQueue

            rescore_queue = RescoreQueue(recompute_scores)
            rescore_queue.start()
            set_rescore_queue(rescore_queue)
            app.state.rescore_queue = rescore_queue
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # shutdown: дать воркеру дожевать очередь и погаснуть
            queue = getattr(app.state, "rescore_queue", None)
            if queue is not None:
                queue.drain(5.0)
                queue.stop()
                set_rescore_queue(None)

    app = FastAPI(
        title="SRP — раннее предупреждение отказов",
        lifespan=lifespan,
    )
    app.state.ingest_token = cfg.ingest_token  # "" = ingest auth disabled (MVP default)
    if not cfg.ingest_token:
        # P0-2 (stoperrors.md): loud, not silent -- the deployed default accepts
        # telemetry AND honours delete/purge/update from anyone on the LAN. This
        # does not change the default itself (would break already-deployed fleets
        # whose client/config.json has no token); it only makes the gap visible.
        _authlog.warning(
            "ingest_token не задан — сервер принимает телеметрию и "
            "административные запросы (delete/purge/update) БЕЗ аутентификации. "
            "Задайте ingest_token в server/config.json, чтобы включить проверку."
        )
    # P0-4 (stoperrors.md): a dedicated signing key for update manifests, separate
    # from ingest_token -- ingest_token also rides a plaintext bearer header on
    # every request, so reusing it as the HMAC key let a passive LAN eavesdropper
    # forge a signed update. Falls back to ingest_token (less safe) when unset.
    app.state.update_hmac_secret = cfg.update_hmac_secret or cfg.ingest_token
    # Separate flag (not "not app.state.update_hmac_secret" -- the fallback already
    # collapsed it to a non-empty value): the dashboard banner needs to know WHY a
    # secret is present, same visibility parity as P0-2's ingest_token banner.
    app.state.update_hmac_fallback = not cfg.update_hmac_secret and bool(cfg.ingest_token)
    if app.state.update_hmac_fallback:
        _authlog.warning(
            "update_hmac_secret не задан, используется ingest_token — менее "
            "безопасно, т.к. этот токен также передаётся как bearer-заголовок."
        )
    app.state.updates_dir = cfg.resolved_updates_dir()  # agent auto-update package drop
    app.state.printer_config = cfg.printer_config()  # for the /printers/poll force button
    app.state.netdisco_config = cfg.netdisco_config()  # for the /discovery/poll force button
    # Ф3: the unified network-map graph cache is created up-front (the handler no
    # longer does the P11-LOW lazy-init on every read). The graph itself still loads
    # on the first read (within the TTL) so a cold start never blocks on it.
    app.state.network_map_cache = GraphCache()
    app.add_middleware(_IngestBodySizeMiddleware)
    app.include_router(api_router)
    app.include_router(web_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
