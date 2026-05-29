"""Ingest pipeline: store an envelope, then recompute scores for the device.

Keeping store+rescore together means an engineer sees fresh scores the moment
any message lands -- no batch job, no waiting. Scores are always recomputed
from the *latest* inventory + historical + heartbeat the server holds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from shared.schema import Envelope, parse_payload

from server import db
from server.scoring import compute_day1_scores, compute_risk


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ingest_envelope(env: Envelope) -> dict[str, Any]:
    # Validate payload shape against the typed model (raises ValueError on bad type).
    parse_payload(env.msg_type, env.payload)

    did, ts = env.device_id, env.ts
    if env.msg_type == "inventory":
        inv = env.payload
        db.upsert_device(
            did,
            ts,
            env.agent_version,
            hostname=inv.get("hostname"),
            manufacturer=inv.get("manufacturer"),
            model=inv.get("model"),
            chassis=inv.get("chassis"),
        )
        db.store_inventory(did, ts, inv)
    elif env.msg_type == "historical":
        db.touch_device(did, ts, env.agent_version)
        db.store_historical(did, ts, env.payload)
    elif env.msg_type == "heartbeat":
        db.touch_device(did, ts, env.agent_version)
        db.store_heartbeat(did, ts, env.payload)
    elif env.msg_type == "events":
        db.touch_device(did, ts, env.agent_version)
        db.store_events(did, env.payload.get("events", []))

    scores = recompute_scores(did)
    return {
        "device_id": did,
        "msg_type": env.msg_type,
        "scores_updated": scores is not None,
        "scores": scores,
    }


def recompute_scores(device_id: str) -> Optional[dict[str, Any]]:
    inv = db.get_inventory(device_id)
    hist = db.get_historical(device_id)
    hbs = db.get_recent_heartbeats(device_id, limit=1)
    hb = hbs[0] if hbs else None
    if inv is None and hist is None and hb is None:
        return None

    day1 = compute_day1_scores(inv, hist, hb)
    risk = compute_risk(inv, hist, hb)
    scores = {
        "performance": day1["performance"],
        "reliability": day1["reliability"],
        "wear": day1["wear"],
        "risk_exposure": day1["risk_exposure"],
        "risk": {
            "classes": risk["classes"],
            "top": risk["top"],
            "overall": risk["overall"],
            "day1_factors": day1["factors"],
        },
    }
    db.store_scores(device_id, _now_iso(), scores)
    return scores
