"""Phase 10 -- §3.13 change detection + ghost lifecycle.

``diff`` turns two topology snapshots into a deterministic delta list (appeared /
disappeared / link_added / link_removed / reclassified / ip_changed). Nodes are keyed
by their stable nid, so a DHCP lease change is ``ip_changed`` -- never a phantom
appeared+disappeared pair (identity, §4.1).

``stale_lifecycle`` ages devices toward removal the careful way (the device-ghost
cleanup lesson): a device is only ``missing`` after sustained absence, and only
``eligible_purge`` after much longer -- one missed cycle is never "disappeared".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

MISSING = "missing"
ELIGIBLE_PURGE = "eligible_purge"


@dataclass(frozen=True)
class TopologyDelta:
    kind: str
    device_nid: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


def _nodes_by_nid(snapshot: dict) -> Dict[str, dict]:
    return {n["nid"]: n for n in snapshot.get("nodes") or [] if n.get("nid")}


def _link_keys(snapshot: dict) -> set:
    keys = set()
    for link in snapshot.get("links") or []:
        a, b = link.get("a"), link.get("b")
        if a and b and a != b:
            keys.add((a, b) if a <= b else (b, a))  # canonical undirected
    return keys


def _detail_key(detail: Dict[str, Any]) -> Tuple:
    return tuple(sorted((str(k), str(v)) for k, v in detail.items()))


def diff(prev: dict, curr: dict) -> List[TopologyDelta]:
    """Deterministic topology delta list between two snapshots."""
    prev_nodes, curr_nodes = _nodes_by_nid(prev), _nodes_by_nid(curr)
    deltas: List[TopologyDelta] = []
    for nid in curr_nodes.keys() - prev_nodes.keys():
        deltas.append(TopologyDelta("appeared", nid))
    for nid in prev_nodes.keys() - curr_nodes.keys():
        deltas.append(TopologyDelta("disappeared", nid))
    for nid in curr_nodes.keys() & prev_nodes.keys():
        before, after = prev_nodes[nid], curr_nodes[nid]
        if before.get("dev_type") != after.get("dev_type"):
            deltas.append(
                TopologyDelta(
                    "reclassified",
                    nid,
                    {"from": before.get("dev_type"), "to": after.get("dev_type")},
                )
            )
        if before.get("ip") != after.get("ip"):
            deltas.append(
                TopologyDelta("ip_changed", nid, {"from": before.get("ip"), "to": after.get("ip")})
            )
    prev_links, curr_links = _link_keys(prev), _link_keys(curr)
    for a, b in curr_links - prev_links:
        deltas.append(TopologyDelta("link_added", None, {"a": a, "b": b}))
    for a, b in prev_links - curr_links:
        deltas.append(TopologyDelta("link_removed", None, {"a": a, "b": b}))
    deltas.sort(key=lambda d: (d.kind, d.device_nid or "", _detail_key(d.detail)))
    return deltas


def _age_seconds(now: str, last_seen: str) -> Optional[float]:
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(last_seen)).total_seconds()
    except (ValueError, TypeError):
        return None  # unparseable timestamp -> never age it out (UNKNOWN over a guess)


def stale_lifecycle(
    devices: List[dict],
    *,
    now: str,
    stale_after_sec: int,
    purge_after_sec: int,
) -> List[Tuple[str, str]]:
    """``[(device_nid, new_status)]`` for devices that aged into missing / purge.

    Sustained absence only: ``missing`` past ``stale_after_sec``, ``eligible_purge``
    past ``purge_after_sec``. A briefly-unseen device is left alone (ghost lesson)."""
    out: List[Tuple[str, str]] = []
    for dev in devices:
        nid, last_seen = dev.get("device_nid"), dev.get("last_seen")
        if not nid or not last_seen:
            continue
        age = _age_seconds(now, last_seen)
        if age is None:
            continue
        if age >= purge_after_sec:
            if dev.get("status") != ELIGIBLE_PURGE:
                out.append((nid, ELIGIBLE_PURGE))
        elif age >= stale_after_sec and dev.get("status") not in (MISSING, ELIGIBLE_PURGE):
            out.append((nid, MISSING))
    out.sort()
    return out
