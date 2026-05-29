"""SRP server entrypoint: assemble the FastAPI app and run it.

    python -m server.main

Binds host/port from ``server/config.json`` (default 0.0.0.0:8000 so the whole
fleet can reach it). The DB is initialized once on startup from the same config.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from server import db
from server.api import router as api_router
from server.config import ServerConfig, load_config
from server.web.dashboard import router as web_router


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
