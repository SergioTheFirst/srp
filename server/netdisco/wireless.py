"""Ф7 T6 -- real wireless client<->AP association edges from a WLC (read-only SNMP).

LLDP/CDP/FDB see wired neighbours; a wireless client is invisible to them. A
wireless controller, though, holds the live association table (which client MAC is
on which AP MAC). Walking it turns "an endpoint somewhere" into a real
``client -> AP`` edge with ``medium=wireless`` -- the headline Ф7 map win.

Fail-closed by design: only a device whose ``sysObjectID`` is under a *confirmed*
wireless-controller enterprise root (AIRESPACE / Aruba / MikroTik) is walked, on a
strict dot-boundary, so a generic host that happens to answer SNMP is never
mistaken for a WLC and never has these vendor tables swept. The client and AP MAC
columns are joined by their shared table index; a MAC that arrives as raw octets or
as text is parsed defensively, and an unpaired/garbage row is dropped, never raised.
Pure function of the SNMP answer (timestamps/reconcile live downstream).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.evidence import HIGH, SOURCE_WIRELESS, LinkEvidence
from server.netdisco.snmp_probe import Session, _mac

# Bound the association walk per column: a large controller can hold 1000+ clients,
# so this is higher than the small presence-probe caps, but still a hard ceiling so a
# slow/hostile controller cannot make one cycle walk unbounded (anti-blast).
_ASSOC_PROBE_ROWS = 2048

# (enterprise root, client-MAC column, serving-AP-MAC column). Only these confirmed
# WLC roots are ever walked -- vendor differences are absorbed by joining the two
# columns on their shared table index, so the same join works for all three.
_WLC_TABLES: Tuple[Tuple[str, str, str], ...] = (
    (oids.WLC_ROOT_AIRESPACE, oids.BSN_MOBILE_STATION_MAC, oids.BSN_MOBILE_STATION_AP_MAC),
    (oids.WLC_ROOT_ARUBA, oids.ARUBA_USER_STA_MAC, oids.ARUBA_USER_AP_MAC),
    (oids.WLC_ROOT_MIKROTIK, oids.MTXR_WL_REG_CLIENT_MAC, oids.MTXR_WL_REG_AP_MAC),
)


def _mac_value(value: object) -> Optional[str]:
    """A MAC column value -> normalised MAC. Controllers return it either as 6 raw
    octets (``_mac``) or as a text address (``normalize_mac``); anything else -> None."""
    raw = _mac(value)
    if raw is not None:
        return raw
    return normalize_mac(value) if isinstance(value, str) else None


def _index_macs(session: Session, column_oid: str) -> Dict[str, str]:
    """Walk one MAC column -> {table-index suffix: normalised MAC}. Rows whose value
    is not a parseable MAC are dropped (UNKNOWN over a fabricated address)."""
    prefix = column_oid + "."
    out: Dict[str, str] = {}
    for oid, value in session.walk(column_oid, max_rows=_ASSOC_PROBE_ROWS).items():
        if not oid.startswith(prefix):
            continue
        mac = _mac_value(value)
        if mac is not None:
            out[oid[len(prefix) :]] = mac
    return out


def _walk_assoc(session: Session, client_oid: str, ap_oid: str) -> List[LinkEvidence]:
    """Join the client + AP MAC columns on their shared index -> wireless edges."""
    clients = _index_macs(session, client_oid)
    aps = _index_macs(session, ap_oid)
    out: List[LinkEvidence] = []
    seen: set = set()
    for idx in sorted(set(clients) & set(aps)):
        client_mac, ap_mac = clients[idx], aps[idx]
        if client_mac == ap_mac:
            continue  # degenerate self-row -- never an edge
        key = (client_mac, ap_mac)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            LinkEvidence(
                a=client_mac,
                b=ap_mac,
                source=SOURCE_WIRELESS,
                confidence=HIGH,
                medium="wireless",
            )
        )
    return out


def collect_wireless(session: Session, *, sys_object_id: Optional[str]) -> List[LinkEvidence]:
    """Real client<->AP association edges, or ``[]`` for a non-WLC device.

    The ``sysObjectID`` is matched against the confirmed WLC roots on a strict dot
    boundary (so ``141790`` never matches the ``14179`` root); a non-match walks
    nothing. The first matching vendor's association table is read and joined."""
    if not sys_object_id:
        return []
    for root, client_oid, ap_oid in _WLC_TABLES:
        if sys_object_id == root or sys_object_id.startswith(root + "."):
            return _walk_assoc(session, client_oid, ap_oid)
    return []
