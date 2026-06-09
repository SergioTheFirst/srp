"""Client/agent configuration from ``client/config.json`` with safe defaults.

The server address lives here so the same agent binary points at whatever
server the deployment uses. There is deliberately **no default** target: the
operator sets ``server_url`` at install time (a LAN server is typical; a public
address is a valid explicit choice). An unset URL is a hard error at startup --
we never silently phone home to a hard-coded host. ``device_id`` is resolved
once and persisted so a machine keeps a stable identity across agent restarts.

Config protection
-----------------
Settings can be guarded with a password set at install time (``--setup`` mode).
The password is stored as a PBKDF2-HMAC-SHA256 token in ``config_password_hash``;
the plaintext is never written to disk. Reading the config never requires the
password -- only writing/changing it does, so the agent itself always starts.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
import sys
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

# When frozen by PyInstaller the config lives next to the .exe, not inside the bundle.
_CONFIG_PATH = (
    Path(sys.executable).parent / "config.json"
    if getattr(sys, "frozen", False)
    else Path(__file__).with_name("config.json")
)

_PBKDF2_ITERS = 260_000


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
    buffer_path: str = "buffer.jsonl"  # offline spool, relative to config dir
    # Site/org identity.  Empty string means "not assigned"; server COALESCE keeps
    # any previously-stored value rather than wiping it.
    site_code: str = ""
    site_name: str = ""
    # Extended org identity set at install time.
    org_code: str = ""  # organisation code (e.g. "ACME"); empty = not assigned
    dept_code: str = ""  # department/subdivision code; empty = not assigned
    comment: str = ""  # free-text label for this endpoint
    ingest_token: str = ""  # nosec B105 -- empty = no token sent; set per deployment
    # Operating mode: when True the agent collects but does NOT attempt server calls.
    offline_mode: bool = False
    # Password protection for config changes.
    # Format: "pbkdf2:sha256:<iters>:<salt_hex>:<hash_hex>"; empty = no password.
    config_password_hash: str = ""  # nosec B105
    # Update channel placeholder -- used by future self-update logic.
    agent_version: str = "0.0.0"
    update_channel: str = "stable"  # "stable" | "beta" | "none"

    def resolved_buffer_path(self) -> Path:
        p = Path(self.buffer_path)
        return p if p.is_absolute() else (_CONFIG_PATH.parent / p)


# ---------------------------------------------------------------------------
# Password helpers (pure stdlib -- no external deps)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return PBKDF2-HMAC-SHA256 token: ``pbkdf2:sha256:<iters>:<salt>:<hash>``."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERS)
    return f"pbkdf2:sha256:{_PBKDF2_ITERS}:{salt}:{dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verify *password* against a stored PBKDF2 token."""
    try:
        _, algo, iters_s, salt, expected = stored_hash.split(":")
        dk = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), bytes.fromhex(salt), int(iters_s))
        return _hmac.compare_digest(dk.hex(), expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Load / persist
# ---------------------------------------------------------------------------


def _machine_guid() -> Optional[str]:
    """Stable per-OS-install id from the registry (survives agent reinstalls)."""
    try:
        import winreg  # Windows-only; absent on dev boxes -> fall back to uuid

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            val, _ = winreg.QueryValueEx(k, "MachineGuid")
            return str(val).strip() or None
    except (OSError, ImportError):
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


def save_config(
    cfg: ClientConfig,
    path: Path = _CONFIG_PATH,
    *,
    password: Optional[str] = None,
) -> None:
    """Persist *cfg* to disk.

    If *cfg* has a password hash, *password* must be supplied and correct --
    raises :class:`ConfigError` otherwise.  Pass ``password=None`` only when
    writing a fresh config that has no hash yet (first-run / ``--setup``).
    """
    if cfg.config_password_hash:
        if password is None:
            raise ConfigError("Config is password-protected. Supply the password via --password.")
        if not verify_password(password, cfg.config_password_hash):
            raise ConfigError("Wrong password -- config was not saved.")
    _persist(cfg, path)


# ---------------------------------------------------------------------------
# Runtime validation
# ---------------------------------------------------------------------------


def validate_runtime_config(cfg: ClientConfig) -> None:
    """Validate config that must be present before the agent can run.

    Called at agent startup *after* any ``--server`` override is applied.
    Offline mode skips the server_url requirement -- the agent will buffer
    data locally and never attempt network calls.
    """
    if cfg.offline_mode:
        return  # offline: no server needed
    if not cfg.server_url.strip():
        raise ConfigError(
            'server_url is not set. Edit client/config.json and set "server_url" to your '
            "SRP server -- a LAN address is typical (e.g. http://192.168.1.10:8000) -- or "
            "pass --server URL on the command line.  To run without a server use offline_mode=true."
        )
