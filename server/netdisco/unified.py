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
from server.netdisco.graph import build_graph, find_articulation_points, find_bridges
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
        # Ф7: a printer FK always wins the subtype; otherwise carry the stored
        # LLDP-MED/service subtype (phone/ap/server) when one was learned.
        "subtype": "printer" if printer_id else d.get("subtype"),
        "first_seen": d.get("first_seen"),
        "last_seen": d.get("last_seen"),
        # B7: SNMP actually answered (sysObjectID read) AND it was classified -> confirmed,
        # not a guess from ARP/passive. Honest "we talked to it" signal.
        "confirmed": bool(d.get("sys_object_id")) and (d.get("dev_type") or "unknown") != "unknown",
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
        "first_seen": None,
        "last_seen": None,
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
        "first_seen": None,
        "last_seen": snap.get("last_seen"),
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
    if snap.get("last_seen"):
        node["last_seen"] = snap["last_seen"]  # live agent contact is fresher than discovery
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
        "first_seen": None,
        "last_seen": p.get("last_seen"),
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
    if p.get("last_seen"):
        node["last_seen"] = p["last_seen"]
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


def _is_adapter_link(link: dict[str, Any]) -> bool:
    """Ф9d: a Tier-3 adapter-sourced edge (``via_source`` like ``adapter:unifi``)."""
    return str(link.get("via_source") or "").startswith("adapter")


def _index_interfaces(
    net_interfaces: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Index ``net_interfaces`` by (device_nid, if_index) so a link's a_if/b_if can pull
    the operator port alias, negotiated speed and oper status onto the edge (S3). Ф7
    persisted these columns; the assembler never read them. First row per key wins."""
    idx: dict[tuple[str, int], dict[str, Any]] = {}
    for row in net_interfaces:
        nid = row.get("device_nid")
        ifx = row.get("if_index")
        if nid and ifx is not None:
            idx.setdefault((nid, int(ifx)), row)
    return idx


def _link_physics(
    link: dict[str, Any], a: str, b: str, iface_idx: dict[tuple[str, int], dict[str, Any]]
) -> dict[str, Any]:
    """S3: pull negotiated speed, operator port alias and oper-down off the two endpoint
    interfaces. Empty LLDP port labels fall back to if_alias (non-LLDP switches regain
    port names for free); speed is the slower of the two ends present."""
    a_if, b_if = link.get("a_if"), link.get("b_if")
    ifa = iface_idx.get((a, int(a_if))) if a_if is not None else None
    ifb = iface_idx.get((b, int(b_if))) if b_if is not None else None
    speeds = [r["speed_mbps"] for r in (ifa, ifb) if r and r.get("speed_mbps") is not None]
    opers = [r.get("oper_up") for r in (ifa, ifb) if r is not None]
    return {
        "speed_mbps": min(speeds) if speeds else None,
        "a_port": link.get("a_port") or (ifa or {}).get("if_alias") or None,
        "b_port": link.get("b_port") or (ifb or {}).get("if_alias") or None,
        "port_down": any(o == 0 for o in opers),
    }


def _real_links(
    net_links: list[dict[str, Any]],
    nodes: dict[str, _Node],
    iface_idx: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    for link in net_links:
        for end in (link.get("a_nid"), link.get("b_nid")):
            if end and end not in nodes:
                nodes[end] = _stub_node(end)
    out: list[dict[str, Any]] = []
    seen: set = set()
    pairs: set = set()
    # Ф9d: draw validated links first; a Tier-3 adapter link only GAP-FILLS a node
    # pair no validated link already connects -- never a parallel edge over an SNMP
    # edge. With no adapter links the order is unchanged (regression-safe).
    ordered = [link for link in net_links if not _is_adapter_link(link)]
    ordered += [link for link in net_links if _is_adapter_link(link)]
    for link in ordered:
        a, b = link.get("a_nid"), link.get("b_nid")
        key = (a, b, link.get("link_kind"))
        if not a or not b or a == b or key in seen:
            continue
        pair = frozenset((a, b))
        if _is_adapter_link(link) and pair in pairs:
            continue
        seen.add(key)
        pairs.add(pair)
        phys = _link_physics(link, a, b, iface_idx)
        out.append(
            {
                "a": a,
                "b": b,
                "link_kind": link.get("link_kind") or "l2-edge",
                "via_source": link.get("via_source"),
                "confidence": link.get("confidence"),
                "ambiguous": bool(link.get("ambiguous", False)),
                # Ф7: the stored per-edge medium wins; the Ф2 AP heuristic is the
                # fallback for pre-Ф7 links that carry no medium. The dot1q VLAN comes
                # straight from the persisted edge; S3 fills empty port labels from the
                # interface if_alias and adds negotiated speed + an oper-down flag.
                "medium": link.get("medium") or _medium_for_link(link, nodes),
                "vlan": link.get("vlan"),
                "a_port": phys["a_port"],
                "b_port": phys["b_port"],
                "speed_mbps": phys["speed_mbps"],
                "port_down": phys["port_down"],
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
        "first_seen": None,
        "last_seen": None,
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
                        "vlan": None,
                        "a_port": None,
                        "b_port": None,
                        "speed_mbps": None,
                        "port_down": False,
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


def _pair_key(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Order-independent key for an undirected node pair (None when either end missing)."""
    return "|".join(sorted((a, b))) if a and b else None


def _attach_overlays(
    node_list: list[_Node],
    link_list: list[dict[str, Any]],
    net_changes: Optional[list[dict[str, Any]]],
    status_series: Optional[dict[str, list[str]]],
) -> None:
    """S2 change-overlay + S5 reachability series/flaps, attached read-side. Changes come
    newest-first (``get_net_changes``) so the first hit per node/link wins; recency itself
    is decided in canvas JS (pure here: no clock). Also defaults the B7 confirmed flag on
    synthesized nodes that never carry a sysObjectID."""
    node_change: dict[str, tuple[Optional[str], Optional[str]]] = {}
    link_change: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for ch in net_changes or []:
        kind, nid = ch.get("kind"), ch.get("device_nid")
        if nid:
            node_change.setdefault(nid, (kind, ch.get("ts")))
        elif kind in ("link_added", "link_removed"):
            detail = ch.get("detail") or {}
            pk = _pair_key(detail.get("a"), detail.get("b"))
            if pk:
                link_change.setdefault(pk, (kind, ch.get("ts")))
    series = status_series or {}
    for n in node_list:
        n.setdefault("confirmed", False)
        kind, ts = node_change.get(n["nid"], (None, None))
        n["change"], n["change_ts"] = kind, ts
        seq = list(series.get(n["nid"]) or [])
        n["reach_series"] = seq
        n["flaps"] = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
    for e in link_list:
        pk = _pair_key(e.get("a"), e.get("b"))
        kind, ts = link_change.get(pk, (None, None)) if pk else (None, None)
        e["change"], e["change_ts"] = kind, ts


def _mark_chokepoints(node_list: list[_Node], link_list: list[dict[str, Any]]) -> None:
    """S4: flag single points of failure on the assembled graph -- articulation-point
    nodes and bridge links -- so the canvas can light a 'risk' layer. Pure topology over
    the graph we just built: no new data, no DB."""
    g = build_graph(
        [{"device_nid": n["nid"]} for n in node_list],
        [{"a_nid": e["a"], "b_nid": e["b"]} for e in link_list],
    )
    arts = find_articulation_points(g)
    bridges = find_bridges(g)
    for n in node_list:
        n["articulation"] = n["nid"] in arts
    for e in link_list:
        e["bridge"] = frozenset((e["a"], e["b"])) in bridges


def build_network_map(
    net_devices: list[dict[str, Any]],
    net_links: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    printers: list[dict[str, Any]],
    net_interfaces: Optional[list[dict[str, Any]]] = None,
    net_changes: Optional[list[dict[str, Any]]] = None,
    status_series: Optional[dict[str, list[str]]] = None,
) -> dict[str, Any]:
    """The one superset graph: nodes from net_devices + agents + printers + gateways
    (deduped by device_nid), edges from net_links + agent-uplinks (medium/quality),
    enriched with interface physics (S3) plus per-subnet anomaly and chokepoint (S4)
    overlays. Pure over already-read inputs."""
    nodes, by_mac, by_ip, by_device_id, by_printer_id = _seed_net_devices(net_devices)
    _merge_agents(nodes, snapshots, by_mac, by_ip, by_device_id)
    _merge_printers(nodes, printers, by_mac, by_ip, by_printer_id)
    links = _real_links(net_links, nodes, _index_interfaces(net_interfaces or []))
    uplinks, subnets = _agent_uplinks(snapshots, nodes, by_mac, by_ip, by_device_id)
    links += uplinks
    node_list = sorted(nodes.values(), key=lambda n: n["nid"])
    link_list = sorted(links, key=lambda e: (e["link_kind"], e["a"], e["b"]))
    _mark_chokepoints(node_list, link_list)
    _attach_overlays(node_list, link_list, net_changes, status_series)
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
