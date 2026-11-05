"""Spec H13 (P0-6): OS stability -- the BSOD rung by recurrence, the cause apart from it.

A BugCheck day is a distinct UTC date of a 1001 (``ErrChain.bugcheck_days``; the 0.3.9
agent's ``hist.bugchecks`` later). >= 2 days in 30 -> h2; >= 3 days in 30 with >= 2 in
the last 7 -> h3; never h4 from BSODs alone (severity, not cause: a BSOD has many causes).
The cause (``cause_class``) only picks the action -- it never moves the state. Pure.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

_WINDOW = timedelta(days=30)
_RECENT = timedelta(days=7)
_FLEET_COHORT_MIN = 5
_FLEET_SHARE = 0.25
_FLEET_AFFECTED = 3
_PATCH_WINDOW = timedelta(days=3)

BSOD_ACTIONS = {
    "fleet_likely": "BSOD у многих машин этой модели — проверить обновления и драйверы на когорте",
    "software_likely": (
        "BSOD после недавнего обновления — проверить или откатить обновление и драйверы"
    ),
    "unknown": "причина не установлена — собрать дампы, проверить драйверы, обновления, память",
}


def _day(value: object) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def bsod_rung(days: list, now: datetime) -> tuple[Optional[str], int]:
    """(rung, distinct BugCheck days in the 30-day window) -- h2 / h3 / None."""
    today = now.date()
    seen = {
        d for d in map(_day, days or []) if d is not None and timedelta(0) <= today - d < _WINDOW
    }
    recent = sum(1 for d in seen if today - d < _RECENT)
    if len(seen) >= 3 and recent >= 2:
        return "h3", len(seen)
    return ("h2" if len(seen) >= 2 else None), len(seen)


def bsod_cause(days: list, cohort: Optional[dict], last_patch_date: Optional[str]) -> str:
    """fleet_likely: a cohort of >= 5 with >= 25 % and >= 3 machines with a BSOD;
    software_likely: a patch installed <= 3 days before the first BSOD day; else unknown
    (BugCheck codes arrive with the 0.3.9 agent -- plan, stage 6)."""
    c = cohort or {}
    size, share = c.get("cohort_size") or 0, float(c.get("cohort_bsod_pct") or 0.0)
    affected = int(c.get("cohort_bsod_n") or 0)  # counted: the share skips non-reporters
    if size >= _FLEET_COHORT_MIN and share >= _FLEET_SHARE and affected >= _FLEET_AFFECTED:
        return "fleet_likely"
    first, patch = min(filter(None, map(_day, days or [])), default=None), _day(last_patch_date)
    if first is not None and patch is not None and timedelta(0) <= first - patch <= _PATCH_WINDOW:
        return "software_likely"
    return "unknown"


def assess_stability(
    days: list,
    now: datetime,
    *,
    cohort: Optional[dict] = None,
    last_patch_date: Optional[str] = None,
) -> Optional[dict]:
    """The BSOD rung for compute_health(stability=...), or None below two BugCheck days."""
    rung, n = bsod_rung(days, now)
    if rung is None:
        return None
    return {"rung": rung, "days": n, "cause_class": bsod_cause(days, cohort, last_patch_date)}
