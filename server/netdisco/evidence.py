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

import ipaddress
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Dict, FrozenSet, List, Optional, Protocol, Set, Tuple

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.models import NetDevice
from server.netdisco.snmp_probe import _mac
from server.printers.discovery import is_rfc1918

# Evidence source tags (also the fusion priority keys in P9, §4.4).
SOURCE_LLDP = "lldp"
SOURCE_CDP = "cdp"
SOURCE_FDB_EDGE = "fdb_edge"
SOURCE_FDB_UPLINK = "fdb_uplink"
SOURCE_FDB_AMBIGUOUS = "fdb_ambiguous"
SOURCE_WIRELESS = "wireless"  # Ф7: real client<->AP association from a WLC (medium=wireless)

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
    # Ф7: directed port labels (``a`` is the local end), per-edge medium, and the
    # dot1q VLAN the edge carries. All UNKNOWN/None until a Ф7 collector sets them.
    a_port: Optional[str] = None
    b_port: Optional[str] = None
    medium: Optional[str] = None
    vlan: Optional[int] = None


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


def collect_lldp(
    local: str, session: Session, *, loc_ports: Optional[Dict[int, str]] = None
) -> List[LinkEvidence]:
    """Walk lldpRemChassisId -> authoritative neighbour evidence (HIGH).

    The local port number rides in the OID index (TimeMark.LocalPortNum.RemIndex).
    Ф7: ``lldpRemPortId`` shares that index, so a directed edge carries the remote
    port label (``b_port``); the local port label (``a_port``) comes from
    ``loc_ports`` (``lldp_loc_port_ifnames`` / ifXTable, keyed by local port number).
    A 6-octet/binary port-id that is not printable text is dropped to None (UNKNOWN
    over a mangled label)."""
    rem_ports: Dict[str, str] = {}
    for oid, value in session.walk(oids.LLDP_REM_PORT_ID).items():
        suffix = _suffix_parts(oid, oids.LLDP_REM_PORT_ID)
        label = _device_id_text(value)
        if suffix is not None and label is not None:
            rem_ports[".".join(suffix)] = label
    out: List[LinkEvidence] = []
    for oid, value in session.walk(oids.LLDP_REM_CHASSIS_ID).items():
        parts = _suffix_parts(oid, oids.LLDP_REM_CHASSIS_ID)
        if parts is None or len(parts) < _LLDP_IDX_MIN:
            continue
        remote = _chassis_id(value)
        if remote is None:
            continue
        local_if = int(parts[1]) if parts[1].isdigit() else None
        a_port = loc_ports.get(local_if) if (loc_ports and local_if is not None) else None
        b_port = rem_ports.get(".".join(parts))
        out.append(
            LinkEvidence(
                a=local,
                b=remote,
                source=SOURCE_LLDP,
                confidence=HIGH,
                local_if=local_if,
                a_port=a_port,
                b_port=b_port,
            )
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


# --- Ф7: LLDP port-id (directed port labels) + mgmt-addr seed + MED class -----

# Fixed index prefix BEFORE the address octets: TimeMark.LocalPortNum.RemIndex.
# AddrSubtype.AddrLen (5 parts); the IPv4 octets start at parts[5]. A valid IPv4 row
# therefore has >= 9 parts -- enforced by the ``len(octets) == 4`` check below, not by
# this prefix count alone (do not treat this constant as a full-row length).
_LLDPMAN_PREFIX_FIELDS = 5
_LLDPMAN_ADDR_SUBTYPE_IPV4 = 1


def lldp_loc_port_ifnames(session: Session) -> Dict[int, str]:
    """Ф7 T1: lldpLocPortDesc -> {local lldp port number: textual port name}.

    Lets a directed LLDP port<->port edge carry the *local* port label without
    relying on ifXTable (some switches publish lldpLocPortDesc but not ifName)."""
    out: Dict[int, str] = {}
    for oid, value in session.walk(oids.LLDP_LOC_PORT_DESC).items():
        parts = _suffix_parts(oid, oids.LLDP_LOC_PORT_DESC)
        if parts is None or len(parts) != 1 or not parts[0].isdigit():
            continue
        name = _device_id_text(value)
        if name is None:
            continue
        out[int(parts[0])] = name
    return out


def collect_lldp_mgmt(local: str, session: Session) -> List[tuple]:
    """Ф7 T2: lldpRemManAddr -> [(local, neighbour-mgmt-IP)] seed pairs.

    The management address rides in the OID index per the LldpManAddressInfo TC
    (index = TimeMark.LocalPortNum.RemIndex.AddrSubtype.AddrLen.<addr octets>).
    We seed IPv4 only (subtype 1, 4 trailing octets); a malformed/foreign index is
    skipped, never raised. The IP is parsed defensively (``ipaddress`` rejects
    anything that is not a clean dotted-quad) AND filtered to RFC1918 -- a hostile
    neighbour advertising a public mgmt address can never become a seed (fail-safe
    regardless of caller, mirroring the probe/harvest RFC1918 gates)."""
    out: List[tuple] = []
    for oid in session.walk(oids.LLDP_REM_MAN_ADDR):
        parts = _suffix_parts(oid, oids.LLDP_REM_MAN_ADDR)
        if parts is None or len(parts) < _LLDPMAN_PREFIX_FIELDS:
            continue
        try:
            subtype = int(parts[3])
        except (IndexError, ValueError):
            continue
        if subtype != _LLDPMAN_ADDR_SUBTYPE_IPV4:
            continue  # IPv4 seeds only (scope of Ф7 seed expansion)
        octets = parts[5:]
        if len(octets) != 4 or not all(p.isdigit() for p in octets):
            continue
        ip = ".".join(str(int(p)) for p in octets)
        try:
            ipaddress.ip_address(ip)  # validates; rejects overflow/junk
        except ValueError:
            continue
        if not is_rfc1918(ip):
            continue  # never seed a public address a neighbour advertised (fail-safe)
        out.append((local, ip))
    # dedup + stable order so the same answer yields the same seed set
    seen: set = set()
    uniq: List[tuple] = []
    for pair in sorted(out):
        if pair not in seen:
            seen.add(pair)
            uniq.append(pair)
    return uniq


# LLDP-MED device class -> stable subtype label. Anything not in this map stays
# UNKNOWN (a vendor-specific class is never guessed into a type).
_LLDP_MED_SUBTYPE: Dict[int, str] = {
    oids.LLDP_MED_AP: "ap",
    oids.LLDP_MED_PHONE: "phone",
    oids.LLDP_MED_SERVER: "server",
}


def collect_lldp_med(local: str, session: Session) -> Dict[int, str]:
    """Ф7 T3: lldpXmedRemDeviceClass -> {local lldp port number: subtype}.

    Keyed by the local LLDP port number (the practical "which access port advertised
    a phone/AP" question); on a managed access port there is one neighbour, so this
    matches the chassis-id seen on that port. Only the meaningful classes map to a
    subtype; a foreign/unknown class is dropped (UNKNOWN over a guess)."""
    out: Dict[int, str] = {}
    for oid, value in session.walk(oids.LLDP_XMED_REM_DEVICE_CLASS).items():
        parts = _suffix_parts(oid, oids.LLDP_XMED_REM_DEVICE_CLASS)
        if parts is None or len(parts) < _LLDP_IDX_MIN:
            continue
        cls = _opt_int(value)
        if cls is None:
            continue
        label = _LLDP_MED_SUBTYPE.get(cls)
        if label is None:
            continue
        if not parts[1].isdigit():
            continue
        out[int(parts[1])] = label
    _ = local  # accepted for signature symmetry with the other collectors
    return out


def _read_port_ifindex(session: Session) -> Dict[int, int]:
    """``dot1dBasePortIfIndex`` -> {bridge port: ifTable ifIndex}. Shared by the
    dot1d and dot1q FDB readers (a VLAN-aware bridge still exposes this map)."""
    port_if: Dict[int, int] = {}
    for oid, value in session.walk(oids.DOT1D_BASE_PORT_IF_INDEX).items():
        parts = _suffix_parts(oid, oids.DOT1D_BASE_PORT_IF_INDEX)
        if parts is None or len(parts) != 1 or not parts[0].isdigit():
            continue
        ifindex = _opt_int(value)
        if ifindex is None:
            continue
        port_if[int(parts[0])] = ifindex
    return port_if


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
        if mac is None or port is None or port < 1:  # BRIDGE-MIB ports are >= 1
            continue
        port_macs[port].add(mac)

    return dict(port_macs), _read_port_ifindex(session)


def read_fdb_dot1q(
    session: Session, *, max_rows: int = 512
) -> Tuple[Dict[int, Set[str]], Dict[str, int]]:
    """Ф7 T4: Q-BRIDGE-MIB VLAN forwarding DB -> ({bridge_port: {mac}}, {mac: vlan}).

    ``dot1qTpFdbPort`` indexes by ``dot1qFdbId(=vlan).mac6octets`` and values the
    bridge port, so each learned MAC carries its VLAN -- a VLAN-aware switch that
    returns nothing from the plain dot1d FDB is recovered here. Same raw grouping
    and fail-closed parsing as :func:`read_fdb` (a malformed/short index is skipped),
    bounded to ``max_rows`` like the dot1d walk."""
    port_macs: Dict[int, Set[str]] = defaultdict(set)
    mac_vlan: Dict[str, int] = {}
    for oid, value in session.walk(oids.DOT1Q_TP_FDB_PORT, max_rows=max_rows).items():
        parts = _suffix_parts(oid, oids.DOT1Q_TP_FDB_PORT)
        if parts is None or len(parts) < 1 + _MAC_OCTETS or not parts[0].isdigit():
            continue
        mac = _mac_from_oid_octets(parts[1 : 1 + _MAC_OCTETS])
        port = _opt_int(value)
        if mac is None or port is None or port < 1:  # BRIDGE-MIB ports are >= 1
            continue
        port_macs[port].add(mac)
        mac_vlan[mac] = int(parts[0])
    return dict(port_macs), mac_vlan


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
    out = collect_lldp(local, session, loc_ports=lldp_loc_port_ifnames(session))
    out += collect_cdp(local, session)
    # Ф7: prefer the dot1q VLAN FDB (carries each MAC's VLAN, recovers VLAN-aware
    # switches); fall back to the plain dot1d FDB when dot1q is empty/unsupported.
    port_macs_q, mac_vlan = read_fdb_dot1q(session)
    if port_macs_q:
        port_macs, port_if = port_macs_q, _read_port_ifindex(session)
    else:
        port_macs, port_if = read_fdb(session)
        mac_vlan = {}
    edges = l2.infer_edges(
        local, port_macs, port_if, infra_macs=infra_macs, own_macs=_own_macs(device)
    )
    if mac_vlan:  # tag each FDB edge with the VLAN its remote MAC was learned on
        edges = [replace(e, vlan=mac_vlan[e.b]) if e.b in mac_vlan else e for e in edges]
    out += edges
    return out
