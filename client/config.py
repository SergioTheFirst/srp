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
    hostname: str = ""  # live machine name (platform.node), refreshed each load
    inventory_interval_sec: int = 14400  # full telemetry cycle -> every 4 h
    historical_interval_sec: int = 14400  # full telemetry cycle -> every 4 h
    heartbeat_interval_sec: int = 14400  # full telemetry cycle -> every 4 h
    events_interval_sec: int = 14400  # full telemetry cycle -> every 4 h
    print_interval_sec: int = 14400  # full telemetry cycle -> every 4 h
    liveness_interval_sec: int = 300  # пинг «я жив»; offline на дашборде виден за ~10 мин
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
    helpdesk_contact: str = ""  # shown in the tray panel; e.g. "IT: 1234 / it@org"
    # Tray personal-certificate reminders (spec §3); additive, all optional.
    tray_cert_warn_days: int = 14  # start warning this many days before expiry
    tray_notify_hours: int = 4  # balloon cadence while in the warning window
    tray_require_cert: bool = False  # nag daily when no signing cert is present at all
    ingest_token: str = ""  # nosec B105 -- empty = no token sent; set per deployment
    update_hmac_secret: str = ""  # nosec B105 -- empty = fall back to ingest_token
    # Operating mode: when True the agent collects but does NOT attempt server calls.
    offline_mode: bool = False
    # Самолечение журнала печати (PrintService/Operational): SYSTEM-агент включает
    # его, если выключен. False = уважать намеренно выключенный журнал (GPO/политика).
    print_log_autoenable: bool = True
    # P2 (agent-vantage netdisco): gated active liveness sweep of the agent's own
    # LAN segment (TCP touches -> Windows ARP-resolves live hosts -> the existing
    # Get-NetNeighbor read picks them up). RFC1918-only, fixed ports, agent's own
    # /24 only -- never configurable (2026-06-11 spec G2). Owner authorized active
    # network scanning in writing 2026-06-19 "by default" for owned segments
    # (mirrors server-side printers.active_scan / netdisco.active_scan, both true
    # in shipped server/config.json); there is no separate "shipped template" for
    # client config the way there is for the server, so this dataclass default IS
    # the effective default for a freshly installed agent.
    active_scan: bool = True
    # Password protection for config changes.
    # Format: "pbkdf2:sha256:<iters>:<salt_hex>:<hash_hex>"; empty = no password.
    config_password_hash: str = ""  # nosec B105
    update_channel: str = "stable"  # "stable" | "beta" | "none"
    update_check_interval_sec: int = 3600  # период проверки обновлений

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


def _hostname() -> str:
    """Machine name -- the discriminator that splits disk-image clones apart."""
    import platform

    return platform.node().strip()


# Namespace tag: domain-separates SRP ids and versions the derivation scheme, so
# a future change to the recipe can be told apart from this one.
_DEVICE_ID_NS = "srp-device-v1"


def resolve_device_id(machine_guid: Optional[str], hostname: str) -> str:
    """Derive a globally-unique, clone-safe device id.

    Uniqueness is the HARD requirement: ``device_id`` is the server PRIMARY KEY,
    so two agents must never share one. ``MachineGuid`` is not unique (disk-image
    clones with Sysprep skipped share it) and neither is the hostname -- mass-
    imaged machines often boot with the SAME default name before rename, so a
    ``guid+hostname`` hash can still collide. We therefore fold a random per-
    install nonce into every fresh derivation: identical ``(guid, hostname)``
    pairs still get distinct ids. ``load_config`` persists the result, so the id
    is stable across restarts; a wiped/reinstalled config re-derives a new id by
    design. ``guid``/``hostname`` stay in the material for debuggability and the
    raw registry GUID never leaves the agent (it is hashed). No ``MachineGuid``
    (non-Windows / unreadable registry) -> a random per-install id.
    """
    nonce = uuid.uuid4().hex
    if not machine_guid:
        return f"agent-{nonce[:16]}"
    material = f"{_DEVICE_ID_NS}|{machine_guid.strip().lower()}|{hostname.strip().lower()}|{nonce}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"dev-{digest[:24]}"


def load_config(path: Path = _CONFIG_PATH) -> ClientConfig:
    cfg = ClientConfig()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(ClientConfig)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)

    # hostname is display/identity, not a stable key: always reflect the CURRENT
    # machine name rather than a value cached on disk, so a rename surfaces at
    # once. device_id stays persisted -- it is the server PRIMARY KEY.
    cfg.hostname = _hostname()

    if not cfg.device_id:
        cfg.device_id = resolve_device_id(_machine_guid(), _hostname())
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
