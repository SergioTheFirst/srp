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

from server import db
from server.analytics.diagnostics import compute_diagnostics
from server.ingest_guards import check_idempotency, check_rate_limit
from server.pipeline import ingest_envelope

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


@router.get("/fleet/print/analytics")
def fleet_print_analytics(days: int = 30) -> dict:
    return db.get_print_analytics(days=_clamp_days(days))


@router.get("/fleet/print/export.csv")
def fleet_print_export(days: int = 30) -> StreamingResponse:
    rows = db.export_print_rows(days=_clamp_days(days))
    buf = io.StringIO()
    fieldnames = [
        "ts",
        "device_id",
        "hostname",
        "printer",
        "pages",
        "size_bytes",
        "user_name",
        "department",
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
def fleet_print(days: int = 30) -> dict:
    return db.get_fleet_print(days=_clamp_days(days))


class MetaPatch(BaseModel):
    department: Optional[str] = Field(default=None, max_length=200)


@router.patch("/devices/{device_id}/meta")
def patch_device_meta(device_id: str, body: MetaPatch) -> dict:
    if db.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    if body.department is not None:
        db.set_device_department(device_id, body.department)
    return {"status": "ok"}
