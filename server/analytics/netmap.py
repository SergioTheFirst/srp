"""Phase-2 network map: pure, deterministic fleet aggregation (D1/D2/D9).

Gateway-keyed clusters from each agent's latest network snapshot: the agents'
own adapter MACs are the identity layer (a neighbor whose MAC belongs to a known
agent is that machine, never an "unknown device"); remaining ARP neighbors are
agentless devices, vendor-hinted via the OUI seed. Per-cluster gateway-probe
quality powers the subnet anomaly: the whole subnet losing packets points at
infrastructure (switch/router/uplink), not at individual PCs.

Read-side only (D7): called from the map page / API / device page -- never from
recompute_scores, so cross-device work cannot run on every ingest.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Optional

from server.analytics.oui import normalize_mac, vendor_for_mac

# D9: >=2 reporting agents and >=60% of them losing >=20% to the gateway.
_ANOMALY_MIN_REPORTING = 2
_ANOMALY_SHARE = 0.6
_DEGRADED_LOSS_PCT = 20.0


def _subnet_hint(gateway: str) -> str:
    parts = gateway.split(".")
    return ".".join(parts[:3]) + ".x" if len(parts) == 4 else gateway


def _ip_key(ip: Optional[str]) -> tuple:
    try:
        return (0, tuple(int(p) for p in (ip or "").split(".")))
    except ValueError:
        return (1, (ip or "",))


def agent_mac_index(snapshots: list[dict[str, Any]]) -> dict[str, str]:
    """MAC -> device_id over every adapter of every agent (identity layer, D2).

    The single source of truth for the agent-MAC identity layer: ``inventory`` and
    ``scheduler`` derive their MAC sets from this map's keys (no parallel copy).
    """
    out: dict[str, str] = {}
    for snap in snapshots:
        for a in snap.get("adapters") or []:
            mac = normalize_mac(a.get("mac")) if isinstance(a, dict) else None
            if mac:
                out[mac] = snap["device_id"]
    return out


def _icmp_filtered(rows: list[dict[str, Any]]) -> bool:
    """Every probe lost 100% with no reply -> firewall vs outage is undecidable (D5)."""
    if not rows:
        return False
    return all((q.get("loss_pct") or 0.0) >= 100.0 and q.get("latency_ms") is None for q in rows)


def _gateway_quality(snap: dict[str, Any], gateway: str) -> Optional[dict[str, Any]]:
    """The device's probe row for *this* gateway, or None when absent/ambiguous.

    No gateway-targeted probe -> None (never reported). Whole-device ICMP
    ambiguity (every probe unanswered, D5) -> None too: an unanswerable device
    must not be counted as a degraded reporter in the subnet cohort.
    """
    rows = [q for q in (snap.get("quality") or []) if isinstance(q, dict)]
    gw_rows = [q for q in rows if q.get("target_kind") == "gateway" and q.get("target") == gateway]
    if not gw_rows or _icmp_filtered(rows):
        return None
    return gw_rows[0]


def _new_cluster(gw: str) -> dict[str, Any]:
    return {
        "gateway": gw,
        "subnet_hint": _subnet_hint(gw),
        "gateway_mac": None,
        "gateway_vendor": None,
        "agents": [],
        "others": {},
        "losses": [],
        "latencies": [],
    }


def _attach_agent(c: dict[str, Any], snap: dict[str, Any], adapters: list[dict[str, Any]]) -> None:
    q = _gateway_quality(snap, c["gateway"])
    loss = q.get("loss_pct") if q else None
    lat = q.get("latency_ms") if q else None
    a0 = adapters[0] if adapters else {}
    c["agents"].append(
        {
            "device_id": snap["device_id"],
            "hostname": snap.get("hostname"),
            "ip": (a0.get("ipv4") or [None])[0],
            "mac": normalize_mac(a0.get("mac")),
            "kind": a0.get("kind"),
            "up": a0.get("up"),
            "loss_pct": loss,
            "latency_ms": lat,
            "last_seen": snap.get("last_seen"),
        }
    )
    if loss is not None:
        c["losses"].append(float(loss))
    if lat is not None:
        c["latencies"].append(float(lat))


def _attach_neighbors(
    c: dict[str, Any], snap: dict[str, Any], mac_to_device: dict[str, str]
) -> None:
    """Extract the gateway's MAC/vendor from this agent's ARP neighbours.

    Agentless ARP-only devices are deliberately NOT collected (owner 2026-06-22):
    the map shows agents, gateways and discovered printers, never the unidentified
    ARP cloud. ``c["others"]`` therefore stays empty -- the key is kept so the map
    and the ``/api/v1/netmap`` response keep a stable shape.
    """
    for n in snap.get("neighbors") or []:
        if not isinstance(n, dict):
            continue
        mac = normalize_mac(n.get("mac"))
        if mac and mac in mac_to_device:
            continue  # a known agent: shown via its own snapshot, never "unknown"
        if n.get("ip") == c["gateway"]:
            c["gateway_mac"] = mac
            c["gateway_vendor"] = vendor_for_mac(mac)


def _finalize(c: dict[str, Any]) -> dict[str, Any]:
    reporting = [a for a in c["agents"] if a["loss_pct"] is not None]
    degraded = [a for a in reporting if a["loss_pct"] >= _DEGRADED_LOSS_PCT]
    anomaly = (
        len(reporting) >= _ANOMALY_MIN_REPORTING
        and len(degraded) / len(reporting) >= _ANOMALY_SHARE
    )
    reason = None
    if anomaly:
        reason = (
            f"{len(degraded)} из {len(reporting)} машин подсети теряют пакеты до шлюза — "
            "похоже на проблему инфраструктуры (свитч/роутер/линк), а не отдельных ПК"
        )
    return {
        "gateway": c["gateway"],
        "subnet_hint": c["subnet_hint"],
        "gateway_mac": c["gateway_mac"],
        "gateway_vendor": c["gateway_vendor"],
        "agents": sorted(c["agents"], key=lambda a: (a.get("hostname") or "", a["device_id"])),
        "others": sorted(
            (
                {
                    "ip": n["ip"],
                    "mac": n["mac"],
                    "vendor": n["vendor"],
                    "state": n["state"],
                    "seen_by": len(n["_seen"]),
                }
                for n in c["others"].values()
            ),
            key=lambda n: _ip_key(n.get("ip")),
        ),
        "quality": {
            "reporting": len(reporting),
            "degraded": len(degraded),
            "median_loss_pct": round(median(c["losses"]), 1) if c["losses"] else None,
            "median_latency_ms": round(median(c["latencies"]), 1) if c["latencies"] else None,
        },
        "anomaly": anomaly,
        "anomaly_reason": reason,
    }


def build_netmap(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """The whole-fleet map: clusters by gateway + agentless unclustered tail."""
    mac_to_device = agent_mac_index(snapshots)
    clusters: dict[str, dict[str, Any]] = {}
    unclustered: list[dict[str, Any]] = []

    for snap in snapshots:
        adapters = [a for a in (snap.get("adapters") or []) if isinstance(a, dict)]
        gateways = sorted({str(a["gateway"]) for a in adapters if a.get("gateway")})
        for gw in gateways:
            c = clusters.setdefault(gw, _new_cluster(gw))
            _attach_agent(c, snap, [a for a in adapters if a.get("gateway") == gw])
        if gateways:
            # Ф1 neighbors carry no iface -> attach to the observer's first gateway.
            _attach_neighbors(clusters[gateways[0]], snap, mac_to_device)
        else:
            unclustered.append({"device_id": snap["device_id"], "hostname": snap.get("hostname")})

    out = [_finalize(clusters[gw]) for gw in sorted(clusters)]
    return {
        "clusters": out,
        "unclustered": unclustered,
        "totals": {
            "agents": len({s["device_id"] for s in snapshots}),
            "others": sum(len(c["others"]) for c in out),
            "clusters": len(out),
            "anomalies": sum(1 for c in out if c["anomaly"]),
        },
    }


def subnet_context_for(snapshots: list[dict[str, Any]], device_id: str) -> Optional[str]:
    """RU annotation for the device page when the device sits in an anomalous subnet (D8).

    Builds the map only over snapshots sharing a gateway with this device (review:
    the device page must not pay for unrelated subnets); the cohort and its
    anomaly verdict are identical to the full map's for those clusters.
    """
    mine = next((s for s in snapshots if s["device_id"] == device_id), None)
    if mine is None:
        return None
    my_gateways = {
        str(a["gateway"])
        for a in mine.get("adapters") or []
        if isinstance(a, dict) and a.get("gateway")
    }
    if not my_gateways:
        return None
    subset = [
        s
        for s in snapshots
        if any(
            isinstance(a, dict) and a.get("gateway") in my_gateways for a in s.get("adapters") or []
        )
    ]
    for c in build_netmap(subset)["clusters"]:
        if c["anomaly"] and any(a["device_id"] == device_id for a in c["agents"]):
            return f"Подсеть {c['subnet_hint']} (шлюз {c['gateway']}): {c['anomaly_reason']}"
    return None
