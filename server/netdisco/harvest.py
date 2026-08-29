"""Phase 7 -- passive SNMP harvest of ARP + routing tables from infra devices.

A router/switch already knows its neighbours (ARP / ipNetToMedia) and routes
(ipCidrRoute). Walking those read-only tables surfaces hosts and next-hops we have
never pinged -- new candidates and L3 evidence at no scan cost. Both functions keep
ONLY RFC1918 results (a public neighbour/next-hop is never emitted) and bound the
walk (large infra tables). MAC parsing is shared with the probe so an OCTET STRING
can never become a wrong-but-confident MAC.
"""

from __future__ import annotations

import ipaddress
from typing import List, Optional, Protocol, Tuple

from server.netdisco import oids
from server.netdisco.snmp_probe import _mac
from server.printers.discovery import is_rfc1918

# Cap the sweeps: a busy router's ARP/route tables can be large, but we only need
# a bounded sample of candidates per cycle.
_ARP_PROBE_ROWS = 4096
_ROUTE_PROBE_ROWS = 4096
_ARP_IDX_MIN = 5  # ifIndex + 4 IP octets
_ROUTE_IDX_MIN = 13  # dest(4) + mask(4) + tos(1) + nextHop(4)


class Session(Protocol):
    def walk(self, base_oid: str, *, max_rows: int = 512) -> dict: ...


def _ip_from_octets(parts: List[str]) -> Optional[str]:
    """4 dotted-decimal octet strings -> validated IPv4 string, else None."""
    if len(parts) != 4:
        return None
    try:
        return str(ipaddress.IPv4Address(".".join(parts)))
    except (ipaddress.AddressValueError, ValueError):
        return None


def _cidr(dest: List[str], mask: List[str]) -> Optional[str]:
    """dest octets + dotted mask octets -> canonical CIDR (host bits tolerated)."""
    dest_ip = _ip_from_octets(dest)
    mask_ip = _ip_from_octets(mask)
    if dest_ip is None or mask_ip is None:
        return None
    try:
        return str(ipaddress.ip_network(f"{dest_ip}/{mask_ip}", strict=False))
    except (ValueError, ipaddress.NetmaskValueError):
        return None


def harvest_arp(
    session: Session, *, max_rows: int = _ARP_PROBE_ROWS
) -> List[Tuple[str, Optional[str]]]:
    """Walk ipNetToMediaPhysAddress -> [(ip, mac)] for RFC1918 neighbours only.

    The OID suffix carries ifIndex + the IP (last 4 octets); the value carries the
    MAC. Deduped by IP; a non-private or unparseable neighbour is skipped."""
    prefix = oids.IP_NET_TO_MEDIA_PHYS + "."
    out: List[Tuple[str, Optional[str]]] = []
    seen: set = set()
    for oid, value in session.walk(oids.IP_NET_TO_MEDIA_PHYS, max_rows=max_rows).items():
        if not oid.startswith(prefix):
            continue
        parts = oid[len(prefix) :].split(".")
        if len(parts) < _ARP_IDX_MIN:
            continue
        ip = _ip_from_octets(parts[-4:])
        if ip is None or ip in seen or not is_rfc1918(ip):
            continue
        seen.add(ip)
        out.append((ip, _mac(value)))
    return out


def harvest_routes(
    session: Session, *, max_rows: int = _ROUTE_PROBE_ROWS
) -> List[Tuple[str, str, Optional[int]]]:
    """Walk ipCidrRouteIfIndex -> [(cidr, next_hop, ifindex)] for RFC1918 next-hops.

    The route index encodes dest(4).mask(4).tos(1).nextHop(4); the value is the
    ifIndex. Kept only when the next-hop is a private address (a local router we
    can reach). Deduped by (cidr, next_hop)."""
    prefix = oids.IP_CIDR_ROUTE_IF_INDEX + "."
    out: List[Tuple[str, str, Optional[int]]] = []
    seen: set = set()
    for oid, value in session.walk(oids.IP_CIDR_ROUTE_IF_INDEX, max_rows=max_rows).items():
        if not oid.startswith(prefix):
            continue
        parts = oid[len(prefix) :].split(".")
        if len(parts) < _ROUTE_IDX_MIN:
            continue
        cidr = _cidr(parts[0:4], parts[4:8])
        next_hop = _ip_from_octets(parts[9:13])
        if cidr is None or next_hop is None or not is_rfc1918(next_hop):
            continue
        key = (cidr, next_hop)
        if key in seen:
            continue
        seen.add(key)
        ifindex = value if isinstance(value, int) and not isinstance(value, bool) else None
        out.append((cidr, next_hop, ifindex))
    return out
