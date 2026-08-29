"""Phase 6 -- device type from probe signals (RFC §4.2).

Determinative evidence only, in precedence order:

    agent > router > printer > switch / ap > endpoint > unknown

UNKNOWN over a guessed type is the project invariant: a vendor-enterprise
sysObjectID is never, by itself, a type (HP makes non-printers, Cisco makes
non-routers), so ``classify`` never reads ``sys_object_id``. Printer precedes
switch/ap (F3) so a network MFU with a Wi-Fi<->Ethernet bridge that answers
BRIDGE-MIB still classifies as printer, not switch/ap (those never
reclassify afterwards). A real wireless interface (ifType 71) alone
classifies "ap" (see the final rule below); a bridge without a real
forwarding DB still counts as a switch signal, and a bare ``ip_forwarding``
bit no longer promotes a host to router on its own -- see
``_ETH_PORTS_ROUTER`` corroboration below.
"""

from __future__ import annotations

from typing import Iterable, Optional, Set

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.models import DeviceProfile

# Корроборация для роутера: ipForwarding=1 включён у любого Docker/Hyper-V/ICS
# хоста, поэтому один этот бит роутером не делает (W: ложные «роутеры» отравляли
# _INFRA_TYPES и корреляцию первопричин).
_ETH_PORTS_ROUTER = 2
_ETH_PORTS_SWITCH = 4


def _bit(value: Optional[int], mask: int) -> bool:
    return bool((value or 0) & mask)


def _eth_ports(profile: DeviceProfile) -> int:
    return sum(1 for i in profile.interfaces if i.if_type == oids.IF_TYPE_ETHERNET)


def _has_wireless(profile: DeviceProfile) -> bool:
    return any(i.if_type == oids.IF_TYPE_IEEE80211 for i in profile.interfaces)


def classify(profile: DeviceProfile, agent_macs: Iterable[str]) -> str:
    """Сигналы зонда -> тип устройства. Порядок правил -- контракт, не деталь."""
    agents: Set[str] = set(agent_macs)
    macs = {normalize_mac(m) for m in profile.macs if m}

    if macs & agents:
        return "agent"
    if not profile.responded:
        return "endpoint" if macs else "unknown"
    if (
        profile.ip_forwarding
        and _bit(profile.sys_services, oids.SYS_SERVICES_L3)
        and _eth_ports(profile) >= _ETH_PORTS_ROUTER
    ):
        return "router"
    if profile.is_printer:
        return "printer"
    if profile.has_fdb or profile.bridge_address:
        return "ap" if _has_wireless(profile) else "switch"
    if (
        _bit(profile.sys_services, oids.SYS_SERVICES_L2)
        and _eth_ports(profile) >= _ETH_PORTS_SWITCH
    ):
        return "switch"
    if _has_wireless(profile):
        return "ap"
    return "endpoint"
