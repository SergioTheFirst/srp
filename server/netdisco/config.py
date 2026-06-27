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

from server.netdisco.adapters.base import KNOWN_ADAPTER_TYPES, AdapterConfig
from server.printers.discovery import is_rfc1918, is_rfc1918_cidr

_MIN_INTERVAL_SEC = 60  # never refresh faster than this, whatever the config says
_DEFAULT_INVENTORY_INTERVAL_SEC = 900
_DEFAULT_DISCOVERY_INTERVAL_SEC = 900
_DEFAULT_CLASSIFY_INTERVAL_SEC = 3600  # SNMP probing is rare: classify once an hour
_DEFAULT_TOPOLOGY_INTERVAL_SEC = 3600  # L2 evidence (LLDP/CDP/FDB) is rare: once an hour
_DEFAULT_REACHABILITY_INTERVAL_SEC = 600  # liveness/outage detection: every 10 min
_DEFAULT_PASSIVE_INTERVAL_SEC = 3600  # passive de-anon is rare: once an hour
_DEFAULT_ADAPTER_INTERVAL_SEC = 900  # optional controller adapters: every 15 min
_DEFAULT_JITTER_SEC = 30

# Ф8 passive identity sources. ``data`` is the offline cross-MAC/printer_ip_map
# de-anon (no network); the rest are bounded RFC1918/link-local probes.
_PASSIVE_PROTOCOLS: tuple[str, ...] = (
    "data",
    "reverse_dns",
    "mdns",
    "ssdp",
    "netbios",
    "wsd",
    "banner",
)

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
    classify_interval_sec: int = _DEFAULT_CLASSIFY_INTERVAL_SEC
    topology_interval_sec: int = _DEFAULT_TOPOLOGY_INTERVAL_SEC
    reachability_interval_sec: int = _DEFAULT_REACHABILITY_INTERVAL_SEC
    jitter_sec: int = _DEFAULT_JITTER_SEC  # de-phase the loop (anti-thundering-herd)
    # --- active scan (P5), OFF behind its own stop-gate ---
    active_scan: bool = False  # second gate: no range scanning until explicit True
    static_ips: tuple[str, ...] = ()  # engineer's manual host list (RFC1918 only)
    scan_cidrs: tuple[str, ...] = ()  # RFC1918 ranges to scan; empty = auto local /24
    scan_max_hosts: int = _DEFAULT_SCAN_MAX_HOSTS
    scan_workers: int = _DEFAULT_SCAN_WORKERS
    scan_ports: tuple[int, ...] = field(default_factory=lambda: _DEFAULT_SCAN_PORTS)
    snmp_community: str = "public"  # plaintext path; only "public" should be plaintext
    snmp_version: int = 1  # on-wire code: 0=v1, 1=v2c (default v2c)
    # Non-public community lives DPAPI-encrypted; this names it in the store
    # (see netdisco/credentials.py). Empty -> use snmp_community plaintext.
    snmp_credential_ref: str = ""
    # --- passive identification (P8), OFF by default ---
    passive_enabled: bool = False  # OFF until explicit True (secure default)
    passive_interval_sec: int = _DEFAULT_PASSIVE_INTERVAL_SEC
    passive_protocols: tuple[str, ...] = field(default_factory=lambda: _PASSIVE_PROTOCOLS)
    # --- optional Tier-3 adapters (P9), empty by default (operator opts in) ---
    optional_adapters: tuple[AdapterConfig, ...] = ()
    adapter_interval_sec: int = _DEFAULT_ADAPTER_INTERVAL_SEC


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


def _as_protocol_tuple(value: Any) -> tuple[str, ...]:
    """Known passive protocols only, deduped, original order kept; empty/garbage ->
    all known (an operator disables the whole pass via ``passive_enabled``)."""
    if not isinstance(value, list):
        return _PASSIVE_PROTOCOLS
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item)
        if name in _PASSIVE_PROTOCOLS and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out) or _PASSIVE_PROTOCOLS


def _as_adapter_tuple(value: Any) -> tuple[AdapterConfig, ...]:
    """Validated optional adapters: a known ``adapter_type`` AND an RFC1918
    ``endpoint`` only -- any unknown type, off-LAN/garbage endpoint, or non-dict
    entry is dropped (an adapter can never point off the private network however
    the JSON is written). ``tls_verify`` defaults secure (True; only explicit
    ``false`` disables)."""
    if not isinstance(value, list):
        return ()
    out: list[AdapterConfig] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        atype = str(item.get("adapter_type") or "")
        endpoint = str(item.get("endpoint") or "")
        if atype not in KNOWN_ADAPTER_TYPES or not is_rfc1918(endpoint):
            continue
        cred = item.get("credential")
        site = item.get("site_id")
        out.append(
            AdapterConfig(
                adapter_type=atype,
                endpoint=endpoint,
                credential=cred if isinstance(cred, str) else "",
                tls_verify=item.get("tls_verify") is not False,
                site_id=site if isinstance(site, str) else "",
            )
        )
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
    classify_interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("classify_interval_sec"), _DEFAULT_CLASSIFY_INTERVAL_SEC),
    )
    topology_interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("topology_interval_sec"), _DEFAULT_TOPOLOGY_INTERVAL_SEC),
    )
    reachability_interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("reachability_interval_sec"), _DEFAULT_REACHABILITY_INTERVAL_SEC),
    )
    passive_interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("passive_interval_sec"), _DEFAULT_PASSIVE_INTERVAL_SEC),
    )
    adapter_interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("adapter_interval_sec"), _DEFAULT_ADAPTER_INTERVAL_SEC),
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
    cred_ref = d.get("snmp_credential_ref")
    cred_ref = cred_ref if isinstance(cred_ref, str) else ""
    return NetdiscoConfig(
        enabled=d.get("enabled") is True,
        inventory_interval_sec=interval,
        discovery_interval_sec=discovery_interval,
        classify_interval_sec=classify_interval,
        topology_interval_sec=topology_interval,
        reachability_interval_sec=reachability_interval,
        jitter_sec=jitter,
        active_scan=d.get("active_scan") is True,
        static_ips=static,
        scan_cidrs=scan_cidrs,
        scan_max_hosts=scan_max_hosts,
        scan_workers=scan_workers,
        scan_ports=scan_ports,
        snmp_community=community,
        snmp_version=version,
        snmp_credential_ref=cred_ref,
        passive_enabled=d.get("passive_enabled") is True,
        passive_interval_sec=passive_interval,
        passive_protocols=_as_protocol_tuple(d.get("passive_protocols")),
        optional_adapters=_as_adapter_tuple(d.get("optional_adapters")),
        adapter_interval_sec=adapter_interval,
    )
