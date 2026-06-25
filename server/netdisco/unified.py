"""Ф2: unified network-map assembler (pure superset graph, read-side, D7).

ONE model. The persistent netdisco backbone (``net_devices``/``net_links``) is the
source of truth for nodes and real L2/L3 links; the Phase-1 identity FKs collapse
an agent/printer and its discovered ``net_device`` into ONE node (canonical key =
``device_nid``); the ephemeral netmap overlays (agent-uplink edges, ICMP quality,
subnet anomaly) enrich it. netmap is not a second topology model.

Pure over already-read inputs: the API/cache layer (Ф3) does the DB reads and
caches the result -- this module never touches the DB or the network. The output
``{"nodes","links","subnets","totals"}`` is a SUPERSET of the stored snapshot
graph node/link shape, so the single canvas (Ф4) renders both old pages from it.
"""

from __future__ import annotations

from typing import Any, Optional

from server.analytics.netmap import (
    quality_overlay,
    subnet_anomaly,
    subnet_hint,
)
from server.analytics.oui import normalize_mac, vendor_for_mac
from server.netdisco.identity import device_nid

# Adapter ``kind`` substrings that mark an agent uplink (and Ф7 wireless edges).
_WIRELESS_KINDS = ("wifi", "wireless", "wi-fi", "802.11", "wlan")

_Node = dict[str, Any]
_Index = dict[str, str]


def historical_graph_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """Ф5 time machine: normalise ONE stored topology snapshot into the unified shape.

    The stored graph is a subset (nodes/links only); the live overlays -- ICMP quality,
    subnet anomaly, identity FKs -- are derived per request and were never persisted, so
    a historical frame carries none (D5: no false confidence in a stale frame). We lift
    nodes/links verbatim and add empty overlays + a totals block keyed like
    ``build_network_map``'s, plus ``history_at``/``received_at`` so the single canvas
    (Ф4) renders a past frame with the same contract and shows the time-machine plaque.

    ONE source of truth for this shape -- the API and the SSR ``/netmap?at=`` route both
    call it, so the plaque marker can never drift between them."""
    raw = snap.get("graph") or {}
    nodes = list(raw.get("nodes") or [])
    links = list(raw.get("links") or [])
    return {
        "nodes": nodes,
        "links": links,
        "subnets": [],
        "totals": {
            "nodes": len(nodes),
            "links": len(links),
            "agents": 0,
            "printers": 0,
            "anomalies": 0,
            "wireless_links": 0,
        },
        "history_at": snap.get("id"),
        "received_at": snap.get("received_at"),
    }


def _card_url(device_id: Optional[str], printer_id: Optional[str], nid: str) -> Optional[str]:
    """Canonical card per Ф2 priority: agent > printer > net-infra (Ф6 adds redirects)."""
    if device_id:
        return f"/device/{device_id}"
    if printer_id:
        return f"/printers/{printer_id}"
    if nid and nid != "nd-unknown":
        return f"/netdisco/device/{nid}"
    return None


def _medium_for_adapter(kind: Optional[str]) -> str:
    k = (kind or "").lower()
    return "wireless" if any(w in k for w in _WIRELESS_KINDS) else "wired"


def _medium_for_link(link: dict[str, Any], nodes: dict[str, _Node]) -> str:
    """Ф2 heuristic: l3 by link_kind; any SNMP link touching an AP -> wireless; else
    wired. Ф7 refines wireless to real client->AP associations (wireless-MIB)."""
    if "l3" in (link.get("link_kind") or "").lower():
        return "l3"
    for end in (link.get("a_nid"), link.get("b_nid")):
        n = nodes.get(end) if isinstance(end, str) else None
        if n is not None and n.get("dev_type") == "ap":
            return "wireless"
    return "wired"


def _resolve_nid(mac: Optional[str], ip: Optional[str], by_mac: _Index, by_ip: _Index) -> str:
    """Reuse the nid this MAC/IP already names (so a gateway/agent/printer never
    duplicates a discovered net_device); else mint the canonical device_nid."""
    nm = normalize_mac(mac)
    if nm and nm in by_mac:
        return by_mac[nm]
    if ip and ip in by_ip:
        return by_ip[ip]
    return device_nid(mac=mac) if nm else device_nid(ip=ip)


def _node_from_net_device(d: dict[str, Any]) -> _Node:
    nid = d["device_nid"]
    device_id, printer_id = d.get("device_id"), d.get("printer_id")
    dev_type = d.get("dev_type") or "unknown"
    if device_id and dev_type == "unknown":
        dev_type = "agent"  # a linked agent gets the agent glyph, not "unknown"
    elif printer_id and dev_type == "unknown":
        dev_type = "printer"  # a linked printer gets the printer glyph (symmetric)
    prov = ["net"] + (["agent"] if device_id else []) + (["printer"] if printer_id else [])
    return {
        "nid": nid,
        "dev_type": dev_type,
        "ip": d.get("ip"),
        "hostname": d.get("hostname"),
        "mac": normalize_mac(d.get("mac")),
        "vendor": d.get("vendor"),
        "status": d.get("status"),
        "model": d.get("model"),
        "subnet": subnet_hint(d.get("ip")),
        "subtype": "printer" if printer_id else None,
        "device_id": device_id,
        "printer_id": printer_id,
        "card_url": _card_url(device_id, printer_id, nid),
        "provenance": sorted(set(prov)),
    }


def _stub_node(nid: str) -> _Node:
    """A link endpoint with no net_device row of its own (so no edge dangles)."""
    return {
        "nid": nid,
        "dev_type": "unknown",
        "ip": None,
        "hostname": None,
        "mac": None,
        "vendor": None,
        "status": None,
        "model": None,
        "subnet": None,
        "subtype": None,
        "device_id": None,
        "printer_id": None,
        "card_url": _card_url(None, None, nid),
        "provenance": ["net"],
    }


def _seed_net_devices(
    net_devices: list[dict[str, Any]],
) -> tuple[dict[str, _Node], _Index, _Index, _Index, _Index]:
    nodes: dict[str, _Node] = {}
    by_mac: _Index = {}
    by_ip: _Index = {}
    by_device_id: _Index = {}
    by_printer_id: _Index = {}
    for d in net_devices:
        nid = d.get("device_nid")
        if not nid:
            continue
        nodes[nid] = _node_from_net_device(d)
        nm = normalize_mac(d.get("mac"))
        if nm:
            by_mac.setdefault(nm, nid)
        if d.get("ip"):
            by_ip.setdefault(d["ip"], nid)
        if d.get("device_id"):
            by_device_id.setdefault(d["device_id"], nid)
        if d.get("printer_id"):
            by_printer_id.setdefault(d["printer_id"], nid)
    return nodes, by_mac, by_ip, by_device_id, by_printer_id


def _primary_adapter(snap: dict[str, Any]) -> dict[str, Any]:
    return next((a for a in snap.get("adapters") or [] if isinstance(a, dict)), {})


def _synth_agent_node(nid: str, snap: dict[str, Any], did: str) -> _Node:
    a0 = _primary_adapter(snap)
    ip0 = (a0.get("ipv4") or [None])[0]
    mac0 = normalize_mac(a0.get("mac"))
    return {
        "nid": nid,
        "dev_type": "agent",
        "ip": ip0,
        "hostname": snap.get("hostname"),
        "mac": mac0,
        "vendor": vendor_for_mac(mac0),
        "status": "up" if a0.get("up") else None,
        "model": None,
        "subnet": subnet_hint(ip0),
        "subtype": None,
        "device_id": did,
        "printer_id": None,
        "card_url": _card_url(did, None, nid),
        "provenance": ["agent"],
    }


def _enrich_agent(node: _Node, snap: dict[str, Any], did: str) -> None:
    a0 = _primary_adapter(snap)
    ip0 = (a0.get("ipv4") or [None])[0]
    mac0 = normalize_mac(a0.get("mac"))
    node["device_id"] = did
    if node.get("dev_type") in (None, "unknown"):
        node["dev_type"] = "agent"
    if not node.get("ip"):
        node["ip"] = ip0
    if not node.get("mac"):
        node["mac"] = mac0
    if not node.get("vendor"):
        node["vendor"] = vendor_for_mac(mac0)
    if not node.get("status") and a0:
        node["status"] = "up" if a0.get("up") else None
    if not node.get("subnet"):
        node["subnet"] = subnet_hint(ip0)
    if not node.get("hostname"):
        node["hostname"] = snap.get("hostname")
    node["provenance"] = sorted(set(node.get("provenance") or []) | {"agent"})
    node["card_url"] = _card_url(did, node.get("printer_id"), node["nid"])


def _merge_agents(
    nodes: dict[str, _Node],
    snapshots: list[dict[str, Any]],
    by_mac: _Index,
    by_ip: _Index,
    by_device_id: _Index,
) -> None:
    for snap in snapshots:
        did = snap.get("device_id")
        if not did:
            continue
        a0 = _primary_adapter(snap)
        ip0 = (a0.get("ipv4") or [None])[0]
        nm0 = normalize_mac(a0.get("mac"))
        nid = by_device_id.get(did) or _resolve_nid(a0.get("mac"), ip0, by_mac, by_ip)
        if nid == "nd-unknown":
            continue  # no MAC/IP -> unplaceable; never collide on the null-identity bucket
        if nid in nodes:
            _enrich_agent(nodes[nid], snap, did)
        else:
            nodes[nid] = _synth_agent_node(nid, snap, did)
        by_device_id[did] = nid
        if nm0:
            by_mac.setdefault(nm0, nid)
        if ip0:
            by_ip.setdefault(ip0, nid)


def _synth_printer_node(nid: str, p: dict[str, Any], pid: str) -> _Node:
    mac = normalize_mac(p.get("mac"))
    return {
        "nid": nid,
        "dev_type": "printer",
        "ip": p.get("ip"),
        "hostname": p.get("hostname"),
        "mac": mac,
        "vendor": p.get("vendor") or vendor_for_mac(mac),
        "status": p.get("status"),
        "model": p.get("model"),
        "subnet": subnet_hint(p.get("ip")),
        "subtype": "printer",
        "device_id": None,
        "printer_id": pid,
        "card_url": _card_url(None, pid, nid),
        "provenance": ["printer"],
    }


def _enrich_printer(node: _Node, p: dict[str, Any], pid: str) -> None:
    mac = normalize_mac(p.get("mac"))
    node["printer_id"] = pid
    node["subtype"] = "printer"
    if node.get("dev_type") in (None, "unknown"):
        node["dev_type"] = "printer"
    if not node.get("ip"):
        node["ip"] = p.get("ip")
    if not node.get("mac"):
        node["mac"] = mac
    node["vendor"] = node.get("vendor") or p.get("vendor") or vendor_for_mac(mac)
    node["model"] = node.get("model") or p.get("model")
    node["status"] = node.get("status") or p.get("status")
    if not node.get("subnet"):
        node["subnet"] = subnet_hint(p.get("ip"))
    if not node.get("hostname"):
        node["hostname"] = p.get("hostname")
    node["provenance"] = sorted(set(node.get("provenance") or []) | {"printer"})
    node["card_url"] = _card_url(node.get("device_id"), pid, node["nid"])


def _merge_printers(
    nodes: dict[str, _Node],
    printers: list[dict[str, Any]],
    by_mac: _Index,
    by_ip: _Index,
    by_printer_id: _Index,
) -> None:
    for p in printers:
        pid = p.get("printer_id")
        if not pid:
            continue
        nm = normalize_mac(p.get("mac"))
        nid = by_printer_id.get(pid) or _resolve_nid(p.get("mac"), p.get("ip"), by_mac, by_ip)
        if nid == "nd-unknown":
            continue  # no MAC/IP -> unplaceable; never collide on the null-identity bucket
        if nid in nodes:
            _enrich_printer(nodes[nid], p, pid)
        else:
            nodes[nid] = _synth_printer_node(nid, p, pid)
        by_printer_id[pid] = nid
        if nm:
            by_mac.setdefault(nm, nid)
        if p.get("ip"):
            by_ip.setdefault(p["ip"], nid)


def _real_links(net_links: list[dict[str, Any]], nodes: dict[str, _Node]) -> list[dict[str, Any]]:
    for link in net_links:
        for end in (link.get("a_nid"), link.get("b_nid")):
            if end and end not in nodes:
                nodes[end] = _stub_node(end)
    out: list[dict[str, Any]] = []
    seen: set = set()
    for link in net_links:
        a, b = link.get("a_nid"), link.get("b_nid")
        key = (a, b, link.get("link_kind"))
        if not a or not b or a == b or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "a": a,
                "b": b,
                "link_kind": link.get("link_kind") or "l2-edge",
                "via_source": link.get("via_source"),
                "confidence": link.get("confidence"),
                "ambiguous": bool(link.get("ambiguous", False)),
                "medium": _medium_for_link(link, nodes),
                "a_port": link.get("a_if"),
                "b_port": link.get("b_if"),
                "quality": None,
            }
        )
    return out


def _gateways(snap: dict[str, Any]) -> dict[str, Optional[str]]:
    """gateway-IP -> the kind of the first adapter that uses it (medium hint)."""
    out: dict[str, Optional[str]] = {}
    for a in snap.get("adapters") or []:
        if not isinstance(a, dict):
            continue
        gw = a.get("gateway")
        if gw and str(gw) not in out:
            out[str(gw)] = a.get("kind")
    return out


def _ensure_gateway(
    nodes: dict[str, _Node], gw_nid: str, gw_ip: str, gw_mac: Optional[str]
) -> None:
    existing = nodes.get(gw_nid)
    if existing is not None:
        if existing.get("dev_type") == "unknown":  # upgrade a bare net_links stub endpoint
            mac = normalize_mac(gw_mac)
            existing["dev_type"] = "router"
            existing["ip"] = existing.get("ip") or gw_ip
            existing["mac"] = existing.get("mac") or mac
            existing["vendor"] = existing.get("vendor") or vendor_for_mac(mac)
            existing["subnet"] = existing.get("subnet") or subnet_hint(gw_ip)
            existing["provenance"] = sorted(set(existing.get("provenance") or []) | {"gateway"})
        return  # a classified net_device gateway is authoritative -- never override it
    mac = normalize_mac(gw_mac)
    nodes[gw_nid] = {
        "nid": gw_nid,
        "dev_type": "router",
        "ip": gw_ip,
        "hostname": None,
        "mac": mac,
        "vendor": vendor_for_mac(mac),
        "status": None,
        "model": None,
        "subnet": subnet_hint(gw_ip),
        "subtype": None,
        "device_id": None,
        "printer_id": None,
        "card_url": _card_url(None, None, gw_nid),
        "provenance": ["gateway"],
    }


def _agent_uplinks(
    snapshots: list[dict[str, Any]],
    nodes: dict[str, _Node],
    by_mac: _Index,
    by_ip: _Index,
    by_device_id: _Index,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    links: list[dict[str, Any]] = []
    seen: set = set()
    subnet_losses: dict[str, list[Optional[float]]] = {}
    for snap in snapshots:
        did = snap.get("device_id")
        agent_nid = by_device_id.get(did) if isinstance(did, str) else None
        if agent_nid is None:
            continue
        gw_mac = {
            n.get("ip"): n.get("mac") for n in snap.get("neighbors") or [] if isinstance(n, dict)
        }
        for gw, kind in _gateways(snap).items():
            gw_nid = _resolve_nid(gw_mac.get(gw), gw, by_mac, by_ip)
            _ensure_gateway(nodes, gw_nid, gw, gw_mac.get(gw))
            q = quality_overlay(snap, gw)
            pair = frozenset({agent_nid, gw_nid})
            if agent_nid != gw_nid and pair not in seen:
                seen.add(pair)
                links.append(
                    {
                        "a": agent_nid,
                        "b": gw_nid,
                        "link_kind": "agent-uplink",
                        "via_source": "agent",
                        "confidence": "high",
                        "ambiguous": False,
                        "medium": _medium_for_adapter(kind),
                        "a_port": None,
                        "b_port": None,
                        "quality": q,
                    }
                )
            sub = subnet_hint(gw)
            if sub:
                subnet_losses.setdefault(sub, []).append(q["loss_pct"] if q else None)
    subnets = [
        {"subnet_hint": sub, **subnet_anomaly(losses)}
        for sub, losses in sorted(subnet_losses.items())
    ]
    return links, subnets


def build_network_map(
    net_devices: list[dict[str, Any]],
    net_links: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    printers: list[dict[str, Any]],
) -> dict[str, Any]:
    """The one superset graph: nodes from net_devices + agents + printers + gateways
    (deduped by device_nid), edges from net_links + agent-uplinks (medium/quality),
    plus a per-subnet anomaly overlay. Pure over already-read inputs."""
    nodes, by_mac, by_ip, by_device_id, by_printer_id = _seed_net_devices(net_devices)
    _merge_agents(nodes, snapshots, by_mac, by_ip, by_device_id)
    _merge_printers(nodes, printers, by_mac, by_ip, by_printer_id)
    links = _real_links(net_links, nodes)
    uplinks, subnets = _agent_uplinks(snapshots, nodes, by_mac, by_ip, by_device_id)
    links += uplinks
    node_list = sorted(nodes.values(), key=lambda n: n["nid"])
    link_list = sorted(links, key=lambda e: (e["link_kind"], e["a"], e["b"]))
    return {
        "nodes": node_list,
        "links": link_list,
        "subnets": subnets,
        "totals": {
            "nodes": len(node_list),
            "links": len(link_list),
            "agents": len({n["device_id"] for n in node_list if n.get("device_id")}),
            "printers": len({n["printer_id"] for n in node_list if n.get("printer_id")}),
            "anomalies": sum(1 for s in subnets if s["anomaly"]),
            "wireless_links": sum(1 for e in link_list if e["medium"] == "wireless"),
        },
    }
