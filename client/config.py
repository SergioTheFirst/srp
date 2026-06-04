"""Client/agent configuration from ``client/config.json`` with safe defaults.

The server address lives here so the same agent binary points at whatever
server the deployment uses. There is deliberately **no default** target: the
operator sets ``server_url`` at install time (a LAN server is typical; a public
address is a valid explicit choice). An unset URL is a hard error at startup --
we never silently phone home to a hard-coded host. ``device_id`` is resolved
once and persisted so a machine keeps a stable identity across agent restarts.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path

_CONFIG_PATH = Path(__file__).with_name("config.json")


class ConfigError(ValueError):
    """Raised when required client configuration is missing or invalid."""


@dataclass
class ClientConfig:
    server_url: str = ""  # required: operator sets this per deployment (no default)
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
    ingest_token: str = ""  # nosec B105 -- empty = no token sent; set per deployment to match server

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
    except (OSError, ImportError):
        # ImportError: winreg is absent off Windows (dev boxes) -> fall back to uuid.
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


def validate_runtime_config(cfg: ClientConfig) -> None:
    """Validate config that must be present before the agent can run.

    Called at agent startup *after* any ``--server`` override is applied, so the
    operator can supply the target via config.json or the CLI. Raises
    :class:`ConfigError` (with actionable guidance) when ``server_url`` is unset.
    """
    if not cfg.server_url.strip():
        raise ConfigError(
            'server_url is not set. Edit client/config.json and set "server_url" to your '
            "SRP server -- a LAN address is typical (e.g. http://192.168.1.10:8000) -- or pass "
            "--server URL on the command line."
        )
