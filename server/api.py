"""REST API: ingest telemetry + query device state and scores."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from shared.schema import Envelope

from server import db
from server.pipeline import ingest_envelope

router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/ingest")
def ingest(env: Envelope) -> dict:
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
