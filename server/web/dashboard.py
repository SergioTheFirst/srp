"""Server-rendered dashboard: fleet overview + device detail.

Jinja2 autoescaping is on for .html, so any device-supplied string (hostname,
model, event message) is HTML-escaped -- no stored XSS from telemetry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from shared.schema import parse_version

from server import db, org_directory
from server.analytics.netmap import subnet_context_for, subnet_hint
from server.netdisco.cache import GraphCache
from server.netdisco.unified import historical_graph_from_snapshot

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

_EMPTY_GRAPH = {
    "nodes": [],
    "links": [],
    "subnets": [],
    "totals": {
        "nodes": 0,
        "links": 0,
        "agents": 0,
        "printers": 0,
        "anomalies": 0,
        "wireless_links": 0,
    },
}


def _unified_map_graph(request: Request) -> dict:
    """Ф4: the unified network-map graph for ``/netmap`` (Ф2 assembler via the Ф3
    GraphCache). Same cache instance the API serves; a well-formed empty graph when
    the fleet is empty so the SSR inventory/canvas both degrade gracefully. The cache
    is created up-front in ``create_app``; the fallback only covers an app built
    outside it."""
    cache = getattr(request.app.state, "network_map_cache", None) or GraphCache()
    return cache.get() or _EMPTY_GRAPH


def health_color(v: Optional[float]) -> str:
    if v is None:
        return "na"
    if v >= 75:
        return "good"
    if v >= 50:
        return "warn"
    return "bad"


def risk_color(v: Optional[float]) -> str:
    if v is None:
        return "na"
    if v < 25:
        return "good"
    if v < 50:
        return "warn"
    return "bad"


def level_color(level: Optional[str]) -> str:
    return {"low": "good", "elevated": "warn", "high": "high", "critical": "bad"}.get(
        level or "", "na"
    )


def pct(v: Optional[float]) -> str:
    return f"{v * 100:.0f}%" if v is not None else "—"


def days_until(iso: Optional[str]) -> Optional[int]:
    """Whole days from now until the given ISO datetime (negative if in the past).

    Returns None if *iso* is None or cannot be parsed.
    """
    if not iso:
        return None
    s = iso.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - datetime.now(timezone.utc)
    return int(delta.total_seconds() // 86400)


def fmt_age(sec: Optional[int]) -> str:
    """Compact human age for last-contact: 45с / 12м / 3ч / 5д."""
    if sec is None:
        return "—"
    if sec <= 0:
        return "0с"
    if sec < 90:
        return f"{sec}с"
    if sec < 5400:
        return f"{sec // 60}м"
    if sec < 172800:
        return f"{sec // 3600}ч"
    return f"{sec // 86400}д"


def printer_status_ru(status: Optional[str], online: Optional[bool]) -> tuple[str, str]:
    """(chip-class, RU label) for a printer status. Stored value stays English;
    this is display-only for the operator (Russian dashboard)."""
    if not online or status == "unreachable":
        return ("bad", "недоступен")
    return {
        "idle": ("good", "готов"),
        "printing": ("accent", "печать"),
        "warmup": ("warn", "разогрев"),
        "stopped": ("bad", "остановлен"),
        "other": ("na", "—"),
        "unknown": ("na", "неизвестно"),
    }.get(status or "", ("na", status or "—"))


def supply_color(pct_left: Optional[int]) -> str:
    """Chip color for a consumed-supply % remaining (toner/ink running out)."""
    if pct_left is None:
        return "na"
    if pct_left < 10:
        return "bad"
    if pct_left < 25:
        return "warn"
    return "good"


# Stored dev_type / status / confidence stay English (machine values, tests pin
# them); these map them to operator-facing Russian + a chip colour at render time.
_NET_TYPE_RU = {
    "router": "маршрутизатор",
    "switch": "коммутатор",
    "ap": "точка доступа",
    "agent": "агент",
    "printer": "принтер",
    "endpoint": "устройство",
    "unknown": "неизвестно",
}

_NET_STATUS_RU = {
    "up": ("good", "на связи"),
    "down": ("bad", "недоступен"),
    "unreachable": ("warn", "за недоступным узлом"),
    "missing": ("na", "пропал"),
}

_NET_CHANGE_RU = {
    "device_new": "появилось устройство",
    "device_gone": "устройство пропало",
    "type_changed": "сменился тип",
    "link_new": "новая связь",
    "link_gone": "связь исчезла",
    "status_changed": "сменился статус",
}


def net_type_ru(dev_type: Optional[str]) -> str:
    return _NET_TYPE_RU.get(dev_type or "", dev_type or "неизвестно")


def net_status_ru(status: Optional[str]) -> tuple[str, str]:
    """(chip-class, RU label) for a network-device reachability status."""
    return _NET_STATUS_RU.get(status or "", ("na", "неизвестно"))


def net_conf_color(confidence: Optional[str]) -> str:
    """Chip colour for an edge/link confidence band."""
    return {"high": "good", "medium": "warn", "low": "na"}.get(confidence or "", "na")


def net_change_ru(kind: Optional[str]) -> str:
    return _NET_CHANGE_RU.get(kind or "", kind or "изменение")


_TEMPLATES.env.globals.update(
    health_color=health_color,
    risk_color=risk_color,
    level_color=level_color,
    pct=pct,
    days_until=days_until,
    fmt_age=fmt_age,
    printer_status_ru=printer_status_ru,
    supply_color=supply_color,
    net_type_ru=net_type_ru,
    net_status_ru=net_status_ru,
    net_conf_color=net_conf_color,
    net_change_ru=net_change_ru,
)


def _printer_kpis(printers: list) -> dict:
    return {
        "total": len(printers),
        "online": sum(1 for p in printers if p.get("online")),
        "pages": sum(p.get("total_pages") or 0 for p in printers),
        "low_supply": sum(
            1 for p in printers if p.get("low_supply_pct") is not None and p["low_supply_pct"] < 15
        ),
        "errors": sum(1 for p in printers if p.get("error_count")),
    }


def _device_flags(d: dict) -> list[str]:
    """Filterable status flags for one device (drive the dashboard search/KPIs)."""
    flags = []
    if (d.get("risk_exposure") or 0) >= 50:
        flags.append("at_risk")
    if d.get("worsening_count"):
        flags.append("worsening")
    if d.get("unknown_domains"):
        flags.append("unknown")
    if d.get("regressed_count"):
        flags.append("regressed")
    if d.get("stale"):
        flags.append("stale")
    if d.get("cert_expiring"):
        flags.append("expiring")
    if d.get("device_trust") == "untrusted":
        flags.append("untrusted")
    if d.get("version_outdated"):
        flags.append("outdated")
    if d.get("is_new"):
        flags.append("new")
    return flags


def _fleet_summary(devices: list) -> dict:
    return {
        "total": len(devices),
        "at_risk": sum(1 for d in devices if (d.get("risk_exposure") or 0) >= 50),
        "worsening": sum(1 for d in devices if d.get("worsening_count")),
        "unknown": sum(1 for d in devices if d.get("unknown_domains")),
        "regressed": sum(1 for d in devices if d.get("regressed_count")),
        "stale": sum(1 for d in devices if d.get("stale")),
        "expiring": sum(1 for d in devices if d.get("cert_expiring")),
        "untrusted": sum(1 for d in devices if d.get("device_trust") == "untrusted"),
        "new7d": sum(1 for d in devices if d.get("is_new")),
        "outdated": sum(1 for d in devices if d.get("version_outdated")),
    }


def _group_by_site(devices: list) -> list:
    """Group devices by site (object/firm); riskiest site first — scales the fleet."""
    groups: dict[str, list] = {}
    for d in devices:
        label = d.get("site_name") or d.get("site_code") or "— без объекта —"
        groups.setdefault(label, []).append(d)
    return sorted(
        groups.items(),
        key=lambda kv: -max((x.get("risk_exposure") or 0) for x in kv[1]),
    )


def _identity_labels(d: dict) -> dict:
    """Decode org/dept codes to display labels via the directory (render-time)."""
    directory = org_directory.get_directory()
    org = directory.org_display(d.get("org_code"))
    dept = directory.dept_display(d.get("org_code"), d.get("dept_code"), d.get("department"))
    return {
        "org_label": {"text": org.text, "known": org.known},
        "dept_label": {"text": dept.text, "known": dept.known},
    }


def _is_recent(iso: Optional[str], *, days: int) -> bool:
    """True if *iso* falls within the last *days* (first_seen -> 'new' chip)."""
    age = days_until(iso)  # negative = in the past
    return age is not None and -days <= age <= 0


def _enrich_fleet(devices: list) -> list:
    """Decorate each device with decoded identity labels + version/new flags.

    'Outdated' is data-driven: the highest agent_version present in the fleet is
    treated as current, so a half-finished rollout is visible without the server
    knowing the 'latest' version out of band.
    """
    parsed = [parse_version(d.get("agent_version")) for d in devices]
    newest = max([v for v in parsed if v is not None], default=None)
    enriched = []
    for d, ver in zip(devices, parsed):
        enriched.append(
            {
                **d,
                **_identity_labels(d),
                "version_outdated": bool(newest is not None and ver is not None and ver < newest),
                "is_new": _is_recent(d.get("first_seen"), days=7),
            }
        )
    return enriched


def _fleet_context(devices: list) -> dict:
    # Build enriched copies (do not mutate db-owned dicts) -- immutable pattern.
    decorated = _enrich_fleet(devices)
    enriched = [{**d, "flags": _device_flags(d)} for d in decorated]
    return {"summary": _fleet_summary(enriched), "groups": _group_by_site(enriched)}


def _attach_printers_to_netmap(m: dict, printers: list) -> dict:
    """Place discovered printers into their subnet cluster (by IP /24) so the map
    shows them as nodes. A printer that duplicates an ARP 'other' node replaces it
    (no double node); printers whose subnet has no cluster go to a loose list.
    Pure over already-read inputs (mutates the fresh build_netmap result)."""
    by_subnet: dict[str, list] = {}
    for p in printers:
        sub = subnet_hint(p.get("ip"))
        if not sub:
            continue
        by_subnet.setdefault(sub, []).append(
            {
                "ip": p.get("ip"),
                "printer_id": p.get("printer_id"),
                "label": p.get("model") or p.get("hostname") or p.get("ip"),
                "vendor": p.get("vendor"),
                "status": p.get("status"),
                "online": p.get("online"),
                "total_pages": p.get("total_pages"),
            }
        )
    placed: set = set()
    for c in m.get("clusters", []):
        cps = by_subnet.get(c.get("subnet_hint"), [])
        if cps:
            ips = {n["ip"] for n in cps}
            c["others"] = [o for o in c.get("others", []) if o.get("ip") not in ips]
            placed.update(ips)
        c["printers"] = cps
    m["printers_unclustered"] = [
        n for lst in by_subnet.values() for n in lst if n["ip"] not in placed
    ]
    return m


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def fleet(request: Request):
    return _TEMPLATES.TemplateResponse(request, "fleet.html", _fleet_context(db.get_devices()))


@router.get("/fleet/fragment", response_class=HTMLResponse)
def fleet_fragment(request: Request):
    """KPI + table partial, polled by the dashboard for near-real-time updates."""
    return _TEMPLATES.TemplateResponse(
        request, "_fleet_body.html", _fleet_context(db.get_devices())
    )


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_health(request: Request):
    """§6 pipeline health page — ingest rate, source health, DB sizes."""
    return _TEMPLATES.TemplateResponse(request, "pipeline.html", {"m": db.get_pipeline_metrics()})


@router.get("/device/{device_id}", response_class=HTMLResponse)
def device(request: Request, device_id: str):
    d = db.get_device(device_id)
    if d is None:
        raise HTTPException(status_code=404, detail="device not found")
    age = db.age_seconds(d.get("last_seen"))
    d = {
        **d,
        "last_seen_age_sec": age,
        "stale": age is not None and age > db.STALE_AFTER_SEC,
        **_identity_labels(d),
    }
    # Phase 2 (D8): if the whole subnet degrades, tell the operator it is the
    # infrastructure, not this PC. Read-side fleet query — page views only.
    net_note = subnet_context_for(db.get_network_snapshots(), device_id)
    # Ф6: the ONE canonical card. If this agent has a topology twin (FK from Ф1),
    # embed its network section here instead of a separate /netdisco/device card.
    nd = db.get_linked_net_device(device_id=device_id)
    return _TEMPLATES.TemplateResponse(
        request, "device.html", {"d": d, "net_subnet_note": net_note, "nd": nd}
    )


@router.get("/netmap", response_class=HTMLResponse)
def network_map(request: Request, at: Optional[str] = None):
    """Network map page (SSR + the unified canvas engine). Ф4: ``/netmap`` now serves
    the ONE unified graph (Ф2 assembler via the Ф3 GraphCache) -- real L2/L3 links +
    agent-uplink + ICMP quality + subnet/anomaly overlays -- through ``_netgraph``.
    The old ephemeral cluster model (``build_netmap``) is retired here; the unified
    superset is the single contract for the canvas and the API. Inventory rows link to
    the canonical card (``card_url``, agent>printer>net-infra). All agent/SNMP strings
    reach the DOM only via ``| tojson`` (autoescape on) / ``textContent``.

    Ф5: ``?at=<snapshot_id>`` renders a HISTORICAL frame instead -- read straight from
    the snapshot store (never the live cache), with a plaque identifying it as a past
    frame. The slider list (``snapshots``) is always passed so the time-machine panel
    works on the live page too."""
    history = None
    if at is not None and at != "":
        try:
            sid = int(at)
        except (TypeError, ValueError):
            sid = -1
        snap = db.get_topology_snapshot(sid)
        if snap is not None:
            # ONE source of truth: the same normaliser the API uses, so the canvas
            # ``history_at`` marker (=> the time-machine plaque) renders on the SSR
            # route too -- the two paths can never drift.
            graph = historical_graph_from_snapshot(snap)
            history = {
                "at": snap.get("id"),
                "received_at": snap.get("received_at"),
                "label": "исторический кадр",
            }
    if history is None:
        graph = _unified_map_graph(request)
    return _TEMPLATES.TemplateResponse(
        request,
        "netmap.html",
        {
            "graph": graph,
            "changes": db.get_net_changes(days=7),
            "snapshots": db.list_topology_snapshots(limit=200),
            "history": history,
        },
    )


@router.get("/topology")
def topology() -> RedirectResponse:
    """Ф10: «Топология» is demolished -- the unified «Карта сети» (/netmap) is the
    single entry point and a strict superset (SSR inventory + the L2/L3 graph + the
    control panel + time machine). A permanent redirect keeps old bookmarks working."""
    return RedirectResponse("/netmap", status_code=301)


@router.get("/netdisco/device/{device_nid}", response_class=HTMLResponse)
def net_device(request: Request, device_nid: str):
    """One network device: interfaces, incident links, reachability status and its
    slice of the change journal. Distinct from /device/{id} (SRP agents)."""
    d = db.get_net_device(device_nid)
    if d is None:
        raise HTTPException(status_code=404, detail="network device not found")
    # Ф6: never two cards for one physical device. A node FK-linked (Ф1) to an
    # agent / printer redirects to that canonical card (which embeds the topology
    # section). Redirect is one-way (net -> canonical), so no loop. Standalone
    # infrastructure (no twin) keeps its own net card below.
    if d.get("device_id"):
        return RedirectResponse(f"/device/{d['device_id']}", status_code=302)
    if d.get("printer_id"):
        return RedirectResponse(f"/printers/{d['printer_id']}", status_code=302)
    changes = [c for c in db.get_net_changes(days=90) if c.get("device_nid") == device_nid]
    return _TEMPLATES.TemplateResponse(request, "net_device.html", {"d": d, "changes": changes})


@router.get("/print", response_class=HTMLResponse)
def print_analytics(
    request: Request,
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    device: str = "",
    printer: str = "",
    ip: str = "",
):
    """Print analytics page (printview rework). Renders the shell + filter panel;
    all data is pulled client-side from the /fleet/print/* endpoints. Filter state
    lives in the URL so it survives reload (values are pre-filled here, escaped by
    autoescape; JS reads the same query string)."""
    days = max(days, 0)
    return _TEMPLATES.TemplateResponse(
        request,
        "print.html",
        {
            "days": days,
            "f_date_from": date_from,
            "f_date_to": date_to,
            "f_device": device,
            "f_printer": printer,
            "f_ip": ip,
        },
    )


@router.get("/printers", response_class=HTMLResponse)
def printers(request: Request, days: int = 30):
    """Network-printer dashboard: hardware counters/supplies/errors + IP + dates +
    reconcile with print_jobs (which PCs printed). SSR (autoescape) + a Plotly bar."""
    days = min(max(days, 0), 365)
    ov = db.get_printers_overview(days=days)
    return _TEMPLATES.TemplateResponse(
        request,
        "printers.html",
        {
            "days": days,
            "ov": ov,
            "kpis": _printer_kpis(ov["printers"]),
            "pages_series": db.get_printers_pages_series(days=days),
        },
    )


@router.get("/printers/{printer_id}", response_class=HTMLResponse)
def printer_card(request: Request, printer_id: str, days: int = 30):
    """One printer: counter history + supplies/trays/errors + source PCs."""
    days = min(max(days, 0), 365)
    d = db.get_printer_detail(printer_id, days=days)
    if d is None:
        raise HTTPException(status_code=404, detail="printer not found")
    # Ф6: embed the topology section of this printer's network twin (FK from Ф1).
    nd = db.get_linked_net_device(printer_id=printer_id)
    return _TEMPLATES.TemplateResponse(
        request, "printer_detail.html", {"d": d, "days": days, "nd": nd}
    )


@router.get("/deploy", response_class=HTMLResponse)
def deploy(request: Request):
    """Deploy-command generator (tray spec §7).

    Pick an org/dept from the directory and get a ready ``setup.exe`` command with
    the right codes + server URL; the password/token stay ``<ПАРОЛЬ>``/``<ТОКЕН>``
    placeholders (the open, auth-less dashboard never holds real secrets).
    Read-only: reflects the directory + the request host, writes nothing.
    """
    orgs = org_directory.get_directory().as_picker()
    default_server = str(request.base_url).rstrip("/")
    return _TEMPLATES.TemplateResponse(
        request, "deploy.html", {"orgs": orgs, "default_server": default_server}
    )
