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
    """One network interface of a device (from SNMP ifTable). UNKNOWN -> None."""

    if_index: Optional[int] = None
    name: Optional[str] = None
    if_type: Optional[int] = None  # numeric ifType (language-independent)
    speed_mbps: Optional[float] = None
    oper_up: Optional[bool] = None
    phys_mac: Optional[str] = None


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
    interfaces: tuple[NetInterface, ...] = ()
    sources: tuple[str, ...] = ()  # which discovery sources found it
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
