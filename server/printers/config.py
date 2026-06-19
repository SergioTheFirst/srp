"""Server-side printer-monitoring config (the phase-4 poll scheduler reads this).

Every value has a safe default. Active scan is OFF and only an explicit ``True``
enables it -- the project's stop-gate: active scanning needs written security
sign-off (it looks like an attack to EDR). Static IPs are RFC1918-filtered on load
(defense in depth -- a public IP can never enter the poll set). SNMP is read-only
v2c by default (v1 fallback); community defaults to ``public``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from server.printers.discovery import is_rfc1918, is_rfc1918_cidr

_MIN_INTERVAL_SEC = 60  # never hammer the network, whatever the config says
_DEFAULT_INTERVAL_SEC = 900


@dataclass(frozen=True)
class PrinterConfig:
    poll_interval_sec: int = _DEFAULT_INTERVAL_SEC
    snmp_community: str = "public"
    snmp_version: int = 1  # on-wire code: 0=v1, 1=v2c (default v2c)
    static_ips: tuple[str, ...] = ()
    active_scan: bool = False  # OFF until explicit True (security stop-gate)
    scan_cidrs: tuple[str, ...] = ()  # RFC1918 ranges to scan; empty = auto local /24
    scan_max_hosts: int = 4096  # hard cap on hosts enumerated per scan (anti-blast)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x]


def load_printer_config(data: Optional[Mapping[str, Any]]) -> PrinterConfig:
    """Build a PrinterConfig from a raw mapping, clamping/filtering unsafe input."""
    d = data or {}
    interval = max(_MIN_INTERVAL_SEC, _as_int(d.get("poll_interval_sec"), _DEFAULT_INTERVAL_SEC))
    community = d.get("snmp_community")
    community = community if isinstance(community, str) and community else "public"
    version = _as_int(d.get("snmp_version"), 1)
    version = version if version in (0, 1) else 1
    static = tuple(ip for ip in _as_str_list(d.get("static_ips")) if is_rfc1918(ip))
    scan_cidrs = tuple(c for c in _as_str_list(d.get("scan_cidrs")) if is_rfc1918_cidr(c))
    scan_max_hosts = max(0, _as_int(d.get("scan_max_hosts"), 4096))
    return PrinterConfig(
        poll_interval_sec=interval,
        snmp_community=community,
        snmp_version=version,
        static_ips=static,
        active_scan=d.get("active_scan") is True,
        scan_cidrs=scan_cidrs,
        scan_max_hosts=scan_max_hosts,
    )
