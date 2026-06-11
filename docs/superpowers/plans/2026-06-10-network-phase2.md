# Network Phase 2 — Implementation Plan (map + network_risk + subnet anomaly)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Server-side Phase 2: a gateway-clustered network map, the `network_risk` Score100 axis, subnet-wide anomaly detection (infra vs PC), and promotion of `network` to a gated trust domain with a semantic validator.

**Architecture:** Pure analytics modules (`oui.py`, `netmap.py`, `network_risk.py`) follow the existing W4.2 engine pattern; one new fleet DB read (`get_network_snapshots`, latest-by-id JOIN); map + anomaly are read-side only (never in `recompute_scores` — W4.0 O(n²) lesson); the axis is wired once in `recompute_scores`. Agent and contract are untouched.

**Tech Stack:** Python 3.9 / FastAPI / SQLite / Jinja2 (autoescape) / pytest. No new dependencies.

**Design:** `docs/superpowers/specs/2026-06-10-network-phase2-design.md` (D1–D12).

## Conventions for every task

- Branch `feat/network-phase2` (created). Conventional commits, no attribution. Stage ONLY files touched by the task.
- TDD: failing test → minimal code → green → commit. While iterating run only the touched test file; the FULL gate once, in Task 9.
- Russian operator prose / English machine values (enums, lineage keys, band/confidence) — tests pin this.
- PostToolUse hook runs `ruff --fix`+`format` on every `.py` edit — accept its formatting.

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `server/analytics/oui.py` | Create | MAC normalisation + vendor seed lookup |
| `server/analytics/netmap.py` | Create | pure map builder, subnet anomaly, device annotation |
| `server/analytics/network_risk.py` | Create | `network_risk` Score100 engine |
| `server/analytics/trends.py` | Modify | `gateway_latency` direction-only trend |
| `server/trust/domains.py` | Modify | `network` domain |
| `server/trust/validators.py` | Modify | `network` material + `validate_network` |
| `server/pipeline.py` | Modify | `_extract_reading` network slice; wire axis |
| `server/analytics/diagnostics.py` | Modify | expose `network_risk` |
| `server/db.py` | Modify | `get_network_snapshots()` |
| `server/api.py` | Modify | `GET /api/v1/netmap` |
| `server/web/dashboard.py` | Modify | `/netmap` page; device subnet note |
| `server/web/templates/netmap.html` | Create | map page |
| `server/web/templates/device.html` | Modify | axis card + quality table + note |
| `server/web/templates/base.html` | Modify | nav link |
| `tests/test_netmap.py` | Create | oui + builder + anomaly (pure) |
| `tests/test_network_risk.py` | Create | engine unit tests |
| `tests/test_netmap_web.py` | Create | db helper + API + pages integration |
| `tests/test_network_ingest_trust.py` | Rewrite | phase-2 trust invariants |
| `tests/test_analytics_trends.py` | Modify | gateway_latency trend |
| `CHANGELOG.md` | Modify | Added line |

---

### Task 1: OUI seed + MAC normalisation (`server/analytics/oui.py`)

**Files:** Create `server/analytics/oui.py`; Create `tests/test_netmap.py` (oui section).

- [ ] **Step 1: Write the failing tests** — `tests/test_netmap.py`:

```python
"""Phase-2 network map: OUI seed, pure builder, subnet anomaly (no DB)."""

from __future__ import annotations

import pytest
from server.analytics.oui import normalize_mac, vendor_for_mac

pytestmark = pytest.mark.unit


def test_normalize_mac_forms():
    assert normalize_mac("00:50:56:aa:bb:cc") == "00-50-56-AA-BB-CC"
    assert normalize_mac("0050.56aa.bbcc") == "00-50-56-AA-BB-CC"
    assert normalize_mac("00-50-56-AA-BB-CC") == "00-50-56-AA-BB-CC"
    assert normalize_mac("garbage") is None
    assert normalize_mac("") is None
    assert normalize_mac(None) is None


def test_vendor_seed_hit_and_honest_unknown():
    assert vendor_for_mac("00:50:56:01:02:03") == "VMware"
    assert vendor_for_mac("B8-27-EB-99-88-77") == "Raspberry Pi"
    assert vendor_for_mac("F4-39-09-11-22-33") is None  # unknown OUI -> no invented vendor
    assert vendor_for_mac(None) is None
```

- [ ] **Step 2: Run** `python -m pytest tests/test_netmap.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `server/analytics/oui.py`:

```python
"""OUI -> vendor seed lookup for the network map (Phase 2, D3).

A deliberately tiny, high-confidence seed (virtualisation + Raspberry Pi) in the
spirit of _KNOWN_BAD_FIRMWARE: a hook, not a platform. The real fleet list is
curated out-of-band; an unknown OUI honestly returns None — UNKNOWN over an
invented vendor name. Keys are the first three MAC octets, normalised "AA-BB-CC".
"""

from __future__ import annotations

import re
from typing import Optional

_NON_HEX = re.compile(r"[^0-9A-F]")

_VENDOR_SEED: dict[str, str] = {
    "00-50-56": "VMware",
    "00-0C-29": "VMware",
    "00-05-69": "VMware",
    "00-15-5D": "Microsoft Hyper-V",
    "08-00-27": "VirtualBox",
    "52-54-00": "QEMU/KVM",
    "B8-27-EB": "Raspberry Pi",
    "DC-A6-32": "Raspberry Pi",
    "E4-5F-01": "Raspberry Pi",
}


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """Uppercase dash-separated AA-BB-CC-DD-EE-FF, or None when not a MAC."""
    if not mac:
        return None
    digits = _NON_HEX.sub("", mac.upper())
    if len(digits) != 12:
        return None
    return "-".join(digits[i : i + 2] for i in range(0, 12, 2))


def vendor_for_mac(mac: Optional[str]) -> Optional[str]:
    norm = normalize_mac(mac)
    if norm is None:
        return None
    return _VENDOR_SEED.get(norm[:8])
```

- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** `feat(analytics): OUI vendor seed + MAC normalisation (network map phase 2)`.

---

### Task 2: Pure map builder + subnet anomaly (`server/analytics/netmap.py`)

**Files:** Create `server/analytics/netmap.py`; extend `tests/test_netmap.py`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_netmap.py`:

```python
from server.analytics.netmap import build_netmap, subnet_context_for


def _snap(did, gw="192.168.1.1", ip="192.168.1.10", mac="AA-BB-CC-00-00-01",
          loss=0.0, lat=1.0, neighbors=None, quality=None, adapters=None):
    if adapters is None:
        adapters = [{"name": "Ethernet", "kind": "ethernet", "mac": mac, "up": True,
                     "ipv4": [ip], "gateway": gw}]
    if quality is None:
        quality = [{"target_kind": "gateway", "target": gw, "latency_ms": lat,
                    "loss_pct": loss, "samples": 3}]
    return {"device_id": did, "hostname": f"pc-{did}", "site_code": None,
            "site_name": None, "last_seen": "2026-06-10T00:00:00+00:00",
            "adapters": adapters, "neighbors": neighbors or [], "quality": quality}


def test_same_gateway_one_cluster_agents_merged_by_mac():
    s1 = _snap("d1", mac="AA-BB-CC-00-00-01",
               neighbors=[{"ip": "192.168.1.11", "mac": "aa:bb:cc:00:00:02", "state": "Reachable"}])
    s2 = _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02")
    m = build_netmap([s1, s2])
    assert m["totals"]["clusters"] == 1
    c = m["clusters"][0]
    assert c["gateway"] == "192.168.1.1"
    assert c["subnet_hint"] == "192.168.1.x"
    assert {a["device_id"] for a in c["agents"]} == {"d1", "d2"}
    assert c["others"] == []  # d2's MAC matched an agent -> never an "unknown device"


def test_unknown_neighbor_union_dedup_and_gateway_extraction():
    n_unknown = {"ip": "192.168.1.50", "mac": "00:50:56:00:00:09", "state": "Stale"}
    n_gw = {"ip": "192.168.1.1", "mac": "DE-AD-BE-EF-00-01", "state": "Reachable"}
    m = build_netmap([
        _snap("d1", neighbors=[n_unknown, n_gw]),
        _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", neighbors=[n_unknown]),
    ])
    c = m["clusters"][0]
    assert len(c["others"]) == 1
    other = c["others"][0]
    assert other["seen_by"] == 2 and other["vendor"] == "VMware"
    assert c["gateway_mac"] == "DE-AD-BE-EF-00-01"  # router shown in header, not others
    assert m["totals"]["others"] == 1


def test_subnet_anomaly_threshold():
    bad = build_netmap([_snap("d1", loss=30.0), _snap("d2", ip="192.168.1.11",
                        mac="AA-BB-CC-00-00-02", loss=40.0)])
    assert bad["clusters"][0]["anomaly"] is True
    assert "инфраструктур" in bad["clusters"][0]["anomaly_reason"]
    ok = build_netmap([_snap("d1", loss=30.0), _snap("d2", ip="192.168.1.11",
                       mac="AA-BB-CC-00-00-02", loss=0.0),
                       _snap("d3", ip="192.168.1.12", mac="AA-BB-CC-00-00-03", loss=0.0)])
    assert ok["clusters"][0]["anomaly"] is False
    single = build_netmap([_snap("d1", loss=90.0)])
    assert single["clusters"][0]["anomaly"] is False  # cohort < 2 never alarms


def test_icmp_filtered_device_not_counted_as_reporting():
    filtered = _snap("d1", quality=[{"target_kind": "gateway", "target": "192.168.1.1",
                                     "latency_ms": None, "loss_pct": 100.0, "samples": 3}])
    m = build_netmap([filtered, _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=25.0)])
    q = m["clusters"][0]["quality"]
    assert q["reporting"] == 1 and q["degraded"] == 1
    assert m["clusters"][0]["anomaly"] is False  # 1 reporting < min cohort


def test_no_gateway_goes_unclustered_and_context_annotation():
    nogw = _snap("d3", adapters=[{"name": "eth", "kind": "ethernet",
                                  "mac": "AA-BB-CC-00-00-03", "up": True,
                                  "ipv4": ["10.0.0.5"], "gateway": None}], quality=[])
    snaps = [_snap("d1", loss=30.0),
             _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0), nogw]
    m = build_netmap(snaps)
    assert [u["device_id"] for u in m["unclustered"]] == ["d3"]
    assert subnet_context_for(snaps, "d1") is not None
    assert "192.168.1.x" in subnet_context_for(snaps, "d1")
    assert subnet_context_for(snaps, "d3") is None
```

- [ ] **Step 2: Run** → FAIL (no module).
- [ ] **Step 3: Implement** `server/analytics/netmap.py`:

```python
"""Phase-2 network map: pure, deterministic fleet aggregation (D1/D2/D9).

Gateway-keyed clusters from each agent's latest network snapshot: the agents'
own adapter MACs are the identity layer (a neighbor whose MAC belongs to a known
agent is that machine, never an "unknown device"); remaining ARP neighbors are
agentless devices, vendor-hinted via the OUI seed. Per-cluster gateway-probe
quality powers the subnet anomaly: the whole subnet losing packets points at
infrastructure (switch/router/uplink), not at individual PCs.

Read-side only (D7): called from the map page / API / device page — never from
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


def _agent_macs(snapshots: list[dict[str, Any]]) -> dict[str, str]:
    """MAC -> device_id over every adapter of every agent (identity layer, D2)."""
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
    return all(
        (q.get("loss_pct") or 0.0) >= 100.0 and q.get("latency_ms") is None for q in rows
    )


def _gateway_quality(snap: dict[str, Any], gateway: str) -> Optional[dict[str, Any]]:
    rows = [q for q in (snap.get("quality") or []) if isinstance(q, dict)]
    if _icmp_filtered(rows):
        return None
    for q in rows:
        if q.get("target_kind") == "gateway" and q.get("target") == gateway:
            return q
    return None


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
    for n in snap.get("neighbors") or []:
        if not isinstance(n, dict):
            continue
        mac = normalize_mac(n.get("mac"))
        ip = n.get("ip")
        if mac and mac in mac_to_device:
            continue  # a known agent: shown via its own snapshot, never "unknown"
        if ip == c["gateway"]:
            c["gateway_mac"] = mac
            c["gateway_vendor"] = vendor_for_mac(mac)
            continue
        node = c["others"].setdefault(
            mac or f"ip:{ip}",
            {
                "ip": ip,
                "mac": mac,
                "vendor": vendor_for_mac(mac),
                "state": n.get("state"),
                "seen_by": 0,
            },
        )
        node["seen_by"] += 1


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
        "others": sorted(c["others"].values(), key=lambda n: _ip_key(n.get("ip"))),
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
    mac_to_device = _agent_macs(snapshots)
    clusters: dict[str, dict[str, Any]] = {}
    unclustered: list[dict[str, Any]] = []

    for snap in snapshots:
        adapters = [a for a in (snap.get("adapters") or []) if isinstance(a, dict)]
        gateways = sorted({a.get("gateway") for a in adapters if a.get("gateway")})
        for gw in gateways:
            c = clusters.setdefault(gw, _new_cluster(gw))
            _attach_agent(c, snap, [a for a in adapters if a.get("gateway") == gw])
        if gateways:
            # Ф1 neighbors carry no iface -> attach to the observer's first gateway.
            _attach_neighbors(clusters[gateways[0]], snap, mac_to_device)
        else:
            unclustered.append(
                {"device_id": snap["device_id"], "hostname": snap.get("hostname")}
            )

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
    """RU annotation for the device page when the device sits in an anomalous subnet (D8)."""
    for c in build_netmap(snapshots)["clusters"]:
        if c["anomaly"] and any(a["device_id"] == device_id for a in c["agents"]):
            return f"Подсеть {c['subnet_hint']} (шлюз {c['gateway']}): {c['anomaly_reason']}"
    return None
```

- [ ] **Step 4: Run** `python -m pytest tests/test_netmap.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(analytics): pure network map builder + subnet anomaly (phase 2)`.

---

### Task 3: `db.get_network_snapshots()`

**Files:** Modify `server/db.py` (after `get_recent_heartbeats`); Create `tests/test_netmap_web.py` (db section).

- [ ] **Step 1: Failing test** — `tests/test_netmap_web.py`:

```python
"""Phase-2 integration: network snapshots, /netmap API + pages."""

from __future__ import annotations

import pytest
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _net_payload(ip="192.168.1.10", mac="AA-BB-CC-00-00-01", gw="192.168.1.1", loss=0.0):
    p = healthy("historical")
    p["network_adapters"] = [{"name": "Ethernet", "kind": "ethernet", "mac": mac,
                              "up": True, "ipv4": [ip], "gateway": gw}]
    p["network_neighbors"] = [{"ip": "192.168.1.50", "mac": "00-50-56-00-00-09",
                               "state": "Reachable"}]
    p["network_quality"] = [{"target_kind": "gateway", "target": gw,
                             "latency_ms": 1.5, "loss_pct": loss, "samples": 3}]
    return p


def _ingest(client, did, payload):
    env = {"device_id": did, "agent_version": "0.1.0", "msg_type": "historical",
           "payload": payload,
           "source_health": {"network": {"status": "ok",
                                         "collected_at": "2026-06-10T00:00:00+00:00"}}}
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 200, r.text


def test_get_network_snapshots_skips_networkless(client):
    from server import db
    _ingest(client, "map-01", _net_payload())
    _ingest(client, "map-02", healthy("historical"))  # no network fields
    snaps = db.get_network_snapshots()
    assert [s["device_id"] for s in snaps] == ["map-01"]
    s = snaps[0]
    assert s["adapters"][0]["gateway"] == "192.168.1.1"
    assert s["neighbors"] and s["quality"]
```

- [ ] **Step 2: Run** `python -m pytest tests/test_netmap_web.py -q` → FAIL (no attribute).
- [ ] **Step 3: Implement** in `server/db.py`:

```python
def get_network_snapshots() -> list[dict[str, Any]]:
    """Latest network snapshot per device (map + subnet-anomaly read side, D7).

    One fleet query (latest-by-id, same pattern as get_devices); devices whose
    latest historical carries no network fields are skipped.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id, d.hostname, d.site_code, d.site_name, d.last_seen,
                   h.payload AS hist_payload
            FROM devices d
            JOIN historical h ON h.device_id = d.device_id
              AND h.id = (SELECT MAX(id) FROM historical WHERE device_id = d.device_id)
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = json.loads(r["hist_payload"]) if r["hist_payload"] else {}
        adapters = payload.get("network_adapters") or []
        neighbors = payload.get("network_neighbors") or []
        quality = payload.get("network_quality") or []
        if not (adapters or neighbors or quality):
            continue
        out.append(
            {
                "device_id": r["device_id"],
                "hostname": r["hostname"],
                "site_code": r["site_code"],
                "site_name": r["site_name"],
                "last_seen": r["last_seen"],
                "adapters": adapters,
                "neighbors": neighbors,
                "quality": quality,
            }
        )
    return out
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(db): get_network_snapshots — latest network payload per device`.

---

### Task 4: `network_risk` engine (`server/analytics/network_risk.py`)

**Files:** Create `server/analytics/network_risk.py`; Create `tests/test_network_risk.py`.

- [ ] **Step 1: Failing tests** — `tests/test_network_risk.py`:

```python
"""network_risk engine: gateway quality leads, APIPA standalone, ICMP honesty."""

from __future__ import annotations

import pytest
from server.analytics.network_risk import compute_network_risk

pytestmark = pytest.mark.unit


def _hist(adapters=None, quality=None):
    return {"network_adapters": adapters or [], "network_quality": quality or []}


def _gw(loss=0.0, lat=1.0, target="192.168.1.1"):
    return {"target_kind": "gateway", "target": target, "latency_ms": lat,
            "loss_pct": loss, "samples": 3}


def _dns(loss=0.0, lat=2.0, target="192.168.1.53"):
    return {"target_kind": "dns", "target": target, "latency_ms": lat,
            "loss_pct": loss, "samples": 3}


def _eth(ip="192.168.1.10", up=True, **kw):
    return {"name": "Ethernet", "kind": "ethernet", "up": up, "ipv4": [ip],
            "gateway": "192.168.1.1", **kw}


def test_untrusted_withheld():
    s = compute_network_risk(_hist([_eth()], [_gw()]), device_trust="untrusted")
    assert s.value is None and s.band == "unknown"


def test_no_telemetry_unknown_even_when_domain_gate_failed():
    s = compute_network_risk({}, domain_state="unknown")
    assert s.value is None
    assert "нет сетевой телеметрии" in s.missing_evidence  # data-absence wins (order)


def test_domain_gate_failed_with_data_withheld():
    s = compute_network_risk(_hist([_eth()], [_gw()]), domain_state="unknown")
    assert s.value is None
    assert "гейт доверия" in s.reason


def test_healthy_gateway_confident_zero_capped_medium():
    s = compute_network_risk(_hist([_eth()], [_gw(loss=0.0, lat=1.0)]))
    assert s.value == 0.0 and s.band == "good"
    assert s.confidence == "medium"  # D11 cap
    assert s.reason == "связь со шлюзом в норме"
    assert any("за пределами шлюза" in m for m in s.missing_evidence)


@pytest.mark.parametrize("loss,expected", [(7.0, 15.0), (25.0, 30.0)])
def test_gateway_partial_loss_grades(loss, expected):
    s = compute_network_risk(_hist([_eth()], [_gw(loss=loss)]))
    assert s.value == expected


def test_gateway_full_loss_with_other_reply_is_failure():
    s = compute_network_risk(_hist([_eth()], [_gw(loss=100.0, lat=None), _dns(loss=0.0)]))
    assert s.value == 45.0 and s.band == "bad"


def test_all_probes_lost_is_icmp_ambiguity_not_alarm():
    s = compute_network_risk(
        _hist([_eth()], [_gw(loss=100.0, lat=None), _dns(loss=100.0, lat=None)])
    )
    assert s.value == 0.0
    assert s.confidence == "low"
    assert any("ICMP" in m for m in s.missing_evidence)
    assert s.factors == []
    assert s.source_lineage["icmp_blocked"] is True


def test_gateway_latency_confirmation():
    assert compute_network_risk(_hist([_eth()], [_gw(loss=0.0, lat=120.0)])).value == 15.0
    assert compute_network_risk(_hist([_eth()], [_gw(loss=0.0, lat=35.0)])).value == 8.0


def test_dns_partial_counts_once_full_loss_ignored():
    s = compute_network_risk(
        _hist([_eth()], [_gw(loss=0.0), _dns(loss=50.0), _dns(loss=60.0, target="192.168.1.54")])
    )
    assert s.value == 8.0  # worst DNS only
    s2 = compute_network_risk(_hist([_eth()], [_gw(loss=0.0), _dns(loss=100.0, lat=None)]))
    assert s2.value == 0.0  # DNS boxes commonly drop ICMP
    assert s2.source_lineage["dns_full_loss_ignored"] == 1


def test_apipa_standalone_failure():
    s = compute_network_risk(_hist([_eth(ip="169.254.10.20")], []))
    assert s.value == 35.0
    assert any("APIPA" in f["label"] for f in s.factors)
    assert s.confidence == "low"  # no quality measurement


def test_wifi_weak_signal():
    wifi = {"name": "Wi-Fi", "kind": "wifi", "up": True, "ipv4": ["192.168.1.20"],
            "gateway": "192.168.1.1", "signal_pct": 20}
    s = compute_network_risk(_hist([wifi], [_gw(loss=0.0)]))
    assert s.value == 12.0
    wifi2 = {**wifi, "signal_pct": 40}
    assert compute_network_risk(_hist([wifi2], [_gw(loss=0.0)])).value == 6.0


def test_adapters_only_low_confidence():
    s = compute_network_risk(_hist([_eth()], []))
    assert s.value == 0.0 and s.confidence == "low"
    assert "не измерено" in s.reason
    assert any("качества" in m for m in s.missing_evidence)


def test_clamped_at_100_and_deterministic():
    h = _hist([_eth(ip="169.254.1.2"), _eth(ip="169.254.1.3")],
              [_gw(loss=100.0, lat=None), _dns(loss=0.0)])
    s1, s2 = compute_network_risk(h), compute_network_risk(h)
    assert s1.value == 100.0 and s1 == s2
```

- [ ] **Step 2: Run** `python -m pytest tests/test_network_risk.py -q` → FAIL.
- [ ] **Step 3: Implement** `server/analytics/network_risk.py`:

```python
"""Phase-2 network health engine: current-state verdict for one device.

Leading signal = measured quality to the *gateway* — the one target that must
answer (graded packet loss; latency confirms). APIPA (169.254.x on an up adapter)
is a strong standalone failure: DHCP never answered, the NIC has no real network.
Weak Wi-Fi signal contributes mildly. ICMP honesty (D5): when every probe of the
machine lost 100% with no reply, firewall-vs-outage is undecidable from this
vantage -> blind spot in missing_evidence, never an alarm; full loss only to DNS
targets while the gateway answers is likewise ignored (DNS boxes drop ICMP).

Confidence caps at medium (D11): one vantage point and coarse ICMP cannot prove
the path beyond the gateway. Gating mirrors the other W4.2 engines: untrusted
identity withholds; absent telemetry -> UNKNOWN (checked first, so an old agent
reads "no data" not "gate failed"); a gate-failed network trust domain withholds.
The latency *trend* lives in trends.py (gateway_latency); this is current state.
"""

from __future__ import annotations

from typing import Any, Optional

from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

_BLIND_SPOT = "видимость только с этой машины: путь за пределами шлюза не наблюдается"

_GW_LOSS_FULL = 45.0
_GW_LOSS_HEAVY = 30.0  # >= 20% loss
_GW_LOSS_LIGHT = 15.0  # >= 5% loss
_GW_LAT_HIGH = 15.0  # >= 100 ms
_GW_LAT_WARN = 8.0  # >= 30 ms
_DNS_PARTIAL = 8.0
_APIPA = 35.0
_WIFI_WEAK = 12.0  # < 30%
_WIFI_LOW = 6.0  # < 50%


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _icmp_filtered(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    return all(
        (_f(q.get("loss_pct")) or 0.0) >= 100.0 and q.get("latency_ms") is None for q in rows
    )


def _withheld(missing: list[str], lineage: dict[str, Any], reason: str) -> Score100:
    return make_score100(
        None,
        "higher_is_worse",
        "unknown",
        "unknown",
        missing_evidence=missing,
        source_lineage=lineage,
        reason=reason,
    )


def compute_network_risk(
    historical: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
    domain_state: Optional[str] = None,
) -> Score100:
    """Deterministic network risk for one device (0..100, higher = worse)."""
    direction: Direction = "higher_is_worse"

    if device_trust == "untrusted":
        return _withheld(
            ["идентификация не подтверждена"],
            {"identity": "untrusted"},
            "идентификатор устройства не подтверждён (контракт §7)",
        )

    hist = historical or {}
    adapters = [a for a in (hist.get("network_adapters") or []) if isinstance(a, dict)]
    quality = [q for q in (hist.get("network_quality") or []) if isinstance(q, dict)]
    if not adapters and not quality:
        return _withheld(
            ["нет сетевой телеметрии"],
            {},
            "агент не передал сетевые данные (UNKNOWN — ложная уверенность недопустима)",
        )

    if domain_state == "unknown":
        return _withheld(
            ["источник network не прошёл проверку доверия"],
            {"network_domain": "unknown"},
            "сетевой источник не прошёл гейт доверия — оценка скрыта",
        )

    value = 0.0
    factors: list[Factor] = []
    missing: list[str] = [_BLIND_SPOT]

    def hit(label: str, delta: float) -> None:
        nonlocal value
        value += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    icmp_blocked = _icmp_filtered(quality)
    usable = [] if icmp_blocked else quality
    if icmp_blocked:
        missing.append(
            "все пробы без ответа: либо ICMP блокируется фаерволом, либо связи нет — "
            "отличить с одной машины нельзя"
        )

    gw_rows = [q for q in usable if q.get("target_kind") == "gateway"]
    dns_rows = [q for q in usable if q.get("target_kind") == "dns"]

    gw_measured = False
    worst_gw = max(gw_rows, key=lambda q: _f(q.get("loss_pct")) or 0.0, default=None)
    if worst_gw is not None and _f(worst_gw.get("loss_pct")) is not None:
        gw_measured = True
        loss = _f(worst_gw.get("loss_pct")) or 0.0
        lat = _f(worst_gw.get("latency_ms"))
        target = worst_gw.get("target")
        if loss >= 100.0:
            hit(f"шлюз {target} не отвечает на ping (потери 100%)", _GW_LOSS_FULL)
        else:
            if loss >= 20.0:
                hit(f"потери до шлюза {target}: {loss:.0f}%", _GW_LOSS_HEAVY)
            elif loss >= 5.0:
                hit(f"потери до шлюза {target}: {loss:.0f}%", _GW_LOSS_LIGHT)
            if lat is not None and lat >= 100.0:
                hit(f"высокая задержка до шлюза: {lat:.0f} мс", _GW_LAT_HIGH)
            elif lat is not None and lat >= 30.0:
                hit(f"повышенная задержка до шлюза: {lat:.0f} мс", _GW_LAT_WARN)
    elif not icmp_blocked:
        missing.append("нет измерений качества связи до шлюза")

    dns_partial = [
        q for q in dns_rows if 5.0 <= (_f(q.get("loss_pct")) or 0.0) < 100.0
    ]
    if dns_partial:
        worst_dns = max(dns_partial, key=lambda q: _f(q.get("loss_pct")) or 0.0)
        hit(
            f"потери до DNS {worst_dns.get('target')}: "
            f"{(_f(worst_dns.get('loss_pct')) or 0.0):.0f}%",
            _DNS_PARTIAL,
        )
    dns_full_ignored = sum(1 for q in dns_rows if (_f(q.get("loss_pct")) or 0.0) >= 100.0)

    for a in adapters:
        if a.get("up") and any(str(ip).startswith("169.254.") for ip in (a.get("ipv4") or [])):
            hit(
                f"адаптер «{a.get('name') or '?'}» без DHCP-адреса (APIPA 169.254.x) — "
                "сети фактически нет",
                _APIPA,
            )

    for a in adapters:
        sig = _f(a.get("signal_pct"))
        if a.get("up") and a.get("kind") == "wifi" and sig is not None:
            if sig < 30.0:
                hit(f"слабый сигнал Wi-Fi: {sig:.0f}%", _WIFI_WEAK)
            elif sig < 50.0:
                hit(f"невысокий сигнал Wi-Fi: {sig:.0f}%", _WIFI_LOW)

    value = max(0.0, min(100.0, value))
    confidence: ScoreConfidence = "medium" if gw_measured else "low"

    reason = ""
    if value == 0.0:
        reason = (
            "связь со шлюзом в норме"
            if gw_measured
            else "тревожных сигналов нет, но качество связи не измерено"
        )

    lineage = {
        "adapters_total": len(adapters),
        "adapters_up": sum(1 for a in adapters if a.get("up")),
        "quality_targets": len(quality),
        "gateway_measured": gw_measured,
        "icmp_blocked": icmp_blocked,
        "dns_full_loss_ignored": dns_full_ignored,
    }
    return make_score100(
        value,
        direction,
        band_for_risk_score(value),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage=lineage,
        reason=reason,
    )
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(analytics): network_risk engine — gateway quality, APIPA, ICMP honesty`.

---

### Task 5: trust flip — domain + validator + reading slice

**Files:** Modify `server/trust/domains.py`, `server/trust/validators.py`, `server/pipeline.py` (`_extract_reading`); Rewrite `tests/test_network_ingest_trust.py`.

- [ ] **Step 1: Rewrite tests** — `tests/test_network_ingest_trust.py` (phase-2 invariants replace phase-1):

```python
"""Phase-2 invariants: 'network' is a gated trust domain with a range validator;
day-1 health axes stay independent of it."""

from __future__ import annotations

import pytest
from server.trust.domains import DOMAIN_SOURCES
from server.trust.states import SemanticStatus
from server.trust.validators import validate_source
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-06-10T00:00:00+00:00"}


def test_network_is_a_trust_domain():
    assert DOMAIN_SOURCES["network"] == {"required": ["network"], "optional": []}


@pytest.mark.parametrize(
    "reading,expected",
    [
        ({"quality": [{"loss_pct": 0.0, "latency_ms": 1.0}]}, SemanticStatus.PLAUSIBLE),
        ({"quality": [{"loss_pct": 150.0}]}, SemanticStatus.IMPLAUSIBLE),
        ({"quality": [{"latency_ms": -5.0}]}, SemanticStatus.IMPLAUSIBLE),
        ({"signal_pcts": [200]}, SemanticStatus.IMPLAUSIBLE),
        ({}, SemanticStatus.PLAUSIBLE),
    ],
)
def test_validate_network_ranges(reading, expected):
    status, _ = validate_source("network", reading, None)
    assert status is expected


def _net_env(did, loss=0.0):
    payload = healthy("historical")
    payload["network_adapters"] = [
        {"name": "Ethernet", "kind": "ethernet", "up": True,
         "ipv4": ["192.168.1.5"], "gateway": "192.168.1.1"}
    ]
    payload["network_quality"] = [
        {"target_kind": "gateway", "target": "192.168.1.1",
         "latency_ms": 1.0, "loss_pct": loss, "samples": 3}
    ]
    sh = {"storage_reliability": _sh("ok"), "battery": _sh("ok"),
          "reliability": _sh("ok"), "boot_time": _sh("ok"), "network": _sh("ok")}
    return {"device_id": did, "agent_version": "0.1.0", "msg_type": "historical",
            "payload": payload, "source_health": sh}


def test_network_domain_trusted_and_axis_scored(client):
    from server import db

    resp = client.post("/api/v1/ingest", json=_net_env("net2-ok"))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("net2-ok")
    assert trust["domains"]["network"]["state"] == "trusted"
    s100 = db.get_device("net2-ok")["scores"]["risk"]["score100"]
    assert s100["network_risk"]["value"] == 0.0
    assert s100["network_risk"]["confidence"] == "medium"
    # day-1 health axes never depend on the network domain
    assert s100["reliability"]["value"] is not None
    assert s100["wear"]["value"] is not None


def test_implausible_quality_gates_axis_but_not_day1(client):
    from server import db

    resp = client.post("/api/v1/ingest", json=_net_env("net2-bad", loss=500.0))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("net2-bad")
    assert trust["domains"]["network"]["state"] == "unknown"
    s100 = db.get_device("net2-bad")["scores"]["risk"]["score100"]
    assert s100["network_risk"]["value"] is None  # gate-failed -> withheld
    assert s100["reliability"]["value"] is not None  # day-1 untouched


def test_old_agent_without_network_reads_no_data_and_lower_observability(client):
    from server import db

    payload = healthy("historical")
    sh = {"storage_reliability": _sh("ok"), "battery": _sh("ok"),
          "reliability": _sh("ok"), "boot_time": _sh("ok")}
    env = {"device_id": "net2-old", "agent_version": "0.1.0", "msg_type": "historical",
           "payload": payload, "source_health": sh}
    assert client.post("/api/v1/ingest", json=env).status_code == 200
    s100 = db.get_device("net2-old")["scores"]["risk"]["score100"]
    assert s100["network_risk"]["value"] is None
    assert "нет сетевой телеметрии" in s100["network_risk"]["missing_evidence"]
    obs = s100["observability"]["value"]
    assert obs is not None and obs < 100.0  # the network blind spot now counts (D12)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_network_ingest_trust.py -q` → FAIL.
- [ ] **Step 3: Implement.** `server/trust/domains.py` — add to `DOMAIN_SOURCES`:

```python
    "network": {"required": ["network"], "optional": []},
```

`server/trust/validators.py` — add `"network"` to `MATERIAL_SOURCES`, add validator + dispatch in `validate_source`:

```python
def validate_network(reading: dict) -> Result:
    """Stateless range checks over the quality probes + Wi-Fi signal (Phase 2).

    The network source feeds the network_risk axis (decision-material), so
    garbage must not pass: loss outside 0..100, negative/absurd latency or a
    signal% outside 0..100 mark the source IMPLAUSIBLE.
    """
    for q in reading.get("quality") or []:
        if not isinstance(q, dict):
            continue
        for key, lo, hi in (("loss_pct", 0.0, 100.0), ("latency_ms", 0.0, 60000.0)):
            status, reason = validate_scalar_range(f"network.{key}", q.get(key), lo, hi)
            if status is not SemanticStatus.PLAUSIBLE:
                return status, reason
    for sig in reading.get("signal_pcts") or []:
        status, reason = validate_scalar_range("network.signal_pct", sig, 0.0, 100.0)
        if status is not SemanticStatus.PLAUSIBLE:
            return status, reason
    return _OK
```

and in `validate_source` (before the final return):

```python
    if source == "network":
        return validate_network(reading)
```

`server/pipeline.py` `_extract_reading` — before the final `return {}`:

```python
    if source == "network":
        # Decision-material slice only (quality probes + Wi-Fi signal); neighbors/
        # connections are bulk map data and stay out of last_good.
        adapters = payload.get("network_adapters") or []
        return {
            "quality": payload.get("network_quality") or [],
            "adapters_count": len(adapters),
            "signal_pcts": [
                a.get("signal_pct")
                for a in adapters
                if isinstance(a, dict) and a.get("signal_pct") is not None
            ],
        }
```

- [ ] **Step 4:** integration asserts on `network_risk` stay RED until Task 6 — run only the unit part: `python -m pytest tests/test_network_ingest_trust.py -q -k "domain or ranges"` → PASS.
- [ ] **Step 5: Commit** `feat(trust): network becomes a gated domain with range validator (phase 2)`.

---

### Task 6: wire the axis in `recompute_scores` + diagnostics

**Files:** Modify `server/pipeline.py` (`recompute_scores`), `server/analytics/diagnostics.py`.

- [ ] **Step 1:** failing tests = the two integration tests from Task 5 (`network_risk` key missing).
- [ ] **Step 2: Implement.** In `server/pipeline.py` imports: `from server.analytics.network_risk import compute_network_risk`. In `recompute_scores`, after the `fleet_anomaly_risk` line:

```python
    net_domain = ((trust or {}).get("domains") or {}).get("network") or {}
    network_risk = compute_network_risk(
        hist, device_trust=device_trust, domain_state=net_domain.get("state")
    )
```

and alongside the other `score_to_dict` lines:

```python
    risk_block["score100"]["network_risk"] = score_to_dict(network_risk)
```

`server/analytics/diagnostics.py` — add to the returned dict:

```python
        "network_risk": score100.get("network_risk"),
```

- [ ] **Step 3: Run** `python -m pytest tests/test_network_ingest_trust.py tests/test_network_risk.py -q` → PASS.
- [ ] **Step 4: Commit** `feat(scoring): network_risk axis wired into recompute_scores + diagnostics`.

---

### Task 7: `gateway_latency` trend (direction-only)

**Files:** Modify `server/analytics/trends.py`; Modify `tests/test_analytics_trends.py`.

- [ ] **Step 1: Failing test** — append to `tests/test_analytics_trends.py`:

```python
def test_gateway_latency_trend_direction_only():
    """Rising gateway latency reads worsening (no ETA — no failure boundary);
    full-loss probes are excluded; trajectory_risk value is NOT driven by it."""
    from server.analytics.trends import compute_trends, trajectory_risk_score

    def row(day, lat, loss=0.0):
        return {
            "received_at": f"2026-06-{day:02d}T00:00:00+00:00",
            "network_quality": [
                {"target_kind": "gateway", "target": "192.168.1.1",
                 "latency_ms": lat, "loss_pct": loss, "samples": 3}
            ],
        }

    series = [row(9, 80.0), row(7, 40.0), row(5, 20.0), row(3, 5.0), row(1, 1.0)]
    trends = compute_trends(series, [])
    t = trends["gateway_latency"]
    assert t.direction == "worsening"
    assert t.eta_days is None and t.slope_per_day > 0
    # full-loss probe carries no usable latency
    assert compute_trends([row(1, None, loss=100.0)], [])["gateway_latency"].n_points == 0
    # direction-only metrics never fabricate trajectory risk
    assert trajectory_risk_score(trends).value == 0.0
```

- [ ] **Step 2: Run** `python -m pytest tests/test_analytics_trends.py -q` → FAIL (KeyError).
- [ ] **Step 3: Implement** in `server/analytics/trends.py` — extractor next to `_cpu_perf_pct`:

```python
def _gateway_latency_ms(row: dict[str, Any]) -> Optional[float]:
    vals = [
        float(q["latency_ms"])
        for q in row.get("network_quality") or []
        if isinstance(q, dict)
        and q.get("target_kind") == "gateway"
        and q.get("latency_ms") is not None
        and (q.get("loss_pct") or 0.0) < 100.0
    ]
    return median(vals) if vals else None
```

(import `median` from `statistics` if not already imported) and in `compute_trends`:

```python
        "gateway_latency": build_trend(
            historical_series,
            "gateway_latency",
            _gateway_latency_ms,
            worsening_sign=1,
            now=now,
        ),
```

- [ ] **Step 4: Run** → PASS (plus the whole file: regressions on existing trends).
- [ ] **Step 5: Commit** `feat(analytics): gateway_latency direction-only trend (phase 2)`.

---

### Task 8: web — map page, API, device card, nav (+CHANGELOG)

**Files:** Modify `server/api.py`, `server/web/dashboard.py`, `server/web/templates/base.html`, `server/web/templates/device.html`; Create `server/web/templates/netmap.html`; Modify `CHANGELOG.md`; extend `tests/test_netmap_web.py`.

- [ ] **Step 1: Failing tests** — append to `tests/test_netmap_web.py`:

```python
def test_netmap_api_and_page(client):
    _ingest(client, "map-11", _net_payload(loss=30.0))
    _ingest(client, "map-12", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02",
                                           loss=40.0))
    api = client.get("/api/v1/netmap")
    assert api.status_code == 200
    m = api.json()
    assert m["totals"]["clusters"] == 1 and m["totals"]["agents"] == 2
    assert m["clusters"][0]["anomaly"] is True

    page = client.get("/netmap")
    assert page.status_code == 200
    body = page.text
    assert "Карта сети" in body and "pc-map-11" in body and "инфраструктур" in body


def test_device_page_shows_axis_and_subnet_note(client):
    _ingest(client, "map-21", _net_payload(loss=30.0))
    _ingest(client, "map-22", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02",
                                           loss=40.0))
    page = client.get("/device/map-21")
    assert page.status_code == 200
    body = page.text
    assert "Здоровье сети" in body          # axis card
    assert "инфраструктур" in body           # subnet annotation (D8)
    assert "Качество связи" in body          # probes table


def test_diagnostics_exposes_network_risk(client):
    _ingest(client, "map-31", _net_payload())
    d = client.get("/api/v1/diagnostics/map-31")
    assert d.status_code == 200
    assert d.json()["network_risk"]["value"] is not None
```

- [ ] **Step 2: Run** → FAIL (404 / missing markup).
- [ ] **Step 3: Implement.**

`server/api.py` — import + route:

```python
from server.analytics.netmap import build_netmap
```

```python
@router.get("/netmap")
def netmap() -> dict:
    """Phase-2 network map: gateway clusters, agentless neighbors, subnet anomalies."""
    return build_netmap(db.get_network_snapshots())
```

`server/web/dashboard.py` — import `from server.analytics.netmap import build_netmap, subnet_context_for`; new route + device note:

```python
@router.get("/netmap", response_class=HTMLResponse)
def network_map(request: Request):
    """Phase-2 network map page (server-rendered, D1: no graph JS library)."""
    return _TEMPLATES.TemplateResponse(
        request, "netmap.html", {"m": build_netmap(db.get_network_snapshots())}
    )
```

and in `device()` pass `"net_subnet_note": subnet_context_for(db.get_network_snapshots(), device_id)` in the template context.

`server/web/templates/base.html` — nav link after «печать»: `<a href="/netmap">карта сети</a>`.

`server/web/templates/netmap.html` — new page (extends base; totals stats; per-cluster card: header «Шлюз X · подсеть Y» + vendor + anomaly `alert-banner`, agents table with device links + loss/latency chips, others as chip grid with vendor/state; vanilla-JS substring filter over rows; no poll — data moves at historical cadence).

`server/web/templates/device.html`:
1. Axis card «Здоровье сети» after the disk-fill card (same axis-card pattern, `net_ax = (s.risk.score100 or {}).get("network_risk")`), with `{% if net_subnet_note %}<div class="axis-blind">⚠ {{ net_subnet_note }}</div>{% endif %}` inside.
2. In the «Сеть» section: a «Качество связи» probes table (Цель/Тип/Задержка/Потери) from `hist.network_quality`.

`CHANGELOG.md` `## [Unreleased]` → `### Added`:

```markdown
- **Карта сети + ось «Здоровье сети» (фаза 2)** — новая страница «карта сети»: машины сгруппированы по шлюзам, видны устройства без агента (по ARP-соседям) с подсказкой производителя; новая оценка `network_risk` на странице устройства (потери/задержка до шлюза, APIPA, слабый Wi-Fi; блокировку ICMP честно показываем как «не видно», а не как аварию); деградация всей подсети помечается как проблема инфраструктуры (свитч/роутер), а не отдельных ПК — на карте и на странице устройства; источник `network` стал доменом доверия с проверкой диапазонов (мусорные числа скрывают оценку, но не трогают остальные); тренд задержки до шлюза в «Траектории»; наблюдаемость теперь учитывает сетевое слепое пятно у старых агентов.
```

- [ ] **Step 4: Run** `python -m pytest tests/test_netmap_web.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(dashboard): network map page + device network axis card + nav (phase 2)`.

---

### Task 9: full gate + smoke + ledger

- [ ] **Step 1:** `python -m ruff check . && python -m ruff format --check .`
- [ ] **Step 2:** `python -m mypy server shared client`
- [ ] **Step 3:** `python -m bandit -q -r server shared client -c pyproject.toml`
- [ ] **Step 4:** `python -m pytest -q --cov --cov-fail-under=80` — fix any cross-test fallout (other files may pin domain sets / observability values).
- [ ] **Step 5:** `python smoke.py` → OK.
- [ ] **Step 6:** Update `CONTINUITY.md`; commit `docs: ledger — network phase 2 done`.
- [ ] **Step 7:** Subagent reviews (security-reviewer mandatory: trust/scoring/SQL surface; code-reviewer for the rest) → apply fixes → re-gate.
- [ ] **Step 8:** `git checkout main && git merge --no-ff feat/network-phase2`. Push only when the user asks.

## Definition of Done (Phase 2)

- [ ] Gate fully green (ruff · mypy[server+shared+client] · bandit · pytest cov ≥80%) + smoke OK.
- [ ] `/netmap` page + `/api/v1/netmap` live; device page shows the axis card, probes table and subnet annotation.
- [ ] `network` is a trust domain: implausible quality withholds `network_risk` but never day-1 axes.
- [ ] Map/anomaly computed read-side only (nothing new in the ingest hot path besides the pure axis).
- [ ] Agent + contract untouched (`CONTRACT_VERSION` still `0.1.0`); CHANGELOG line in branch.
