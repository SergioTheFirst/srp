"""Network collector (Phase 1): adapter/IP/Wi-Fi health, ARP neighbors,
internal-only TCP connections, link quality, and (T1) internal routing-table
entries. Pure stdlib.

Language independence: the PowerShell script emits only numeric values and
English enum names (Status/State); never localized text. Privacy: external
(public) addresses are dropped here, before anything is serialized.
"""

from __future__ import annotations

import contextlib
import ipaddress
from typing import Any, Callable, Optional

from client.collectors.lan_discovery import collect_lan_discovery
from client.collectors.lan_names import resolve_netbios_names
from client.collectors.lan_scan import sweep as sweep_lan
from client.collectors.ps import as_list, run_ps
from client.collectors.sources import NETWORK, CollectorResult, failed, field_status, health

# Every cap stays <= the contract max_length (shared/schema.py NET_*_MAX):
# a compliant agent can never be 422'd by its own server.
_MAX_ADAPTERS = 64
_MAX_NEIGHBORS = 256
_MAX_CONNECTIONS = 256
_MAX_QUALITY = 16
_MAX_ROUTES = 64
_MAX_LAN_HINTS = 128
_BAD_MACS = {"", "00-00-00-00-00-00", "FF-FF-FF-FF-FF-FF"}
_MCAST_MAC_PREFIXES = ("01-00-5E", "33-33", "01-80-C2")

# Spec privacy contract: ONLY RFC1918 addresses may leave the agent.
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

# Get-NetAdapter TransmitLinkSpeed "speed unknown" driver sentinels: exact
# 32-bit all-ones, or anything implausibly large (covers the 64-bit all-ones
# sentinel, which float round-tripping in _i() shifts off its exact value).
_SPEED_UNKNOWN_32 = 0xFFFFFFFF
_SPEED_MAX_PLAUSIBLE_BPS = 1_000_000_000_000_000  # 1 Pbps


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    f = _f(v)
    return None if f is None else int(f)


def _kind(iftype: Any) -> str:
    n = _i(iftype)
    if n == 6:
        return "ethernet"
    if n == 71:
        return "wifi"
    return "other"


# T3: adapter role/tunnel classification -- pure substring match over metadata
# the agent already collects (name/desc/kind); no new probing or network I/O.
# ponytail: bare "tun"/"tap" are NOT in this list -- they are short enough to
# false-hit on ordinary words (e.g. "tun" inside "Fortune"). Real tunnel drivers
# announce themselves with longer, distinctive vendor tokens (tap-windows,
# tun2socks, wintun, wireguard...) which are already enough to cover them.
_TUNNEL_TOKENS = (
    "openvpn",
    "tap-windows",
    "wireguard",
    "tun2socks",
    "outline",
    "tailscale",
    "zerotier",
    "wintun",
    "pptp",
    "l2tp",
    "ppp",
    "wan miniport",
    "vpn",
    "data channel offload",
)
# "virtual" alone already covers "VirtualBox Host-Only ..." and "Wi-Fi Direct
# Virtual Adapter", so those don't need their own separate tokens.
_VIRTUAL_TOKENS = ("hyper-v", "vethernet", "vmware", "virtual", "loopback", "npcap", "bluetooth")


def _adapter_role(name: Optional[str], desc: Optional[str], kind: str) -> tuple[str, bool]:
    text = f"{name or ''} {desc or ''}".lower()
    if any(tok in text for tok in _TUNNEL_TOKENS):
        return "tunnel", True
    if any(tok in text for tok in _VIRTUAL_TOKENS):
        return "virtual", False
    if kind == "ethernet":
        return "lan", False
    if kind == "wifi":
        return "wifi", False
    return "other", False


def _is_internal(ip: Optional[str]) -> bool:
    """True only for RFC1918 LAN addresses (the spec's privacy contract).

    Deliberately stricter than ``ipaddress.is_private``: TEST-NET, benchmarking,
    CGNAT, loopback, link-local, multicast and broadcast all fall outside 10/8,
    172.16/12 and 192.168/16 and are never serialized. IPv6: IPv4-mapped forms
    unwrap to their v4 address; ULA/global v6 are dropped (RFC1918-only).
    Known Phase-1 gap: a connection to a public-IP SRP server is dropped too —
    treating server_url as internal needs it plumbed into the collector.
    """
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address):
        mapped = a.ipv4_mapped
        if mapped is None:
            return False
        a = mapped
    return any(a in net for net in _RFC1918)


def _is_rfc1918_cidr(cidr: Any) -> bool:
    """True only for an IPv4 network fully contained in an RFC1918 block.

    T1 route filter: a route's *destination* must itself be a private network,
    never merely a private-looking string. Mirrors
    ``server/printers/discovery.is_rfc1918_cidr`` (duplicated, not imported --
    ``client/`` stays pure stdlib with zero cross-package imports). Fail-closed
    on anything malformed or non-IPv4: a route we can't parse never leaves the
    agent.
    """
    if not cidr or not isinstance(cidr, str):
        return False
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        return False
    if not isinstance(net, ipaddress.IPv4Network):
        return False
    return any(
        isinstance(block, ipaddress.IPv4Network) and net.subnet_of(block) for block in _RFC1918
    )


def _clean_strs(value: Any) -> list[str]:
    return [str(x) for x in as_list(value) if x]


def _parse_adapter(raw: Any) -> Optional[dict[str, Any]]:
    # The adapter's own ipv4/ipv6/gateway/dns are intentionally NOT privacy-
    # filtered: this is the machine reporting its own NIC config (spec §4).
    if not isinstance(raw, dict):
        return None
    bps = _i(raw.get("link_bps"))
    if bps is not None and (bps == _SPEED_UNKNOWN_32 or bps > _SPEED_MAX_PLAUSIBLE_BPS):
        bps = None
    kind = _kind(raw.get("iftype"))
    role, tunnel = _adapter_role(raw.get("name"), raw.get("desc"), kind)
    return {
        "name": (raw.get("name") or None),
        "desc": (raw.get("desc") or None),
        "mac": (raw.get("mac") or None),
        "kind": kind,
        "role": role,
        "tunnel": tunnel,
        "up": (bool(raw.get("up")) if raw.get("up") is not None else None),
        "link_mbps": (round(bps / 1_000_000, 1) if bps else None),
        "ipv4": _clean_strs(raw.get("ipv4")),
        "ipv6": _clean_strs(raw.get("ipv6")),
        "gateway": (raw.get("gateway") or None),
        "dns": _clean_strs(raw.get("dns")),
        "dhcp": (bool(raw.get("dhcp")) if raw.get("dhcp") is not None else None),
        "ssid": (raw.get("ssid") or None),
        "signal_pct": _i(raw.get("signal_pct")),
        "channel": _i(raw.get("channel")),
    }


def _parse_neighbor(raw: Any) -> Optional[dict[str, Any]]:
    # Broadcast/multicast dropping is MAC-based (FF-FF…, 01-00-5E…): detecting
    # a subnet *directed* broadcast by IP needs the prefix length, which the
    # script does not emit — an RFC1918 .255 with a unicast MAC passes (Ф1 gap).
    if not isinstance(raw, dict):
        return None
    ip = raw.get("ip")
    if not _is_internal(ip):
        return None
    mac = (raw.get("mac") or "").upper()
    if mac in _BAD_MACS or any(mac.startswith(p) for p in _MCAST_MAC_PREFIXES):
        return None
    return {"ip": ip, "mac": raw.get("mac"), "state": (raw.get("state") or None)}


def _parse_connection(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    if not _is_internal(raw.get("remote_ip")):  # privacy: only internal peers
        return None
    return {
        "local_ip": (raw.get("local_ip") or None),
        "local_port": _i(raw.get("local_port")),
        "remote_ip": raw.get("remote_ip"),
        "remote_port": _i(raw.get("remote_port")),
        "state": (raw.get("state") or None),
    }


def _parse_quality(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {
        "target_kind": (raw.get("target_kind") or None),
        "target": (raw.get("target") or None),
        "latency_ms": _f(raw.get("latency_ms")),
        "loss_pct": _f(raw.get("loss_pct")),
        "samples": _i(raw.get("samples")),
    }


def _lan_adapter_ips(adapters: list[dict[str, Any]]) -> list[str]:
    """Adapter addresses worth joining P1's multicast listen on: real LAN/Wi-Fi
    uplinks only. A tunnel adapter can ALSO carry an RFC1918 address (e.g. an
    Outline/OpenVPN endpoint at 10.x.x.x), so ``role`` -- not RFC1918-ness --
    is the gate that keeps the join off the VPN tunnel."""
    return [
        ip
        for a in adapters
        if a.get("role") in ("lan", "wifi")
        for ip in (a.get("ipv4") or [])
        if ip
    ]


def _lan_hints(
    ips: list[str],
    collect_fn: Callable[[list[str]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Best-effort: a multicast-listen failure (blocked port, no permission)
    must never break the rest of network collection (mirrors ``_with_names``)."""
    if not ips:
        return []
    try:
        return collect_fn(ips)
    except Exception:
        return []


def _parse_route(raw: Any, gateways: set) -> Optional[dict[str, Any]]:
    """Keep a routing-table entry only when it is real inter-subnet reachability:

    dest and next_hop both RFC1918, AND next_hop is not any adapter's own default
    gateway (the default route + bogon anti-leak routes all point at the gateway
    and are already the agent-uplink edge -- feeding them would just duplicate it).
    This is the whole T1 privacy contract: nothing else ever leaves the agent.
    """
    if not isinstance(raw, dict):
        return None
    next_hop = raw.get("next_hop")
    dest = raw.get("dest")
    if not _is_internal(next_hop) or not _is_rfc1918_cidr(dest):
        return None
    if next_hop in gateways:
        return None
    return {
        "dest": dest,
        "next_hop": next_hop,
        "if_index": _i(raw.get("if_index")),
        "metric": _i(raw.get("metric")),
    }


_NET_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'

$dh = @{}
foreach ($i in Get-NetIPInterface -AddressFamily IPv4) { $dh["$($i.InterfaceIndex)"] = ($i.Dhcp -eq 'Enabled') }
$cfg = @{}
foreach ($c in Get-NetIPConfiguration) { $cfg["$($c.InterfaceIndex)"] = $c }

$adapters = @()
foreach ($a in Get-NetAdapter) {
  $c = $cfg["$($a.ifIndex)"]
  $adapters += [ordered]@{
    name     = "$($a.Name)"
    desc     = "$($a.InterfaceDescription)"
    mac      = "$($a.MacAddress)"
    iftype   = [int]$a.InterfaceType
    up       = ($a.Status -eq 'Up')
    link_bps = [int64]$a.TransmitLinkSpeed
    ipv4     = @($c.IPv4Address.IPAddress)
    ipv6     = @($c.IPv6Address.IPAddress)
    gateway  = "$($c.IPv4DefaultGateway.NextHop)"
    dns      = @($c.DNSServer | Where-Object { $_.AddressFamily -eq 2 } | ForEach-Object { $_.ServerAddresses })
    dhcp     = [bool]$dh["$($a.ifIndex)"]
  }
}

$neighbors = @()
foreach ($n in Get-NetNeighbor -AddressFamily IPv4) {
  $neighbors += [ordered]@{ ip="$($n.IPAddress)"; mac="$($n.LinkLayerAddress)"; state="$($n.State)" }
}

$conns = @()
foreach ($t in Get-NetTCPConnection) {
  $conns += [ordered]@{ local_ip="$($t.LocalAddress)"; local_port=[int]$t.LocalPort;
    remote_ip="$($t.RemoteAddress)"; remote_port=[int]$t.RemotePort; state="$($t.State)" }
}

$routes = @()
foreach ($r in Get-NetRoute -AddressFamily IPv4) {
  $routes += [ordered]@{ dest="$($r.DestinationPrefix)"; next_hop="$($r.NextHop)";
    if_index=[int]$r.InterfaceIndex; metric=[int]$r.RouteMetric }
}

$targets = @()
foreach ($a in $adapters) {
  if ($a.gateway) { $targets += [pscustomobject]@{ kind='gateway'; addr=$a.gateway } }
  foreach ($d in $a.dns) { if ($d) { $targets += [pscustomobject]@{ kind='dns'; addr=$d } } }
}
# Dedupe keeping insertion order (gateways first), cap at 4 targets: WinPS 5.1
# Test-Connection has no -TimeoutSeconds, worst case ~4s/probe * 3 * 4 = ~48s
# stays under the 60s run_ps cap. Ping only literal IPs (no name resolution).
$seen = @{}
$uniq = @()
foreach ($tg in $targets) {
  if (-not $seen["$($tg.addr)"]) { $seen["$($tg.addr)"] = $true; $uniq += $tg }
}
$targets = @($uniq | Select-Object -First 4)
$quality = @()
foreach ($tg in $targets) {
  if (-not [System.Net.IPAddress]::TryParse($tg.addr, [ref]$null)) { continue }
  $r = @(Test-Connection -ComputerName $tg.addr -Count 3 -ErrorAction SilentlyContinue)
  $recv = $r.Count
  $lat = if ($recv -gt 0) { [math]::Round((($r | Measure-Object ResponseTime -Average).Average), 1) } else { $null }
  $quality += [ordered]@{ target_kind=$tg.kind; target=$tg.addr; latency_ms=$lat;
    loss_pct=[math]::Round(((3 - $recv) / 3.0) * 100, 1); samples=3 }
}

[ordered]@{ adapters=@($adapters); neighbors=@($neighbors); connections=@($conns);
  quality=@($quality); routes=@($routes) } |
  ConvertTo-Json -Depth 5 -Compress
"""

# P2: re-reads ONLY the neighbor table (not the whole _NET_SCRIPT, whose
# Test-Connection quality block budgets up to ~48s) right after an active sweep,
# so the sweep's freshly ARP-resolved hosts land in the SAME collection cycle.
_NEIGHBOR_RESCAN_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$neighbors = @()
foreach ($n in Get-NetNeighbor -AddressFamily IPv4) {
  $neighbors += [ordered]@{ ip="$($n.IPAddress)"; mac="$($n.LinkLayerAddress)"; state="$($n.State)" }
}
[ordered]@{ neighbors=@($neighbors) } | ConvertTo-Json -Depth 5 -Compress
"""


def _rescan_neighbors(
    adapters: list[dict[str, Any]], neighbors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """P2: best-effort active sweep + an immediate Get-NetNeighbor re-read, so
    THIS collection cycle (not the next one, hours away) surfaces whatever the
    sweep ARP-resolved. Every step fails open -- a sweep or rescan problem must
    never break the rest of network collection, and must never change the
    neighbor list beyond what a normal passive pass would have produced (the
    rescan is re-run through the exact same _parse_neighbor privacy filter)."""
    with contextlib.suppress(Exception):
        sweep_lan(_lan_adapter_ips(adapters))  # a sweep failure must not skip the rescan
    try:
        rescan = run_ps(_NEIGHBOR_RESCAN_SCRIPT, timeout=15)
    except Exception:  # noqa: BLE001 -- run_ps already self-guards; belt-and-suspenders
        return neighbors
    if rescan.status != "ok" or not isinstance(rescan.data, dict):
        return neighbors
    fresh = [n for n in (_parse_neighbor(x) for x in as_list(rescan.data.get("neighbors"))) if n]
    if not fresh:
        return neighbors
    merged = {n["ip"]: n for n in neighbors}
    merged.update({n["ip"]: n for n in fresh})
    return list(merged.values())


def _with_names(
    neighbors: list[dict[str, Any]],
    resolve_names_fn: Callable[[list[str]], dict[str, str]],
) -> list[dict[str, Any]]:
    """Attach an agent-resolved NetBIOS name to each neighbor that has one.

    The agent is the only host L2-adjacent to this LAN, so it is the only
    vantage point NBNS (UDP/137) can name neighbors from -- the server can't
    reach it off-subnet. Best-effort: a resolver failure/timeout must never
    break the rest of network collection, so any exception just leaves the
    neighbor list untouched (fail-closed, mirrors resolve_netbios_names itself)."""
    ips = [n["ip"] for n in neighbors if n.get("ip")]
    if not ips:
        return neighbors
    try:
        names = resolve_names_fn(ips)
    except Exception:
        return neighbors
    if not names:
        return neighbors
    return [
        {**n, "name": names[n["ip"]], "name_source": "netbios"} if n.get("ip") in names else n
        for n in neighbors
    ]


def collect_network(active_scan: bool = False) -> CollectorResult:
    # Phase 1 runs ONE script: a policy-blocked individual cmdlet (under
    # SilentlyContinue) yields an empty block inside an "ok" result — per-block
    # partial status is deliberately deferred to Phase 2 (spec §5.4 deviation).
    result = run_ps(_NET_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([NETWORK], status))

    d = result.data
    adapters = [a for a in (_parse_adapter(x) for x in as_list(d.get("adapters"))) if a]
    neighbors = [n for n in (_parse_neighbor(x) for x in as_list(d.get("neighbors"))) if n]
    connections = [c for c in (_parse_connection(x) for x in as_list(d.get("connections"))) if c]
    quality = [q for q in (_parse_quality(x) for x in as_list(d.get("quality"))) if q]
    gateways = {a["gateway"] for a in adapters if a.get("gateway")}
    routes = [r for r in (_parse_route(x, gateways) for x in as_list(d.get("routes"))) if r]
    if active_scan:
        neighbors = _rescan_neighbors(adapters, neighbors)
    neighbors = _with_names(neighbors[:_MAX_NEIGHBORS], resolve_netbios_names)
    lan_hints = _lan_hints(_lan_adapter_ips(adapters), collect_lan_discovery)[:_MAX_LAN_HINTS]

    payload = {
        "network_adapters": adapters[:_MAX_ADAPTERS],
        "network_neighbors": neighbors,
        "network_connections": connections[:_MAX_CONNECTIONS],
        "network_quality": quality[:_MAX_QUALITY],
        "network_routes": routes[:_MAX_ROUTES],
        "lan_hints": lan_hints,
    }
    present = bool(adapters or neighbors or connections or quality or routes or lan_hints)
    return CollectorResult(payload, {NETWORK: health(field_status(present))})
