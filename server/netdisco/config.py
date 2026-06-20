"""Server-side netdisco configuration (mirrors PrinterConfig).

Every value has a safe default and discovery is OFF until ``enabled`` is an
explicit ``True`` -- the same secure-default stance as printer polling and the
ingest token. Intervals are clamped to a floor so no config can make the loop
hammer the network or the server.

Active scanning (P5) is gated behind a second explicit flag, ``active_scan``
(the project stop-gate, owner-authorized 2026-06-19 for owned RFC1918 LANs).
Every scan input is hardened on load, defense-in-depth with ``scan.py``:
``scan_cidrs`` keeps only RFC1918 networks, ``static_ips`` only RFC1918 hosts,
``scan_max_hosts``/``scan_workers`` are bounded, and ``scan_ports`` are range-
checked -- a public address or oversized blast radius can never enter the scan
set however the JSON is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from server.printers.discovery import is_rfc1918, is_rfc1918_cidr

_MIN_INTERVAL_SEC = 60  # never refresh faster than this, whatever the config says
_DEFAULT_INVENTORY_INTERVAL_SEC = 900
_DEFAULT_DISCOVERY_INTERVAL_SEC = 900
_DEFAULT_JITTER_SEC = 30

_DEFAULT_SCAN_MAX_HOSTS = 4096  # hard cap on hosts enumerated per scan (anti-blast)
_DEFAULT_SCAN_WORKERS = 64
_MAX_SCAN_WORKERS = 256  # ceiling: bound the concurrent socket fan-out
# Common liveness ports (web / ssh / smb / rpc / rdp). A host answering any one
# is "alive"; classification happens later (P6). SNMP/161 is probed separately.
_DEFAULT_SCAN_PORTS: tuple[int, ...] = (22, 80, 135, 443, 445, 3389)
_PORT_MIN, _PORT_MAX = 1, 65535


@dataclass(frozen=True)
class NetdiscoConfig:
    enabled: bool = False  # OFF until explicit True (secure default)
    inventory_interval_sec: int = _DEFAULT_INVENTORY_INTERVAL_SEC
    discovery_interval_sec: int = _DEFAULT_DISCOVERY_INTERVAL_SEC
    jitter_sec: int = _DEFAULT_JITTER_SEC  # de-phase the loop (anti-thundering-herd)
    # --- active scan (P5), OFF behind its own stop-gate ---
    active_scan: bool = False  # second gate: no range scanning until explicit True
    static_ips: tuple[str, ...] = ()  # engineer's manual host list (RFC1918 only)
    scan_cidrs: tuple[str, ...] = ()  # RFC1918 ranges to scan; empty = auto local /24
    scan_max_hosts: int = _DEFAULT_SCAN_MAX_HOSTS
    scan_workers: int = _DEFAULT_SCAN_WORKERS
    scan_ports: tuple[int, ...] = field(default_factory=lambda: _DEFAULT_SCAN_PORTS)
    snmp_community: str = "public"
    snmp_version: int = 1  # on-wire code: 0=v1, 1=v2c (default v2c)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x]


def _as_port_tuple(value: Any) -> tuple[int, ...]:
    """In-range ports only, deduped, original order kept; empty -> caller defaults."""
    if not isinstance(value, list):
        return ()
    out: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if _PORT_MIN <= port <= _PORT_MAX and port not in seen:
            seen.add(port)
            out.append(port)
    return tuple(out)


def load_netdisco_config(data: Optional[Mapping[str, Any]]) -> NetdiscoConfig:
    """Build a NetdiscoConfig from a raw mapping, clamping/filtering unsafe input."""
    d = data or {}
    interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("inventory_interval_sec"), _DEFAULT_INVENTORY_INTERVAL_SEC),
    )
    discovery_interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("discovery_interval_sec"), _DEFAULT_DISCOVERY_INTERVAL_SEC),
    )
    jitter = max(0, _as_int(d.get("jitter_sec"), _DEFAULT_JITTER_SEC))
    static = tuple(ip for ip in _as_str_list(d.get("static_ips")) if is_rfc1918(ip))
    scan_cidrs = tuple(c for c in _as_str_list(d.get("scan_cidrs")) if is_rfc1918_cidr(c))
    scan_max_hosts = max(0, _as_int(d.get("scan_max_hosts"), _DEFAULT_SCAN_MAX_HOSTS))
    scan_workers = min(
        _MAX_SCAN_WORKERS, max(1, _as_int(d.get("scan_workers"), _DEFAULT_SCAN_WORKERS))
    )
    scan_ports = _as_port_tuple(d.get("scan_ports")) or _DEFAULT_SCAN_PORTS
    community = d.get("snmp_community")
    community = community if isinstance(community, str) and community else "public"
    version = _as_int(d.get("snmp_version"), 1)
    version = version if version in (0, 1) else 1
    return NetdiscoConfig(
        enabled=d.get("enabled") is True,
        inventory_interval_sec=interval,
        discovery_interval_sec=discovery_interval,
        jitter_sec=jitter,
        active_scan=d.get("active_scan") is True,
        static_ips=static,
        scan_cidrs=scan_cidrs,
        scan_max_hosts=scan_max_hosts,
        scan_workers=scan_workers,
        scan_ports=scan_ports,
        snmp_community=community,
        snmp_version=version,
    )
