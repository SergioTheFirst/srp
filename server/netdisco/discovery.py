"""Phase 5 -- netdisco candidate gathering (generalizes printers.discovery.merge).

Unions the non-agent discovery sources into one deduplicated candidate list:
ARP neighbours (already collected), the engineer's static list, and active-scan
hits (P5). Dedup precedence is MAC > IP (a host that changed IP keeps one
identity); every candidate IP is RFC1918-rechecked. The heavy lifting is the
already-tested printer ``merge`` (with no spooler/agent hints -- that source is
printer-specific); this module just renames the result to a network candidate so
netdisco stays self-contained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from server.printers import discovery as _printer_discovery


@dataclass(frozen=True)
class NetCandidate:
    """A host that MIGHT be a network device; classification happens at probe time."""

    ip: Optional[str]
    mac: Optional[str]
    name: Optional[str]
    sources: tuple[str, ...]


def gather_candidates(
    *,
    arp_snapshots: list[dict[str, Any]],
    static_ips: Sequence[str] = (),
    scan_ips: Sequence[str] = (),
) -> list[NetCandidate]:
    """Union + dedup ARP / static / scan sources into network candidates."""
    merged = _printer_discovery.merge(
        agent_hints=[],  # spooler hints are printer-specific; netdisco has none
        arp_snapshots=arp_snapshots,
        static_ips=tuple(static_ips),
        scan_ips=tuple(scan_ips),
    )
    return [NetCandidate(ip=c.ip, mac=c.mac, name=c.name, sources=c.sources) for c in merged]
