"""SRP server entrypoint: assemble the FastAPI app and run it.

    python -m server.main

Binds host/port from ``server/config.json`` (default 0.0.0.0:8000 so the whole
fleet can reach it). The DB is initialized once on startup from the same config.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from server import db
from server.api import router as api_router
from server.config import ServerConfig, load_config
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


def create_app(cfg: ServerConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_db(
            cfg.resolved_db_path(),
            retain_heartbeats=cfg.retain_heartbeats,
            retain_events=cfg.retain_events,
        )
        yield

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
