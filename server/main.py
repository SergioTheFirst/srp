"""SRP server entrypoint: assemble the FastAPI app and run it.

    python -m server.main

Binds host/port from ``server/config.json`` (default 0.0.0.0:8000 so the whole
fleet can reach it). The DB is initialized once on startup from the same config.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from server import db, org_directory
from server.api import router as api_router
from server.config import ServerConfig, load_config
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
                return Response("Request body too large", status_code=413)
            # Also guard chunked TE (no Content-Length header): read and cache
            # the body so pydantic can still parse it from the Starlette cache.
            if cl is None:
                body = await request.body()
                if len(body) > _MAX_INGEST_BODY_BYTES:
                    return Response("Request body too large", status_code=413)
        return await call_next(request)


_log = logging.getLogger("srp.retention")


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


async def _retention_loop(cfg: ServerConfig) -> None:
    """Re-run the retention sweep every purge_interval_hours until cancelled."""
    interval_sec = cfg.purge_interval_hours * 3600
    while True:
        await asyncio.sleep(interval_sec)
        _run_retention_sweep(cfg)  # self-guards transient errors (see above)


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


def create_app(cfg: ServerConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_db(
            cfg.resolved_db_path(),
            retain_heartbeats=cfg.retain_heartbeats,
            retain_events=cfg.retain_events,
            retain_printer_readings=cfg.retain_printer_readings,
        )
        org_directory.init_directory(cfg.resolved_org_directory_path())
        _run_retention_sweep(cfg)  # clear long-silent ghosts at startup
        tasks: list[asyncio.Task[None]] = []
        if cfg.device_retention_days > 0 and cfg.purge_interval_hours > 0:
            tasks.append(asyncio.create_task(_retention_loop(cfg)))
        if cfg.printer_poll_enabled:
            tasks.append(asyncio.create_task(_printer_poll_loop(cfg)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="SRP — раннее предупреждение отказов",
        lifespan=lifespan,
    )
    app.state.ingest_token = cfg.ingest_token  # "" = ingest auth disabled (MVP default)
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
