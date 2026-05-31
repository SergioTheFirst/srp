"""Server configuration loaded from ``server/config.json`` with safe defaults."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

_CONFIG_PATH = Path(__file__).with_name("config.json")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ServerConfig:
    # B104: bind all interfaces on purpose so the whole fleet can reach the server.
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8000
    db_path: str = "srp.db"  # relative -> resolved against project root
    retain_heartbeats: int = 500  # per device, keep last N (MVP cap)
    retain_events: int = 1000  # per device
    # B105: not a secret literal -- empty = ingest auth OFF; real token set via config.json/env.
    ingest_token: str = ""  # nosec B105

    def resolved_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (_PROJECT_ROOT / p)


def load_config(path: Path = _CONFIG_PATH) -> ServerConfig:
    cfg = ServerConfig()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(ServerConfig)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
    return cfg
