"""Network collector (Phase 1): adapter/IP/Wi-Fi health, ARP neighbors,
internal-only TCP connections, and link quality. Pure stdlib.

Language independence: the PowerShell script emits only numeric values and
English enum names (Status/State); never localized text. Privacy: external
(public) addresses are dropped here, before anything is serialized.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Optional

from client.collectors.ps import as_list, run_ps
from client.collectors.sources import NETWORK, CollectorResult, failed, field_status, health

_MAX_NEIGHBORS = 256
_MAX_CONNECTIONS = 256
_BAD_MACS = {"", "00-00-00-00-00-00", "FF-FF-FF-FF-FF-FF"}
_MCAST_MAC_PREFIXES = ("01-00-5E", "33-33", "01-80-C2")


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


def _is_internal(ip: Optional[str]) -> bool:
    """True only for private LAN addresses usable as a map edge (no loopback,
    link-local, multicast, or unspecified)."""
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        a.is_private
        and not a.is_loopback
        and not a.is_link_local
        and not a.is_multicast
        and not a.is_unspecified
    )


def _clean_strs(value: Any) -> list[str]:
    return [str(x) for x in as_list(value) if x]


def _parse_adapter(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    bps = _i(raw.get("link_bps"))
    return {
        "name": (raw.get("name") or None),
        "desc": (raw.get("desc") or None),
        "mac": (raw.get("mac") or None),
        "kind": _kind(raw.get("iftype")),
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
    iftype   = [int]$a.ifType
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

$targets = @()
foreach ($a in $adapters) {
  if ($a.gateway) { $targets += [pscustomobject]@{ kind='gateway'; addr=$a.gateway } }
  foreach ($d in $a.dns) { if ($d) { $targets += [pscustomobject]@{ kind='dns'; addr=$d } } }
}
$targets = $targets | Sort-Object addr -Unique
$quality = @()
foreach ($tg in $targets) {
  $r = @(Test-Connection -ComputerName $tg.addr -Count 3 -ErrorAction SilentlyContinue)
  $recv = $r.Count
  $lat = if ($recv -gt 0) { [math]::Round((($r | Measure-Object ResponseTime -Average).Average), 1) } else { $null }
  $quality += [ordered]@{ target_kind=$tg.kind; target=$tg.addr; latency_ms=$lat;
    loss_pct=[math]::Round(((3 - $recv) / 3.0) * 100, 1); samples=3 }
}

[ordered]@{ adapters=@($adapters); neighbors=@($neighbors); connections=@($conns); quality=@($quality) } |
  ConvertTo-Json -Depth 5 -Compress
"""


def collect_network() -> CollectorResult:
    result = run_ps(_NET_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([NETWORK], status))

    d = result.data
    adapters = [a for a in (_parse_adapter(x) for x in as_list(d.get("adapters"))) if a]
    neighbors = [n for n in (_parse_neighbor(x) for x in as_list(d.get("neighbors"))) if n]
    connections = [c for c in (_parse_connection(x) for x in as_list(d.get("connections"))) if c]
    quality = [q for q in (_parse_quality(x) for x in as_list(d.get("quality"))) if q]

    payload = {
        "network_adapters": adapters,
        "network_neighbors": neighbors[:_MAX_NEIGHBORS],
        "network_connections": connections[:_MAX_CONNECTIONS],
        "network_quality": quality,
    }
    present = bool(adapters or neighbors or connections)
    return CollectorResult(payload, {NETWORK: health(field_status(present))})
