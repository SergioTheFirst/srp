"""Phase 7 — active LAN-segment scan for printers (AUTHORIZED 2026-06-19).

The project's active-scan stop-gate was lifted in writing by the owner: scanning
the local segment for printers is legitimate asset discovery on an owned network
(see memory printer-active-scan-authorized). This module finds *candidate* hosts;
the collector then classifies/reads them. It is read-only and tightly bounded.

Hard safety rails (every one enforced here, not just by config):
  * RFC1918 ONLY -- the CIDR is RFC1918-checked and every enumerated host is
    re-checked; a public address can never be probed.
  * Bounded blast radius -- hosts capped (``scan_max_hosts``), parallelism capped,
    per-probe timeouts short; printer-specific ports only (9100 raw / 631 IPP /
    161 SNMP). No SET, no payload beyond a read SNMP GET / a TCP connect.
  * OFF by default -- only runs when ``PrinterConfig.active_scan is True``.

stdlib-only (socket / ipaddress / concurrent.futures) -- portable to the agent.
"""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

from server.printers import oids, snmp
from server.printers.config import PrinterConfig
from server.printers.discovery import is_rfc1918

_PRINTER_TCP_PORTS = (9100, 631)  # JetDirect/raw + IPP — printer-specific
_TCP_TIMEOUT = 0.4
_SNMP_TIMEOUT = 0.5
_DEFAULT_WORKERS = 64


def _local_ips() -> List[str]:
    """The server's own RFC1918 IPv4 addresses (best-effort, no traffic sent)."""
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # picks the default-route iface; sends nothing
            ips.add(str(s.getsockname()[0]))
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(str(info[4][0]))
    except OSError:
        pass
    return [ip for ip in ips if is_rfc1918(ip)]


def local_cidrs() -> List[str]:
    """The server's local /24 segment(s) — the default scan range ("the segment")."""
    return sorted({ip.rsplit(".", 1)[0] + ".0/24" for ip in _local_ips()})


def expand_cidrs(cidrs: List[str], max_hosts: int) -> List[str]:
    """Enumerate host IPs across *cidrs*, RFC1918-only, deduped, capped at max_hosts."""
    if max_hosts <= 0:
        return []  # 0 = kill-switch (no hosts), never "one host" off the cap check
    out: List[str] = []
    seen: set[str] = set()
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if not isinstance(net, ipaddress.IPv4Network):
            continue
        for host in net.hosts():
            ip = str(host)
            if ip in seen or not is_rfc1918(ip):
                continue
            seen.add(ip)
            out.append(ip)
            if len(out) >= max_hosts:
                return out
    return out


def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def host_is_candidate(
    ip: str,
    *,
    community: str = "public",
    version: int = 1,
    tcp_timeout: float = _TCP_TIMEOUT,
    snmp_timeout: float = _SNMP_TIMEOUT,
) -> bool:
    """True if *ip* answers on a printer-specific port (defense-in-depth RFC1918).

    Cheap TCP checks (9100/631) first; only if both are closed do we try a single
    read SNMP GET (sysObjectID) on 161. A non-empty SNMP reply means the host is
    SNMP-capable -- the collector then decides if it is really a printer.
    """
    if not is_rfc1918(ip):
        return False
    for port in _PRINTER_TCP_PORTS:
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
    printer_cfg: PrinterConfig,
    *,
    host_check: Optional[Callable[[str], bool]] = None,
    max_workers: int = _DEFAULT_WORKERS,
) -> List[str]:
    """Return RFC1918 IPs that look like printers. [] unless active_scan is True.

    ``host_check`` is injectable so tests exercise the enumeration/concurrency
    without touching the network.
    """
    if not printer_cfg.active_scan:
        return []
    cidrs = list(printer_cfg.scan_cidrs) or local_cidrs()
    hosts = expand_cidrs(cidrs, printer_cfg.scan_max_hosts)
    if not hosts:
        return []

    def default_check(ip: str) -> bool:
        try:
            return host_is_candidate(
                ip, community=printer_cfg.snmp_community, version=printer_cfg.snmp_version
            )
        except Exception:  # noqa: BLE001 -- one bad host must never abort the whole scan
            return False

    check = host_check or default_check
    found: List[str] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(hosts))) as pool:
        for ip, ok in zip(hosts, pool.map(check, hosts)):
            if ok:
                found.append(ip)
    return found
