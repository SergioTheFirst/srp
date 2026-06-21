"""Phase 6 -- device type from probe signals (RFC §4.2).

Determinative evidence only, in precedence order:

    agent > router > switch / ap > printer > endpoint > unknown

UNKNOWN over a guessed type is the project invariant: a vendor-enterprise
sysObjectID is never, by itself, a type (HP makes non-printers, Cisco makes
non-routers), so ``classify`` never reads ``sys_object_id``. AP needs a real
wireless interface (ifType 71); a bridge without one degrades honestly to switch.
"""

from __future__ import annotations

from typing import Iterable, Set

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.models import DeviceProfile


def _has_wireless(profile: DeviceProfile) -> bool:
    return any(i.if_type == oids.IF_TYPE_IEEE80211 for i in profile.interfaces)


def classify(profile: DeviceProfile, agent_macs: Iterable[str]) -> str:
    """Map probe signals to a device type. ``agent_macs`` = the fleet's own adapter
    MACs (already normalized); a device whose MAC is one of ours is ``agent``."""
    agents: Set[str] = set(agent_macs)
    macs = {normalize_mac(m) for m in profile.macs if m}

    if macs & agents:
        return "agent"
    if not profile.responded:
        # Silent host: an inventory MAC means we have seen it on the LAN (endpoint);
        # nothing at all -> unknown, never a fabricated type.
        return "endpoint" if macs else "unknown"
    if profile.ip_forwarding:
        return "router"
    if profile.bridge_address and profile.has_fdb:
        return "ap" if _has_wireless(profile) else "switch"
    if profile.is_printer:
        return "printer"
    return "endpoint"
