"""Immutable netdisco models.

Frozen dataclasses (CLAUDE.md §5 immutability invariant): a stored device can
never be mutated in place. Every unknown field is ``None`` / an empty tuple --
UNKNOWN over an invented value, exactly as the trust/scoring core does.

Phase 1 defines the device + interface models consumed by identity and the
inventory builder. Link/evidence/graph models land in the phases that consume
them (TDD: no model without a consumer).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class NetInterface:
    """One network interface of a device (from SNMP ifTable + ifXTable Ф7). UNKNOWN -> None."""

    if_index: Optional[int] = None
    name: Optional[str] = None
    if_type: Optional[int] = None  # numeric ifType (language-independent)
    speed_mbps: Optional[float] = None
    oper_up: Optional[bool] = None
    phys_mac: Optional[str] = None
    if_alias: Optional[str] = None  # Ф7 ifAlias: operator description ("uplink to core")


@dataclass(frozen=True)
class DeviceProfile:
    """What one SNMP probe learned about a host (phase 6 input to ``classify``).

    Everything is best-effort: ``responded`` is False for an SNMP-mute host and
    every other field stays at its UNKNOWN default. Signals are raw -- the type
    decision lives entirely in ``classify`` (collector ⊥ semantic)."""

    ip: str
    responded: bool = False
    sys_object_id: Optional[str] = None
    sys_descr: Optional[str] = None
    sys_name: Optional[str] = None
    sys_services: Optional[int] = None  # numeric layer bitmask (language-independent)
    ip_forwarding: Optional[bool] = None  # True iff ipForwarding == 1 (router)
    bridge_address: Optional[str] = None  # dot1dBaseBridgeAddress present
    has_fdb: bool = False  # non-empty forwarding DB (switch confirmation)
    is_printer: bool = False  # printers.classify.is_printer on the Printer-MIB probe
    serial: Optional[str] = None
    model_name: Optional[str] = None  # Ф7 ENTITY-MIB entPhysicalModelName (exact model)
    interfaces: tuple[NetInterface, ...] = ()
    macs: tuple[str, ...] = ()  # normalized phys MACs seen across the ifTable


@dataclass(frozen=True)
class ResolvedLink:
    """One topology edge after fusion (§4.4): the winning claim for a node pair.

    ``a``/``b`` are stable node-ids (canonical order a <= b). ``via_source`` is the
    winning evidence source, ``confidence`` its band (LOW when ``ambiguous`` -- a
    contradiction was shown rather than resolved away). ``observed_at`` is the
    freshest contributing observation, stamped by the topology cycle.

    Ф7 additions: ``medium`` (wired/wireless/l3 -- wireless set by the real
    client->AP association, l3 by link_kind), ``vlan`` (the dot1q tag the edge
    carries, from Q-BRIDGE-FDB). Both optional/None until a Ф7 collector sets them.
    ``a_port``/``b_port`` carry the human port label when LLDP/ifXTable names it."""

    a: str
    b: str
    via_source: str
    confidence: str
    link_kind: str = "l2-edge"
    ambiguous: bool = False
    observed_at: Optional[str] = None
    medium: Optional[str] = None
    vlan: Optional[int] = None
    a_port: Optional[str] = None
    b_port: Optional[str] = None


@dataclass(frozen=True)
class NetDevice:
    """A discovered network device. ``nid`` is the stable identity (see
    ``identity.device_nid``); everything else is best-effort and may be None."""

    nid: str
    ip: Optional[str] = None
    hostname: Optional[str] = None
    mac: Optional[str] = None
    vendor: Optional[str] = None
    dev_type: str = "unknown"  # router/switch/ap/agent/printer/server/endpoint/unknown
    sys_object_id: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    site_code: Optional[str] = None
    status: Optional[str] = None  # up/down/unreachable/missing
    subtype: Optional[str] = None  # Ф7: LLDP-MED class / service type (phone/ap/server)
    interfaces: tuple[NetInterface, ...] = ()
    sources: tuple[str, ...] = ()  # which discovery sources found it
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
