"""Stable network-device identity.

Precedence chassis-id > serial > MAC > IP mirrors ``printer_identity``: the
strongest identifier that survives a DHCP lease change wins, so a renewed IP is
never read as "device disappeared + new device appeared". The MAC is normalised
through the single OUI helper (one source of truth) so a device is one identity
under any case/separator. Nothing usable -> ``nd-unknown`` (UNKNOWN over a guess).
"""

from __future__ import annotations

from typing import Optional

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
    if ip and ip.strip():
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
