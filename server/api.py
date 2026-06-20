"""REST API: ingest telemetry + query device state and scores."""

from __future__ import annotations

import csv
import hmac
import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from shared.schema import Envelope, utcnow_iso

from server import db, org_directory
from server.analytics.diagnostics import compute_diagnostics
from server.analytics.netmap import build_netmap
from server.ingest_guards import check_idempotency, check_rate_limit
from server.netdisco import scheduler as netdisco_scheduler
from server.pipeline import ingest_envelope
from server.printers import scheduler

router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/ingest")
def ingest(env: Envelope, request: Request) -> dict:
    expected = getattr(request.app.state, "ingest_token", "")
    provided = request.headers.get("x-srp-token") or ""
    if expected and not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing ingest token")
    if not check_rate_limit(env.device_id):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not check_idempotency(env.idempotency_key):
        return {"device_id": env.device_id, "msg_type": env.msg_type, "duplicate": True}
    try:
        return ingest_envelope(env)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/devices")
def list_devices() -> list[dict]:
    return db.get_devices()


@router.get("/devices/{device_id}")
def get_device(device_id: str) -> dict:
    device = db.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return device


@router.get("/diagnostics/{device_id}")
def diagnostics(device_id: str) -> dict:
    """W4.1 trajectory: deterministic slopes + ETA + the trajectory_risk axis."""
    result = compute_diagnostics(device_id)
    if result is None:
        raise HTTPException(status_code=404, detail="device not found")
    return result


class AckBody(BaseModel):
    note: str = Field(default="", max_length=1000)


@router.post("/devices/{device_id}/ack")
def ack_device(device_id: str, body: AckBody) -> dict:
    """Operator feedback: acknowledge a device + attach a note."""
    if db.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    db.set_ack(device_id, body.note, utcnow_iso())
    return {"status": "ok"}


@router.get("/metrics")
def metrics() -> dict:
    """Pipeline health snapshot: fleet counts, ingest rate, source health, DB sizes."""
    return db.get_pipeline_metrics()


@router.get("/netmap")
def netmap() -> dict:
    """Phase-2 network map: gateway clusters, agentless neighbors, subnet anomalies."""
    return build_netmap(db.get_network_snapshots())


@router.get("/netdisco/devices")
def netdisco_devices(dev_type: Optional[str] = None, site: Optional[str] = None) -> dict:
    """Persistent network-device inventory (netdisco phase 3), optionally filtered
    by device type (router/switch/ap/agent/endpoint/unknown) and site."""
    return {"devices": db.get_net_devices(dev_type=dev_type, site=site)}


@router.post("/discovery/poll")
def poll_discovery() -> dict:
    """Force one netdisco inventory cycle now (dashboard button). Bounded by the
    scheduler's anti-DoS lock -- a concurrent call returns busy, not a second pass."""
    return netdisco_scheduler.poll_now()


# ---------------------------------------------------------------------------
# Print tracking endpoints
# ---------------------------------------------------------------------------

_DAYS_MIN = 0
_DAYS_MAX = 365


def _clamp_days(days: int) -> int:
    return min(max(days, _DAYS_MIN), _DAYS_MAX)


@router.get("/devices/{device_id}/print")
def device_print(device_id: str, days: int = 30) -> dict:
    if db.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    return db.get_device_print(device_id, days=_clamp_days(days))


def _decode_departments(raw: list[dict]) -> list[dict]:
    """Decode raw (org_code, dept_code, department) buckets to display labels
    and merge buckets that render to the same label (tray spec §7).

    Each device maps to exactly one raw bucket, so summing ``devices_count``
    across merged buckets stays correct. ``known=False`` carries the
    "not in directory" chip through to the chart.
    """
    directory = org_directory.get_directory()
    merged: dict[tuple, dict] = {}
    for row in raw:
        label = directory.dept_display(
            row.get("org_code"), row.get("dept_code"), row.get("department")
        )
        key = (label.text, label.known)
        bucket = merged.setdefault(
            key,
            {"dept": label.text, "known": label.known, "pages": 0, "jobs": 0, "devices_count": 0},
        )
        bucket["pages"] += int(row.get("pages") or 0)
        bucket["jobs"] += int(row.get("jobs") or 0)
        bucket["devices_count"] += int(row.get("devices_count") or 0)
    return sorted(merged.values(), key=lambda b: b["pages"], reverse=True)


@router.get("/fleet/print/analytics")
def fleet_print_analytics(days: int = 30) -> dict:
    data = db.get_print_analytics(days=_clamp_days(days))
    data["departments"] = _decode_departments(data["departments"])
    return data


@router.get("/fleet/print/export.csv")
def fleet_print_export(days: int = 30) -> StreamingResponse:
    rows = db.export_print_rows(days=_clamp_days(days))
    directory = org_directory.get_directory()
    for row in rows:
        # Decode names render-time; the legacy free-text `department` column is
        # kept for the transition (tray spec §7).
        row["org_name"] = directory.org_display(row.get("org_code")).text
        row["dept_name"] = directory.dept_display(
            row.get("org_code"), row.get("dept_code"), row.get("department")
        ).text
    buf = io.StringIO()
    fieldnames = [
        "ts",
        "device_id",
        "hostname",
        "org_name",
        "dept_name",
        "printer",
        "pages",
        "size_bytes",
        "user_name",
        "department",
        "source",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=print_export_{days}d.csv"},
    )


@router.get("/fleet/print")
def fleet_print(days: int = 30, today: bool = False) -> dict:
    if today:
        return db.get_fleet_print(today=True)
    return db.get_fleet_print(days=_clamp_days(days))


class MetaPatch(BaseModel):
    # `department` is DEPRECATED (superseded by dept_code + org_directory, tray
    # spec §7) but still accepted for the transition. `comment` is the device's
    # free-text label going forward.
    department: Optional[str] = Field(default=None, max_length=200)
    comment: Optional[str] = Field(default=None, max_length=200)


@router.patch("/devices/{device_id}/meta")
def patch_device_meta(device_id: str, body: MetaPatch) -> dict:
    if db.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    if body.department is not None:
        db.set_device_department(device_id, body.department)  # deprecated path
    if body.comment is not None:
        db.set_device_comment(device_id, body.comment)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Device cleanup (ghost hygiene, 2026-06-16)
# ---------------------------------------------------------------------------
class PurgeBody(BaseModel):
    # Delete devices silent for at least this many days (server-stamped last_seen).
    # Minimum 1 so a stray days=0 can never wipe the whole fleet.
    days: int = Field(default=30, ge=1, le=3650)
    dry_run: bool = False


@router.post("/devices/{device_id}/delete")
def delete_device(device_id: str) -> dict:
    """Remove a device and ALL its data. POST-only so a stray GET never deletes."""
    if not db.delete_device(device_id):
        raise HTTPException(status_code=404, detail="device not found")
    return {"status": "ok", "deleted": True}


@router.post("/devices/purge")
def purge_devices(body: PurgeBody) -> dict:
    """Bulk-clear ghosts: delete (or, with dry_run, preview) devices silent past *days*."""
    return db.purge_devices_silent_for(body.days, dry_run=body.dry_run)


# ---------------------------------------------------------------------------
# Network printers (phase 6)
# ---------------------------------------------------------------------------
@router.get("/printers")
def list_printers(days: int = 30) -> dict:
    """Hardware printer inventory + per-printer software print reconcile."""
    return db.get_printers_overview(days=_clamp_days(days))


@router.get("/printers/{printer_id}")
def printer_detail(printer_id: str, days: int = 30) -> dict:
    """One printer: inventory + counter series + which PCs printed to it."""
    p = db.get_printer_detail(printer_id, days=_clamp_days(days))
    if p is None:
        raise HTTPException(status_code=404, detail="printer not found")
    return p


@router.post("/printers/poll")
def poll_printers(request: Request) -> dict:
    """Force one printer poll cycle now (dashboard button). Bounded by SNMP/HTTP
    timeouts; probes only already-discovered hosts, never scans ranges here."""
    printer_cfg = getattr(request.app.state, "printer_config", None)
    if printer_cfg is None:
        from server.printers.config import load_printer_config

        printer_cfg = load_printer_config(None)
    return scheduler.poll_now(printer_cfg)
