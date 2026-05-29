"""Server-rendered dashboard: fleet overview + device detail.

Jinja2 autoescaping is on for .html, so any device-supplied string (hostname,
model, event message) is HTML-escaped -- no stored XSS from telemetry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from server import db

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


_TEMPLATES.env.globals.update(
    health_color=health_color,
    risk_color=risk_color,
    level_color=level_color,
    pct=pct,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def fleet(request: Request):
    devices = db.get_devices()
    summary = {
        "total": len(devices),
        "at_risk": sum(1 for d in devices if (d.get("risk_exposure") or 0) >= 50),
        "watch": sum(1 for d in devices if 25 <= (d.get("risk_exposure") or 0) < 50),
    }
    return _TEMPLATES.TemplateResponse(
        request, "fleet.html", {"devices": devices, "summary": summary}
    )


@router.get("/device/{device_id}", response_class=HTMLResponse)
def device(request: Request, device_id: str):
    d = db.get_device(device_id)
    if d is None:
        raise HTTPException(status_code=404, detail="device not found")
    return _TEMPLATES.TemplateResponse(request, "device.html", {"d": d})
