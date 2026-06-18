"""Printer-port discovery hints: the agent reads its own spooler configuration
(``Get-Printer`` / ``Get-PrinterPort``) to learn which network printers it prints
to. This is a *discovery seed* (an IP the server may poll later), NOT trust or
scoring telemetry, and NOT an active scan -- it reads local print config.

Privacy (spec): ONLY RFC1918 printer host addresses leave the agent; hostnames
and public IPs are dropped. Language independence: only the structured IP is
parsed; printer names pass through as opaque labels (never enum/locale-parsed).
Because the hint is informational, the collector emits NO ``source_health`` entry
-- ``printer_ports`` must never become a trust domain (it would penalise older
agents' observability score for a non-health signal). Pure stdlib; WinPS 5.1
(``Get-Printer``/``Get-PrinterPort`` ship in the built-in PrintManagement module).
"""

from __future__ import annotations

import ipaddress
from typing import Any, Optional

from client.collectors.ps import as_list, run_ps
from client.collectors.sources import CollectorResult

# Stay <= the contract caps (shared/schema.py PRINTER_PORTS_MAX and the
# PrinterPortHint.name max_length): a compliant agent can never be 422'd by its
# own server -- a single pathological printer name must not reject the whole
# historical envelope (which would drop that sweep's storage/battery/network too).
_MAX_HINTS = 256
_MAX_NAME_LEN = 256

# Privacy contract: only RFC1918 LAN addresses may leave the agent. Mirrors the
# network collector's filter (client/collectors/network.py::_is_internal); kept
# local so this stdlib collector has no cross-collector coupling.
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918(host: Optional[str]) -> bool:
    """True only for a literal RFC1918 address. A DNS name, public IP, loopback,
    link-local or any non-literal returns False (and is therefore never sent)."""
    if not host or not isinstance(host, str):
        return False
    try:
        addr = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is None:
            return False
        addr = mapped
    return any(addr in net for net in _RFC1918)


# Get-Printer maps a printer's friendly name to its port name; Get-PrinterPort
# maps a port to its host address. Joined -> printer name + host IP. Emits only
# structured strings (names + addresses), never localized status text.
_SCRIPT = r"""
$byPort = @{}
foreach ($pr in Get-Printer -ErrorAction SilentlyContinue) {
  $byPort["$($pr.PortName)"] = "$($pr.Name)"
}
$ports = @()
foreach ($p in Get-PrinterPort -ErrorAction SilentlyContinue) {
  $addr = "$($p.PrinterHostAddress)"
  if (-not $addr) { continue }
  $nm = $byPort["$($p.Name)"]
  if (-not $nm) { $nm = "$($p.Name)" }
  $ports += [ordered]@{ name = $nm; host = $addr }
}
[ordered]@{ ports = @($ports) } | ConvertTo-Json -Depth 3 -Compress
"""


def _hints_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: {"ports": [{name, host}]} -> [{name, ip}], RFC1918-only, deduped by
    IP (first name wins), capped. Non-dicts and empty/non-RFC1918 hosts dropped."""
    hints: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in as_list(data.get("ports")):
        if not isinstance(raw, dict):
            continue
        host = raw.get("host")
        if not _is_rfc1918(host):
            continue
        ip = str(host).strip()  # _is_rfc1918 guaranteed a valid literal string
        if ip in seen:
            continue
        seen.add(ip)
        name = raw.get("name") or None
        if name:
            name = str(name)[:_MAX_NAME_LEN]
        hints.append({"name": name, "ip": ip})
        if len(hints) >= _MAX_HINTS:
            break
    return hints


def collect_printer_ports() -> CollectorResult:
    """Read spooler ports -> RFC1918 printer-IP hints. No source_health (the hint
    is informational; a failure yields no hints, never a false-healthy signal)."""
    res = run_ps(_SCRIPT, timeout=30)
    if res.status != "ok" or not isinstance(res.data, dict):
        return CollectorResult(None, {})
    return CollectorResult({"printer_ports": _hints_from(res.data)}, {})
