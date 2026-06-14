"""Server-rendered dashboard: fleet overview + device detail.

Jinja2 autoescaping is on for .html, so any device-supplied string (hostname,
model, event message) is HTML-escaped -- no stored XSS from telemetry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from shared.schema import parse_version

from server import db, org_directory
from server.analytics.netmap import build_netmap, subnet_context_for

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


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
        return "только что"
    if sec < 90:
        return f"{sec}с"
    if sec < 5400:
        return f"{sec // 60}м"
    if sec < 172800:
        return f"{sec // 3600}ч"
    return f"{sec // 86400}д"


_TEMPLATES.env.globals.update(
    health_color=health_color,
    risk_color=risk_color,
    level_color=level_color,
    pct=pct,
    days_until=days_until,
    fmt_age=fmt_age,
)


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
    return _TEMPLATES.TemplateResponse(
        request, "device.html", {"d": d, "net_subnet_note": net_note}
    )


@router.get("/netmap", response_class=HTMLResponse)
def network_map(request: Request):
    """Phase-2 network map page (server-rendered, D1: no graph JS library)."""
    return _TEMPLATES.TemplateResponse(
        request, "netmap.html", {"m": build_netmap(db.get_network_snapshots())}
    )


@router.get("/print", response_class=HTMLResponse)
def print_analytics(request: Request, days: int = 30):
    """Print analytics page — fleet-wide charts (Plotly.js)."""
    days = max(days, 0)
    return _TEMPLATES.TemplateResponse(request, "print.html", {"days": days})


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
