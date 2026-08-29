"""Stable network-device identity.

Precedence chassis-id > serial > MAC > IP mirrors ``printer_identity``: the
strongest identifier that survives a DHCP lease change wins, so a renewed IP is
never read as "device disappeared + new device appeared". The MAC is normalised
through the single OUI helper (one source of truth) so a device is one identity
under any case/separator. Nothing usable -> ``nd-unknown`` (UNKNOWN over a guess).
"""

from __future__ import annotations

import ipaddress
from typing import Any, Optional

from server.analytics.oui import normalize_mac

_TOKEN_MAX = 48
# Identifier strength: a stronger scheme may absorb a weaker record (migration),
# never the reverse. ``unknown`` is weakest so any real id replaces it.
_STRENGTH = {"chassis": 4, "sn": 3, "mac": 2, "ip": 1, "unknown": 0}


def _norm_token(raw: Optional[str]) -> Optional[str]:
    """Slugify a chassis-id / serial: alnum kept, runs of others -> single '-',
    upper-cased and length-capped. Empty / non-alnum input -> None."""
    if not raw or not raw.strip():
        return None
    slug = "".join(c if c.isalnum() else "-" for c in raw.strip().upper())
    collapsed = "-".join(part for part in slug.split("-") if part)
    return collapsed[:_TOKEN_MAX] or None


def _valid_ip(ip: str) -> bool:
    """True for a syntactically valid IPv4/IPv6 literal. The nid may become a
    graph/DB key, so a non-address string is never embedded as one."""
    try:
        ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return True


def device_nid(
    *,
    chassis_id: Optional[str] = None,
    serial: Optional[str] = None,
    mac: Optional[str] = None,
    ip: Optional[str] = None,
) -> str:
    """The stable id for a network device, by descending identifier strength."""
    token = _norm_token(chassis_id)
    if token:
        return "nd-chassis-" + token
    token = _norm_token(serial)
    if token:
        return "nd-sn-" + token
    norm_mac = normalize_mac(mac)
    if norm_mac:
        return "nd-mac-" + norm_mac
    if ip and _valid_ip(ip):
        return "nd-ip-" + ip.strip()
    return "nd-unknown"


def _scheme(nid: str) -> str:
    parts = nid.split("-", 2)
    if len(parts) >= 2 and parts[0] == "nd":
        return parts[1]
    return "unknown"


def merge_identity(old_nid: str, new_nid: str) -> str:
    """Pick the surviving id when a device is re-observed under a new identifier.

    A strictly stronger scheme wins (record migrates up); equal or weaker keeps
    the existing id so a transient weak observation never demotes a known device
    and identities do not churn.
    """
    if _STRENGTH.get(_scheme(new_nid), 0) > _STRENGTH.get(_scheme(old_nid), 0):
        return new_nid
    return old_nid


def _canon_ip(ip: Optional[str]) -> Optional[str]:
    """Canonical **private** (RFC1918/ULA) IPv4/IPv6 literal, else None.

    Canonicalising (so ``192.168.1.7`` and ``192.168.001.007`` are one key) AND
    gating on ``is_private`` keeps the printer IP-reserve join inside the LAN: a
    public address two hosts could share via NAT/routing must never forge a
    device<->printer link (defence-in-depth atop the upstream RFC1918 gate). Used
    only as the printer IP reserve (MAC-less rows)."""
    if not ip or not ip.strip():
        return None
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    return str(addr) if addr.is_private else None


def link_identities(
    net_devices: list[dict[str, Any]],
    agent_macs: dict[str, str],
    printers: list[dict[str, Any]],
) -> dict[str, dict[str, Optional[str]]]:
    """FK-link each network device to its agent (``devices``) and/or printer.

    The join key is the normalised MAC (one source of truth, ``normalize_mac``):
    a ``net_devices`` row whose MAC is a known agent adapter is that PC; a row
    whose MAC matches a printer is that printer. The IP is a reserve used ONLY
    when the network device carries no MAC (a MAC-less ARP/ip-keyed row), never to
    override or second-guess a MAC -- a shared DHCP lease must not forge a link.

    ``agent_macs`` is ``netmap.agent_mac_index`` (normalised-MAC -> device_id).
    Returns ``{device_nid: {"device_id", "printer_id"}}`` for the rows that linked
    to at least one record; unmatched rows are omitted (no empty link is written).
    """
    printer_by_mac: dict[str, str] = {}
    printer_by_ip: dict[str, str] = {}
    for prn in printers:
        pid = prn.get("printer_id")
        if not pid:
            continue
        pmac = normalize_mac(prn.get("mac"))
        if pmac:
            printer_by_mac.setdefault(pmac, pid)
        pip = _canon_ip(prn.get("ip"))
        if pip:
            printer_by_ip.setdefault(pip, pid)

    links: dict[str, dict[str, Optional[str]]] = {}
    for nd in net_devices:
        nid = nd.get("device_nid")
        if not nid or nid == "nd-unknown":  # the shared UNKNOWN bucket is never a link target
            continue
        norm_mac = normalize_mac(nd.get("mac"))
        if norm_mac:
            device_id = agent_macs.get(norm_mac)
            printer_id = printer_by_mac.get(norm_mac)
        else:  # MAC-less row -> IP reserve, printers only (agents always have a MAC)
            device_id = None
            nd_ip = _canon_ip(nd.get("ip"))
            printer_id = printer_by_ip.get(nd_ip) if nd_ip else None
        if device_id is not None or printer_id is not None:
            links[nid] = {"device_id": device_id, "printer_id": printer_id}
    return links
