"""W4.1 read-side: expose the trajectory diagnostics the pipeline already computed.

The trend engine runs once, inside ``recompute_scores`` (single source of truth),
and its output is persisted in the score blob. This read side simply surfaces that
stored result so the API/dashboard never recompute (and never diverge from) it.
"""

from __future__ import annotations

from typing import Any, Optional

from server import db


def compute_diagnostics(device_id: str) -> Optional[dict[str, Any]]:
    """Stored trajectory diagnostics for one device, or None if it is unknown."""
    device = db.get_device(device_id)
    if device is None:
        return None
    risk = ((device.get("scores") or {}).get("risk")) or {}
    score100 = risk.get("score100") or {}
    return {
        "device_id": device_id,
        "trajectory_risk": score100.get("trajectory_risk"),
        "storage_risk": score100.get("storage_risk"),
        "battery_risk": score100.get("battery_risk"),
        "trends": risk.get("trajectory") or {},
    }
