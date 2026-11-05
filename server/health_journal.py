"""Health journal (spec 2026-10-30 H18): verdict level changes + outcome labels.

Why: without labelled outcomes the verdict can never be calibrated -- this starts the
label set now, on today's logic. Transitions are server-written only; an operator adds
an outcome through the ack endpoint (admin token + rate limit, see server/api.py).
Storage, caps and the 152-FZ note clearing on delete live in server/db.py.

Machine values (outcomes, states) are English; the dashboard shows Russian labels.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Optional

from server import db

STATES = ("h0", "h1", "h2", "h3", "h4")

# What the operator found when checking an alert. preventive_replacement = the disk was
# replaced BECAUSE of the alert and the failure was never confirmed: such a label must
# not count as a confirmed failure.
OperatorOutcome = Literal[
    "confirmed_failure",
    "preventive_replacement",
    "false_alarm",
    "resolved_software",
    "unverified",
]
OPERATOR_OUTCOMES: tuple[str, ...] = (
    "confirmed_failure",
    "preventive_replacement",
    "false_alarm",
    "resolved_software",
    "unverified",
)


def _level(health: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The last known h-level of a verdict blob (an unknown cycle keeps the memory)."""
    if not isinstance(health, Mapping):
        return None
    level = health.get("last_known_state") or health.get("state")
    return level if level in STATES else None


def record_transition(
    device_id: str,
    prev_health: Optional[Mapping[str, Any]],
    health: Mapping[str, Any],
    ts: str,
) -> bool:
    """Append a journal row when the verdict's h-level changed; True if written.

    Entering ``unknown`` is not a level change (the morning blind minute would flood
    the journal); a level reached through an unknown cycle is marked ``via_unknown``.
    """
    new = _level(health)
    old = _level(prev_health)
    if new is None or new == old:
        return False
    db.append_health_transition(
        device_id,
        ts,
        old,
        new,
        raw_state=health.get("raw_state"),
        dominant=health.get("dominant"),
        rung_source=health.get("rung_source"),
        detail={
            "state": health.get("state"),
            "via_unknown": isinstance(prev_health, Mapping)
            and prev_health.get("state") == "unknown",
        },
    )
    return True


def record_operator_outcome(device_id: str, outcome: str, note: str, ts: str) -> bool:
    """The operator's verdict on an alert, stamped with the verdict it judges.

    False when the device was deleted meanwhile (the caller answers 404)."""
    return db.add_health_outcome(
        device_id, ts, "operator", outcome, note or None, require_device=True
    )


def record_disk_replacement(device_id: str, health: Mapping[str, Any], ts: str) -> bool:
    """Auto label ``disk_replaced`` on the swap cycle (the verdict's disk_replaced, H1):
    a replacement at h0 is the miss the operator labels never show."""
    if health.get("disk_replaced") is not True:
        return False
    db.add_health_outcome(device_id, ts, "auto", "disk_replaced")
    return True


def mark_os_reinstalls(superseded: Mapping[str, str], ts: str) -> int:
    """Auto label ``os_reinstalled`` for records the fleet canon superseded; idempotent."""
    done = db.devices_with_outcome("os_reinstalled")
    written = 0
    for old in superseded:
        if old in done:
            continue
        db.add_health_outcome(old, ts, "auto", "os_reinstalled")
        written += 1
    return written
