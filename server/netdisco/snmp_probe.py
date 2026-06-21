"""Phase 6 -- read-only SNMP probe of a single candidate host.

Reuses the printers SNMP stack (``SnmpSession``, GET/WALK, never SET): this is an
observation engine, not a management one. Turns SNMP answers into an immutable
``DeviceProfile`` of raw signals; the type verdict is made later in ``classify``
(collector ⊥ semantic). An SNMP-mute host returns ``responded=False`` and we walk
nothing -- a dead address costs one timed-out GET, no table sweeps.

Presence checks (FDB, Printer-MIB, serial) are bounded to a handful of rows: we
only need "is this subtree non-empty?", never the whole table (that is phase 8's
job). Interface MACs come from the ifTable walk we already do, so AP detection
(ifType 71) and the agent-MAC match cost no extra round-trips.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Protocol

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.models import DeviceProfile, NetInterface
from server.printers.classify import is_printer

# Bounded presence probes: a few rows answer "non-empty?" without sweeping a
# 48-port switch's whole forwarding DB on a cheap classify pass.
_FDB_PROBE_ROWS = 8
_PRINTER_PROBE_ROWS = 8
_SERIAL_PROBE_ROWS = 32
_BPS_PER_MBPS = 1_000_000
_MAC_OCTETS = 6


class Session(Protocol):
    """What the probe needs from an SNMP session (see ``snmp.SnmpSession``)."""

    def get(self, oid_list: List[str]) -> Dict[str, object]: ...

    def walk(self, base_oid: str, *, max_rows: int = 512) -> Dict[str, object]: ...


def _str(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value != "" else None


def _int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _forwarding(value: object) -> Optional[bool]:
    code = _int(value)
    if code == 1:
        return True
    if code == 2:
        return False
    return None  # absent / other -> UNKNOWN, never a guessed router verdict


def _oper_up(value: object) -> Optional[bool]:
    code = _int(value)
    if code == 1:
        return True
    if code == 2:
        return False
    return None


def _mac(value: object) -> Optional[str]:
    """ifPhysAddress -> normalized MAC. The SNMP decoder hands OCTET STRINGs back
    as text; re-encode to bytes and accept only a clean 6-octet address (a utf-8-
    mangled MAC fails the length check -> None = UNKNOWN, never a wrong MAC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        return None
    if len(raw) != _MAC_OCTETS:
        return None
    return normalize_mac(":".join(f"{b:02x}" for b in raw))


def _by_index(walked: Dict[str, object], base: str) -> Dict[str, object]:
    """{full_oid: value} -> {index-suffix: value} for one walked column."""
    prefix = base + "."
    return {oid[len(prefix) :]: val for oid, val in walked.items() if oid.startswith(prefix)}


def _first_str(walked: Dict[str, object]) -> Optional[str]:
    for value in walked.values():
        text = _str(value)
        if text is not None:
            return text
    return None


def _build_interfaces(session: Session) -> tuple[NetInterface, ...]:
    descr = _by_index(session.walk(oids.IF_DESCR), oids.IF_DESCR)
    if_type = _by_index(session.walk(oids.IF_TYPE), oids.IF_TYPE)
    speed = _by_index(session.walk(oids.IF_SPEED), oids.IF_SPEED)
    phys = _by_index(session.walk(oids.IF_PHYS_ADDRESS), oids.IF_PHYS_ADDRESS)
    oper = _by_index(session.walk(oids.IF_OPER_STATUS), oids.IF_OPER_STATUS)
    indices = sorted(
        {k for col in (descr, if_type, speed, phys, oper) for k in col if k.isdigit()},
        key=int,
    )
    out = []
    for idx in indices:
        bps = _int(speed.get(idx))
        out.append(
            NetInterface(
                if_index=int(idx),
                name=_str(descr.get(idx)),
                if_type=_int(if_type.get(idx)),
                speed_mbps=(bps / _BPS_PER_MBPS) if bps is not None else None,
                oper_up=_oper_up(oper.get(idx)),
                phys_mac=_mac(phys.get(idx)),
            )
        )
    return tuple(out)


def probe_device(ip: str, session: Session, *, is_printer_fn=is_printer) -> DeviceProfile:
    """Probe one host over SNMP -> ``DeviceProfile``. Read-only, bounded, no raises.

    An SNMP-mute host (empty GET) short-circuits to ``responded=False`` before any
    table walk. Dependencies (the printer classifier) are injectable for tests."""
    scal = session.get(
        [
            oids.SYS_DESCR,
            oids.SYS_OBJECT_ID,
            oids.SYS_NAME,
            oids.SYS_SERVICES,
            oids.IP_FORWARDING,
            oids.DOT1D_BASE_BRIDGE_ADDRESS,
        ]
    )
    if not scal:
        return DeviceProfile(ip=ip, responded=False)

    interfaces = _build_interfaces(session)
    bridge = _str(scal.get(oids.DOT1D_BASE_BRIDGE_ADDRESS))
    has_fdb = (
        bool(session.walk(oids.DOT1D_TP_FDB_PORT, max_rows=_FDB_PROBE_ROWS)) if bridge else False
    )
    printer_probe = session.walk(oids.PRINTER_MIB, max_rows=_PRINTER_PROBE_ROWS)
    serial = _first_str(session.walk(oids.ENT_PHYSICAL_SERIAL, max_rows=_SERIAL_PROBE_ROWS))
    macs = tuple(dict.fromkeys(i.phys_mac for i in interfaces if i.phys_mac))

    return DeviceProfile(
        ip=ip,
        responded=True,
        sys_object_id=_str(scal.get(oids.SYS_OBJECT_ID)),
        sys_descr=_str(scal.get(oids.SYS_DESCR)),
        sys_name=_str(scal.get(oids.SYS_NAME)),
        sys_services=_int(scal.get(oids.SYS_SERVICES)),
        ip_forwarding=_forwarding(scal.get(oids.IP_FORWARDING)),
        bridge_address=bridge,
        has_fdb=has_fdb,
        is_printer=is_printer_fn(printer_probe),
        serial=serial,
        interfaces=interfaces,
        macs=macs,
    )
