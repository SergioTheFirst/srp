"""Segment links and life evidence derived from agent snapshots (pure, no DB, no clock).

An ARP entry on a real LAN/Wi-Fi adapter proves the neighbour sits in the observer's
broadcast segment, so it is linked to that segment's gateway. Nodes are never created
here: ``resolve(mac, ip)`` returns the nid of an EXISTING node or ``None``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from server.analytics.netmap import primary_adapter, subnet_hint
from server.netdisco.changes import _age_seconds
from server.printers.discovery import is_rfc1918

_Resolve = Callable[[Optional[str], Optional[str]], Optional[str]]


def _ts(snap: dict[str, Any]) -> str:
    """Effective snapshot time: the earlier of the agent clock and the server-stamped last_seen."""
    at, seen = snap.get("snapshot_at") or "", snap.get("last_seen") or ""
    gap = _age_seconds(at, seen)
    if gap is not None:
        return seen if gap > 0 else at
    return at if _age_seconds(at, at) is not None else seen


def _neighbors(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in snap.get("neighbors") or [] if isinstance(n, dict)]


def _first_ip(adapter: dict[str, Any]) -> Optional[str]:
    return next((i for i in adapter.get("ipv4") or [] if isinstance(i, str)), None)


def _usable_adapters(snap: dict[str, Any]) -> list[dict[str, Any]]:
    # ponytail: legit PCs have 1-2 LAN adapters with 1-2 addresses; the wire schema has no cap
    # on NetAdapter.ipv4, so one snapshot vouches for at most 4 adapters x 2 addresses.
    # Upgrade path = a cap on NetAdapter.ipv4 in the agent contract plus an agent-side clip.
    usable = [
        dict(a, ipv4=list(a["ipv4"])[:2])
        for a in snap.get("adapters") or []
        if isinstance(a, dict)
        and a.get("up") is True
        and a.get("role") in ("lan", "wifi")
        and a.get("tunnel") is not True
        and a.get("gateway")
        and _first_ip(a)
    ]
    return usable[:4]


def _is_fresh(now: str, ts: str, limit: float) -> bool:
    age = _age_seconds(now, ts)
    return age is not None and 0 <= age <= limit  # a future stamp is never fresh


def _snap_links(snap: dict[str, Any], resolve: _Resolve, skip: set) -> set:
    adapters = _usable_adapters(snap)
    macs = {n.get("ip"): n.get("mac") for n in _neighbors(snap)}
    out = set()
    for n in _neighbors(snap):
        ip = n.get("ip")
        hint = subnet_hint(ip) if ip else None
        ad = next(
            (a for a in adapters if hint and any(subnet_hint(i) == hint for i in a["ipv4"])), None
        )
        if ad is None:
            continue
        gw = str(ad["gateway"])
        gw_nid = resolve(macs.get(gw), gw)
        dev_nid = resolve(n.get("mac"), ip)
        if gw_nid and dev_nid and dev_nid != gw_nid and dev_nid not in skip:
            out.add((dev_nid, gw_nid))
    return out


def segment_links(
    snapshots: list[dict[str, Any]],
    resolve: _Resolve,
    skip_nids: set,
    *,
    fresh_sec: int = 21600,
) -> list[dict[str, Any]]:
    # ponytail: a segment is assumed to be a /24 (853 of 853 observations on real data;
    # upgrade path = adapter prefix length in the agent contract); no observer voting or
    # proxy-ARP guard (0 conflicts on real data).
    stamps = [t for t in (_ts(s) for s in snapshots) if _age_seconds(t, t) is not None]
    if not stamps:
        return []
    newest = max(stamps)
    observers: dict = {}
    for snap in snapshots:
        if _is_fresh(newest, _ts(snap), fresh_sec):
            for pair in _snap_links(snap, resolve, skip_nids):
                observers.setdefault(pair, set()).add(snap.get("device_id"))
    return [
        {
            "a": a,
            "b": b,
            "link_kind": "l2-segment",
            "via_source": "arp",
            "confidence": "low",
            "ambiguous": False,
            "medium": "unknown",
            "vlan": None,
            "a_port": None,
            "b_port": None,
            "speed_mbps": None,
            "port_down": False,
            "quality": None,
            "observers": len(obs),
        }
        for (a, b), obs in sorted(observers.items())
    ]


def alive_nids(
    snapshots: list[dict[str, Any]],
    resolve: _Resolve,
    *,
    now: str,
    fresh_sec: float,
    agent_fresh_sec: float,
) -> dict[str, str]:
    """``{nid: newest evidence time}`` -- the agent itself, Reachable neighbours, gateways.

    A neighbour or gateway counts only through a usable adapter (up, lan/wifi, no tunnel)
    of the same /24, and a neighbour only when RFC1918: one agent vouches for its segment.
    """
    out: dict[str, str] = {}
    for snap in snapshots:
        seen = snap.get("last_seen") or ""
        if _is_fresh(now, seen, agent_fresh_sec):
            ad = primary_adapter(snap.get("adapters"))
            ip = _first_ip(ad) if ad else None
            _put(out, resolve(ad.get("mac"), ip if is_rfc1918(ip) else None) if ad else None, seen)
        ts = _ts(snap)
        # a silent observer vouches for nobody: its ARP/quality evidence needs it online now
        if _is_fresh(now, seen, agent_fresh_sec) and _is_fresh(now, ts, fresh_sec):
            for nid in _snap_alive(snap, resolve):
                _put(out, nid, ts)
    return out


def _put(out: dict[str, str], nid: Optional[str], ts: str) -> None:
    if nid and (nid not in out or (_age_seconds(ts, out[nid]) or 0) > 0):
        out[nid] = ts


def _snap_alive(snap: dict[str, Any], resolve: _Resolve) -> set:
    adapters = _usable_adapters(snap)
    hints = {subnet_hint(i) for a in adapters for i in a["ipv4"]}
    gateways = {str(a["gateway"]) for a in adapters}
    macs = {n.get("ip"): n.get("mac") for n in _neighbors(snap)}
    out = set()
    for n in _neighbors(snap):
        ip = n.get("ip")
        if n.get("state") == "Reachable" and is_rfc1918(ip) and subnet_hint(ip) in hints:
            out.add(resolve(n.get("mac"), ip))
    for q in snap.get("quality") or []:
        loss = q.get("loss_pct") if isinstance(q, dict) else None
        is_gw = isinstance(q, dict) and q.get("target_kind") == "gateway"
        if is_gw and loss is not None and loss < 100 and q.get("target") in gateways:
            out.add(resolve(macs.get(q.get("target")), q.get("target")))
    return out
