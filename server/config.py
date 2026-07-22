"""Server configuration loaded from ``server/config.json`` with safe defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Optional

from server.netdisco.config import NetdiscoConfig, load_netdisco_config
from server.printers.config import PrinterConfig, load_printer_config

_CONFIG_PATH = Path(__file__).with_name("config.json")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Env override for the org/department directory path (tray spec §7).
_ORG_DIRECTORY_ENV = "SRP_ORG_DIRECTORY"


@dataclass
class ServerConfig:
    # B104: bind all interfaces on purpose so the whole fleet can reach the server.
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8000
    db_path: str = "srp.db"  # relative -> resolved against project root
    retain_heartbeats: int = 500  # per device, keep last N (MVP cap)
    retain_events: int = 1000  # per device
    # Порог «offline» на дашборде, сек: 2 пропущенных liveness-пинга агента (300 с).
    stale_after_sec: int = 600
    # P2-2: per-SOURCE trust staleness re-eval -- NOT the same thing as
    # stale_after_sec above (device-level offline flag). A source whose last
    # real evidence (device_source_trust.evidence_seen_at) is older than this
    # degrades to STALE on the periodic sweep below, independent of ingest.
    source_stale_after_sec: int = 43200  # 12h ~= 3 missed 4h agent cycles
    source_stale_reeval_interval_sec: int = 3600  # 1h cadence; 0 disables the loop
    # W4.0: пересчитывать скоры в фоновом воркере, а не в HTTP-запросе ingest.
    async_rescore: bool = False
    # Device-ghost hygiene (2026-06-16): auto-delete a device after this many days
    # of silence (judged on the server-stamped last_seen). 0 disables auto-purge.
    device_retention_days: int = 30
    purge_interval_hours: int = 24  # cadence of the background retention sweep
    # ssd3 Ф5: rollup/retention (rollup_heartbeats_daily/rollup_events_daily
    # fold raw rows into heartbeat_rollup_daily/event_rollup_daily; prune_aged
    # bounds raw-row age on top of the existing per-device row caps). Reuses
    # purge_interval_hours as its cadence -- no separate loop/interval.
    heartbeat_raw_days: int = 30
    events_raw_days: int = 90
    rollup_days: int = 730
    retain_disk_readings: int = 2000
    # B105: not a secret literal -- empty = ingest auth OFF; real token set via config.json/env.
    ingest_token: str = ""  # nosec B105
    update_hmac_secret: str = ""  # nosec B105 -- empty = fall back to ingest_token
    # Org/department directory (tray spec §7); relative -> resolved against root.
    # Names are decoded render-time; nothing here is a secret.
    org_directory_path: str = "org_directory.json"
    # Network-printer monitoring (phase 4). Polling is OFF by default (secure
    # default, like ingest_token=""); the deployed config.json enables it. The
    # ``printers`` block is parsed into a PrinterConfig via printer_config().
    printer_poll_enabled: bool = False
    printers: Optional[dict[str, Any]] = None
    retain_printer_readings: int = 2000
    # Network discovery (netdisco). OFF by default (secure default, like
    # printer_poll_enabled). The ``netdisco`` block is parsed into a
    # NetdiscoConfig via netdisco_config().
    netdisco_enabled: bool = False
    netdisco: Optional[dict[str, Any]] = None
    retain_net_readings: int = 2000
    retain_net_snapshots: int = 500
    # Agent auto-update package drop: the operator copies build.bat's
    # srp-agent-update-<ver>.zip + manifest.json here. Relative -> project root.
    updates_dir: str = "server/updates"

    def resolved_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (_PROJECT_ROOT / p)

    def resolved_updates_dir(self) -> Path:
        p = Path(self.updates_dir)
        return p if p.is_absolute() else (_PROJECT_ROOT / p)

    def resolved_org_directory_path(self) -> Path:
        p = Path(self.org_directory_path)
        return p if p.is_absolute() else (_PROJECT_ROOT / p)

    def printer_config(self) -> PrinterConfig:
        """Parse the raw ``printers`` block into a validated PrinterConfig."""
        return load_printer_config(self.printers)

    def netdisco_config(self) -> NetdiscoConfig:
        """Parse the raw ``netdisco`` block into a validated NetdiscoConfig."""
        return load_netdisco_config(self.netdisco)


def load_config(path: Path = _CONFIG_PATH) -> ServerConfig:
    cfg = ServerConfig()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(ServerConfig)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
    env_dir = os.environ.get(_ORG_DIRECTORY_ENV)
    if env_dir:
        cfg.org_directory_path = env_dir
    return cfg
