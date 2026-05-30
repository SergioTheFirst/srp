"""Client/agent configuration from ``client/config.json`` with safe defaults.

The server address lives here so the same agent binary points at whatever
server the deployment uses -- default is the production box on the global
network. ``device_id`` is resolved once and persisted so a machine keeps a
stable identity across agent restarts.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path

_CONFIG_PATH = Path(__file__).with_name("config.json")

# Default points at the production server on the global network (overridable).
_DEFAULT_SERVER_URL = "http://212.42.56.189:8000"


@dataclass
class ClientConfig:
    server_url: str = _DEFAULT_SERVER_URL
    device_id: str = ""  # resolved on first run, then persisted
    inventory_interval_sec: int = 86400  # identity changes slowly -> daily
    historical_interval_sec: int = 86400  # 30-day rollups -> daily is plenty
    heartbeat_interval_sec: int = 300  # live vitals -> every 5 min
    events_interval_sec: int = 900  # event-log sweep -> every 15 min
    http_timeout_sec: int = 15
    buffer_path: str = "buffer.jsonl"  # offline spool, relative to client/
    # Site/org identity (W1.1).  Operator sets these in config.json per deployment.
    # Empty string means "not assigned"; sent as None in the envelope so the server's
    # COALESCE keeps any previously-stored value rather than wiping it.
    site_code: str = ""
    site_name: str = ""

    def resolved_buffer_path(self) -> Path:
        p = Path(self.buffer_path)
        return p if p.is_absolute() else (_CONFIG_PATH.parent / p)


def _machine_guid() -> str | None:
    """Stable per-OS-install id from the registry (survives agent reinstalls)."""
    try:
        import winreg  # Windows-only; absent on dev boxes -> fall back to uuid

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            val, _ = winreg.QueryValueEx(k, "MachineGuid")
            return str(val).strip() or None
    except OSError:
        return None


def load_config(path: Path = _CONFIG_PATH) -> ClientConfig:
    cfg = ClientConfig()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(ClientConfig)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)

    if not cfg.device_id:
        cfg.device_id = _machine_guid() or f"agent-{uuid.uuid4().hex[:16]}"
        _persist(cfg, path)
    return cfg


def _persist(cfg: ClientConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
