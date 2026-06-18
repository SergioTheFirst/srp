"""Silent printer discovery: union three NON-scanning sources into one
deduplicated candidate list.

Sources (all silent -- no probe packets, so no security sign-off needed):
  * agent spooler-port hints  -- ``HistoricalPayload.printer_ports`` (phase 3)
  * ARP snapshots             -- already collected by the network collector
  * the engineer's static list -- ``PrinterConfig.static_ips``

Dedup precedence is serial > MAC > IP; a serial only appears later, at SNMP probe
time, so discovery dedups on MAC (a printer that changed IP keeps one identity)
then IP. Each candidate carries which sources found it, so the phase-4 poll
scheduler can bound its SNMP fan-out by origin. Privacy: every candidate IP is
re-checked RFC1918 here (defense in depth, even though the agent already filters).
Pure: ``merge`` takes already-read inputs, so it never touches the DB/network.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Optional

_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def is_rfc1918(ip: Any) -> bool:
    """True only for a literal RFC1918 address (the spec's privacy contract)."""
    if not ip or not isinstance(ip, str):
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is None:
            return False
        addr = mapped
    return any(addr in net for net in _RFC1918)


def _norm_mac(mac: Any) -> Optional[str]:
    if not mac or not isinstance(mac, str):
        return None
    cleaned = mac.strip().upper()
    return cleaned or None


@dataclass(frozen=True)
class PrinterCandidate:
    """A host that MIGHT be a printer; classification happens at SNMP probe time."""

    ip: Optional[str]
    mac: Optional[str]
    name: Optional[str]
    sources: tuple[str, ...]


def merge(
    *,
    agent_hints: list[dict[str, Any]],
    arp_snapshots: list[dict[str, Any]],
    static_ips: tuple[str, ...],
) -> list[PrinterCandidate]:
    """Union + dedup the three discovery sources into sorted candidates."""
    by_ip: dict[str, dict[str, Any]] = {}

    def add(ip: Any, mac: Any, name: Any, source: str) -> None:
        if not is_rfc1918(ip):
            return
        key = ip.strip()
        rec = by_ip.setdefault(key, {"ip": key, "mac": None, "name": None, "sources": set()})
        rec["sources"].add(source)
        normalized_mac = _norm_mac(mac)
        if normalized_mac and not rec["mac"]:
            rec["mac"] = normalized_mac
        if name and not rec["name"]:
            rec["name"] = name

    for hint in agent_hints:
        if isinstance(hint, dict):
            add(hint.get("ip"), None, hint.get("name"), "spooler")
    for snap in arp_snapshots:
        for neighbor in snap.get("neighbors") or []:
            if isinstance(neighbor, dict):
                add(neighbor.get("ip"), neighbor.get("mac"), None, "arp")
    for ip in static_ips:
        add(ip, None, None, "config")

    return _collapse_by_mac(by_ip)


def _collapse_by_mac(by_ip: dict[str, dict[str, Any]]) -> list[PrinterCandidate]:
    """Fold IP-keyed records sharing a MAC into one (MAC outranks IP)."""
    by_mac: dict[str, list[dict[str, Any]]] = {}
    standalone: list[dict[str, Any]] = []
    for rec in by_ip.values():
        if rec["mac"]:
            by_mac.setdefault(rec["mac"], []).append(rec)
        else:
            standalone.append(rec)

    merged: list[dict[str, Any]] = list(standalone)
    for mac, recs in by_mac.items():
        recs_sorted = sorted(recs, key=lambda r: r["ip"])
        sources: set[str] = set()
        name: Optional[str] = None
        for rec in recs_sorted:
            sources |= rec["sources"]
            if name is None and rec["name"]:
                name = rec["name"]
        merged.append({"ip": recs_sorted[0]["ip"], "mac": mac, "name": name, "sources": sources})

    candidates = [
        PrinterCandidate(
            ip=rec["ip"],
            mac=rec["mac"],
            name=rec["name"],
            sources=tuple(sorted(rec["sources"])),
        )
        for rec in merged
    ]
    return sorted(candidates, key=lambda c: c.ip or "")
