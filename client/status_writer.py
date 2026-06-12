"""Local ``status.json`` for the tray process (tray spec §1).

One-way IPC: the agent (SYSTEM) atomically rewrites ``status.json`` next to its
offline buffer every cycle; the per-user tray only reads it. The document
deliberately carries NO secrets -- no ingest token, no password hash, no URL
userinfo -- the tray needs none of them (pinned by tests). Pure stdlib.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import socket
import time
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional

from client.collectors.print_jobs import read_print_counters
from client.config import ClientConfig
from client.transport import AGENT_VERSION

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# LAN IP discovery
# --------------------------------------------------------------------------- #


def filter_lan_ips(candidates: Iterable[str]) -> list[str]:
    """Keep RFC1918 IPv4 addresses only, order-preserving dedup.

    Loopback, link-local, public and IPv6 addresses are dropped: the panel
    shows the address a colleague/helpdesk can actually reach on the LAN.
    """
    out: list[str] = []
    for raw in candidates:
        try:
            ip = ipaddress.ip_address(str(raw).strip())
        except ValueError:
            continue
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
            continue
        text = str(ip)
        if text not in out:
            out.append(text)
    return out


def candidate_ips(server_url: str) -> list[str]:
    """Best-effort LAN IP discovery; never raises (offline boxes included).

    Primary: UDP-connect toward the server host -- the OS picks the outbound
    interface without sending a packet. Fallback: resolve the local hostname.
    """
    found: list[str] = []
    host = urllib.parse.urlsplit(server_url).hostname if server_url else None
    if host:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect((host, 9))
                found.append(probe.getsockname()[0])
            finally:
                probe.close()
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass
    return filter_lan_ips(found)


# --------------------------------------------------------------------------- #
# Environment probes (never raise)
# --------------------------------------------------------------------------- #


def _disk_free_gb() -> Optional[float]:
    """Free space on the system drive, in GiB (1 decimal)."""
    root = os.environ.get("SYSTEMDRIVE", "")
    path = root + "\\" if root else os.path.abspath(os.sep)
    try:
        return round(shutil.disk_usage(path).free / 2**30, 1)
    except OSError:
        return None


def _uptime_days() -> Optional[float]:
    """Days since boot via GetTickCount64; None off-Windows."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Default restype is c_int: a 64-bit tick count would truncate after
        # ~24.8 days of uptime -- exactly the machines we care about.
        kernel32.GetTickCount64.restype = ctypes.c_uint64
        return round(kernel32.GetTickCount64() / 86_400_000.0, 1)
    except (ImportError, AttributeError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Document assembly + atomic write
# --------------------------------------------------------------------------- #


def build_status(
    *,
    cfg: ClientConfig,
    now: float,
    hostname: str,
    ips: list[str],
    last_ok_ts: Optional[float],
    last_error: str,
    buffer_depth: int,
    print_counters: dict[str, Any],
    disk_free_gb: Optional[float],
    uptime_days: Optional[float],
) -> dict[str, Any]:
    """Assemble the status document (pure; the full field set of spec §1)."""
    return {
        "ts": int(now),
        "agent_version": AGENT_VERSION,
        "last_send_ok_ts": int(last_ok_ts) if last_ok_ts is not None else None,
        "last_send_error": last_error,
        "buffer_depth": buffer_depth,
        "hostname": hostname,
        "ips": list(ips),
        "org_code": cfg.org_code or "",
        "dept_code": cfg.dept_code or "",
        "print_today_pages": int(print_counters.get("today", 0)),
        "print_month_pages": int(print_counters.get("month", 0)),
        "print_mode": str(print_counters.get("mode", "events")),
        "disk_free_gb": disk_free_gb,
        "uptime_days": uptime_days,
    }


def write_status(path: Path, doc: dict[str, Any]) -> None:
    """Atomic write (tmp + ``os.replace``). I/O failure is logged, never raised."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("status.json not written (%s): %s", path, exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def publish_status(cfg: ClientConfig, transport: Any, print_state_path: Path) -> None:
    """Gather, build, write. Crash-proof: any failure logs and returns."""
    try:
        counters = read_print_counters(print_state_path, today=date.today())
        doc = build_status(
            cfg=cfg,
            now=time.time(),
            hostname=socket.gethostname(),
            ips=candidate_ips(cfg.server_url),
            last_ok_ts=getattr(transport, "last_ok_ts", None),
            last_error=getattr(transport, "last_error", ""),
            buffer_depth=transport.buffer_depth() if hasattr(transport, "buffer_depth") else 0,
            print_counters=counters,
            disk_free_gb=_disk_free_gb(),
            uptime_days=_uptime_days(),
        )
        write_status(cfg.resolved_buffer_path().with_name("status.json"), doc)
    except Exception:  # noqa: BLE001 -- status publishing must never kill the agent loop
        log.exception("publish_status failed")
