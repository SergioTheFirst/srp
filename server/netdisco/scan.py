"""Phase 5 -- generalized active LAN-segment scan (behind ``active_scan``).

Generalizes ``printers/scan.py`` from printer-specific ports to a configurable
liveness sweep that finds *candidate* hosts on the owned segment; later phases
(P6 SNMP probe / classify) decide what each one is. The owner lifted the active-
scan stop-gate in writing for owned RFC1918 LANs (memory
printer-active-scan-authorized); this is the netdisco generalization of that.

Hard safety rails (enforced here, not just by config):
  * RFC1918 ONLY -- the range is RFC1918-checked on config load AND every
    enumerated host is re-checked; a public address can never be probed.
  * Bounded blast radius -- hosts capped (``scan_max_hosts``, ``0`` = kill-
    switch), worker fan-out capped (``scan_workers``), per-probe timeouts short.
  * Read-only -- a TCP connect (no payload) then at most one read SNMP GET
    (sysObjectID); never a SET.
  * OFF by default -- returns ``[]`` unless ``NetdiscoConfig.active_scan`` is True.

The RFC1918 host enumeration is shared with the printer scanner
(``printers.scan.expand_cidrs``/``local_cidrs``) so the range logic has one
source of truth. stdlib-only (socket / concurrent.futures).
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Sequence

from server.netdisco.config import NetdiscoConfig
from server.netdisco.credentials import default_store, resolve_community
from server.printers import oids, snmp
from server.printers.discovery import is_rfc1918
from server.printers.scan import expand_cidrs, local_cidrs

_TCP_TIMEOUT = 0.4
_SNMP_TIMEOUT = 0.5


def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def host_is_alive(
    ip: str,
    *,
    ports: Sequence[int],
    community: str = "public",
    version: int = 1,
    tcp_timeout: float = _TCP_TIMEOUT,
    snmp_timeout: float = _SNMP_TIMEOUT,
) -> bool:
    """True if *ip* answers on any liveness port (defense-in-depth RFC1918).

    Cheap TCP connects first; only if all are closed do we try a single read SNMP
    GET (sysObjectID) on 161. A non-empty SNMP reply means SNMP-capable -- the
    classify phase later decides what kind of device it is.
    """
    if not is_rfc1918(ip):
        return False
    for port in ports:
        if _tcp_open(ip, port, tcp_timeout):
            return True
    reply = snmp.snmp_get(
        ip,
        [oids.STANDARD["sys_object_id"]],
        community=community,
        version=version,
        timeout=snmp_timeout,
        retries=0,
    )
    return bool(reply)


def scan(
    cfg: NetdiscoConfig,
    *,
    host_check: Optional[Callable[[str], bool]] = None,
    max_workers: Optional[int] = None,
) -> List[str]:
    """Return RFC1918 IPs that look alive. ``[]`` unless ``cfg.active_scan`` is True.

    ``host_check`` is injectable so tests exercise the enumeration/concurrency
    without touching the network.
    """
    if not cfg.active_scan:
        return []
    cidrs = list(cfg.scan_cidrs) or local_cidrs()
    hosts = expand_cidrs(cidrs, cfg.scan_max_hosts)  # RFC1918 + host-cap enforced here
    if not hosts:
        return []

    community = resolve_community(cfg, store=default_store())

    def default_check(ip: str) -> bool:
        try:
            return host_is_alive(
                ip,
                ports=cfg.scan_ports,
                community=community,
                version=cfg.snmp_version,
            )
        except Exception:  # noqa: BLE001 -- one bad host must never abort the whole scan
            return False

    check = host_check or default_check
    workers = min(max_workers or cfg.scan_workers, len(hosts))
    found: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ip, ok in zip(hosts, pool.map(check, hosts)):
            if ok:
                found.append(ip)
    return found
