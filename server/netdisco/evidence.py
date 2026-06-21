"""Phase 8 -- collect L2 link evidence from neighbour MIBs (read-only SNMP).

A physical "port <-> port" link is never a bare fact; it is the *winner* among
competing pieces of evidence (LLDP > CDP > FDB inference), exactly mirroring the SRP
trust core's "collector |= semantic, UNKNOWN over false confidence". This module
turns three MIBs into immutable ``LinkEvidence``:

* **LLDP** (``lldpRemTable``) -- standards-based, authoritative neighbour.
* **CDP** (``cdpCacheTable``) -- Cisco neighbour, high authority.
* **bridge FDB** (``dot1dTpFdbPort`` + ``dot1dBasePortIfIndex``) -- raw "which MAC
  behind which port", fed to the §4.3 inference in :mod:`server.netdisco.l2`.

All parsing is fail-closed: a malformed/foreign/empty row is skipped, never raised.
A 6-octet chassis-id renders as a normalised MAC so the same neighbour seen over
LLDP and FDB merges in fusion (P9). Reconciliation/timestamps live downstream; the
collectors are pure functions of the SNMP answer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Protocol, Set, Tuple

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.models import NetDevice
from server.netdisco.snmp_probe import _mac

# Evidence source tags (also the fusion priority keys in P9, §4.4).
SOURCE_LLDP = "lldp"
SOURCE_CDP = "cdp"
SOURCE_FDB_EDGE = "fdb_edge"
SOURCE_FDB_UPLINK = "fdb_uplink"
SOURCE_FDB_AMBIGUOUS = "fdb_ambiguous"

# Confidence bands (English machine values -- pinned by tests, never localized).
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

_MAC_OCTETS = 6
_LLDP_IDX_MIN = 3  # TimeMark.LocalPortNum.RemIndex
_CDP_IDX_MIN = 2  # cdpCacheIfIndex.cdpCacheDeviceIndex
_OCTET_MAX = 255


class Session(Protocol):
    def walk(self, base_oid: str, *, max_rows: int = 512) -> Dict[str, object]: ...


@dataclass(frozen=True)
class LinkEvidence:
    """One competing claim that node ``a`` links to node ``b``.

    ``a`` is the local node hint (the probed device's nid); ``b`` is a remote hint
    (a normalised MAC, an LLDP/CDP device id). Fusion (P9) normalises both to stable
    node-ids and picks a winner by ``source`` priority + freshness. ``observed_at``
    is stamped by the topology cycle, not here (the collectors stay deterministic)."""

    a: str
    b: str
    source: str
    confidence: str
    local_if: Optional[int] = None
    observed_at: Optional[str] = None


def _opt_int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _suffix_parts(oid: str, base: str) -> Optional[List[str]]:
    """OID suffix under ``base.`` as dotted parts, or None if not under the base."""
    prefix = base + "."
    if not oid.startswith(prefix):
        return None
    return oid[len(prefix) :].split(".")


def _device_id_text(value: object) -> Optional[str]:
    """A printable ASCII neighbour id (CDP/LLDP hostname), else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text and text.isascii() and text.isprintable():
        return text
    return None


def _chassis_id(value: object) -> Optional[str]:
    """LLDP chassis-id -> normalised MAC (6-octet subtype) or printable text id."""
    mac = _mac(value)  # 6 raw octets -> AA-BB-... ; utf-8-mangled -> None
    if mac is not None:
        return mac
    return _device_id_text(value)


def _mac_from_oid_octets(parts: List[str]) -> Optional[str]:
    """6 decimal OID-suffix octets (the FDB index MAC) -> normalised MAC, else None."""
    if len(parts) != _MAC_OCTETS:
        return None
    octets = []
    for part in parts:
        if not part.isdigit():
            return None
        num = int(part)
        if num > _OCTET_MAX:
            return None
        octets.append(f"{num:02x}")
    return normalize_mac(":".join(octets))


def collect_lldp(local: str, session: Session) -> List[LinkEvidence]:
    """Walk lldpRemChassisId -> authoritative neighbour evidence (HIGH).

    The local port number rides in the OID index (TimeMark.LocalPortNum.RemIndex)."""
    out: List[LinkEvidence] = []
    for oid, value in session.walk(oids.LLDP_REM_CHASSIS_ID).items():
        parts = _suffix_parts(oid, oids.LLDP_REM_CHASSIS_ID)
        if parts is None or len(parts) < _LLDP_IDX_MIN:
            continue
        remote = _chassis_id(value)
        if remote is None:
            continue
        local_if = int(parts[1]) if parts[1].isdigit() else None
        out.append(
            LinkEvidence(a=local, b=remote, source=SOURCE_LLDP, confidence=HIGH, local_if=local_if)
        )
    return out


def collect_cdp(local: str, session: Session) -> List[LinkEvidence]:
    """Walk cdpCacheDeviceId -> Cisco neighbour evidence (HIGH).

    The local ifIndex rides in the OID index (cdpCacheIfIndex.cdpCacheDeviceIndex)."""
    out: List[LinkEvidence] = []
    for oid, value in session.walk(oids.CDP_CACHE_DEVICE_ID).items():
        parts = _suffix_parts(oid, oids.CDP_CACHE_DEVICE_ID)
        if parts is None or len(parts) < _CDP_IDX_MIN:
            continue
        remote = _device_id_text(value)
        if remote is None:
            continue
        local_if = int(parts[0]) if parts[0].isdigit() else None
        out.append(
            LinkEvidence(a=local, b=remote, source=SOURCE_CDP, confidence=HIGH, local_if=local_if)
        )
    return out


def read_fdb(session: Session) -> Tuple[Dict[int, Set[str]], Dict[int, int]]:
    """Read the bridge forwarding DB -> ({bridge_port: {mac}}, {bridge_port: ifindex}).

    ``dot1dTpFdbPort`` indexes by the 6-octet MAC (in the OID suffix) and values the
    bridge port; ``dot1dBasePortIfIndex`` maps that bridge port to an ifTable ifIndex.
    Raw grouping only -- multicast/own-MAC filtering and edge inference live in
    :func:`server.netdisco.l2.infer_edges` (§4.3)."""
    port_macs: Dict[int, Set[str]] = defaultdict(set)
    for oid, value in session.walk(oids.DOT1D_TP_FDB_PORT).items():
        parts = _suffix_parts(oid, oids.DOT1D_TP_FDB_PORT)
        if parts is None:
            continue
        mac = _mac_from_oid_octets(parts)
        port = _opt_int(value)
        if mac is None or port is None:
            continue
        port_macs[port].add(mac)

    port_if: Dict[int, int] = {}
    for oid, value in session.walk(oids.DOT1D_BASE_PORT_IF_INDEX).items():
        parts = _suffix_parts(oid, oids.DOT1D_BASE_PORT_IF_INDEX)
        if parts is None or len(parts) != 1 or not parts[0].isdigit():
            continue
        ifindex = _opt_int(value)
        if ifindex is None:
            continue
        port_if[int(parts[0])] = ifindex

    return dict(port_macs), port_if


def _own_macs(device: NetDevice) -> FrozenSet[str]:
    """The probed switch's own MACs (primary + per-interface), normalised."""
    macs = set()
    for raw in (device.mac, *(iface.phys_mac for iface in device.interfaces)):
        norm = normalize_mac(raw)
        if norm:
            macs.add(norm)
    return frozenset(macs)


def collect_evidence(
    device: NetDevice, session: Session, *, infra_macs: FrozenSet[str] = frozenset()
) -> List[LinkEvidence]:
    """All link evidence for ``device``: LLDP + CDP neighbours + §4.3 FDB edges.

    ``infra_macs`` (known infrastructure MACs across the fleet) lets the FDB step
    tell an uplink from an edge. The switch's own MACs are filtered automatically."""
    from server.netdisco import l2  # local import: evidence <-> l2 would be cyclic

    local = device.nid
    out = collect_lldp(local, session)
    out += collect_cdp(local, session)
    port_macs, port_if = read_fdb(session)
    out += l2.infer_edges(
        local, port_macs, port_if, infra_macs=infra_macs, own_macs=_own_macs(device)
    )
    return out
