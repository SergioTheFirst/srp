"""ssd3 Ф8: deterministic outcome-counting for fleet rule self-reinforcement, not ML."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

RULE_KEYS = ("pending_high", "media_recurrence", "early_chain")

# code-flag each rule_key reinforces (Task 2 wires the actual multiplication;
# this module only needs the names for documentation/validation, not the mapping
# itself -- do not hardcode flag strings here, that's Task 2's concern).

# Russian labels for the /pipeline dashboard and storage.py's lineage factors --
# single source of truth so the same rule has the same name everywhere. Echo the
# EXISTING factor labels in storage.py as closely as possible (same concept,
# same words) rather than inventing new phrasing.
RULE_LABELS: dict[str, str] = {
    "pending_high": "повышенное число pending-секторов",
    "media_recurrence": "рецидив дефектов диска",
    "early_chain": "ранние сигналы без повреждений",
}

# Public (no leading underscore): db.py's run_rulestats_scan derives its history
# lookback from REFUTE_WINDOW (the longer of the two) so the two files can't drift
# apart on "how many days of history does a full resolution need".
CONFIRM_WINDOW = timedelta(days=45)
REFUTE_WINDOW = timedelta(days=60)

_BOOST_MIN_CONFIRMED = 5
_CEILING_MIN_CONFIRMED = 15
_BOOST_MIN_RATIO = 0.7
_MUTE_MIN_REFUTED = 10
_MUTE_MAX_RATIO = 0.3
_BOOST_MULT = 1.2
_CEILING_MULT = 1.5
_MUTE_MULT = 0.8
_NEUTRAL_MULT = 1.0


# --------------------------------------------------------------------------- #
# Row helpers -- every .get chained with a default; pre-Ф2 rows (no "coords"
# key at all) and any None at any level must read as "flag absent", never raise.
# --------------------------------------------------------------------------- #
def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO ts -> aware datetime; naive strings assumed UTC; unparseable -> None."""
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row_flags(row: dict[str, Any]) -> list:
    risk = row.get("risk") or {}
    score100 = risk.get("score100") or {}
    storage = score100.get("storage_risk") or {}
    coords = storage.get("coords") or {}
    flags = coords.get("flags")
    return flags if isinstance(flags, list) else []


def _confirms(row: dict[str, Any]) -> bool:
    """errchain.stage>=2 OR storage_risk.band=='bad' -- the Ф8 confirm condition."""
    risk = row.get("risk") or {}
    stage = (risk.get("errchain") or {}).get("stage", 0)
    storage = (risk.get("score100") or {}).get("storage_risk") or {}
    stage_ok = isinstance(stage, (int, float)) and stage >= 2
    return stage_ok or storage.get("band") == "bad"


def _has_row_in_window(
    rows: list[dict[str, Any]],
    end_ts: datetime,
    window: timedelta,
    predicate: Callable[[dict[str, Any]], bool],
) -> bool:
    """Any row with end_ts < ts <= end_ts+window satisfying predicate(row)."""
    upper = end_ts + window
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is not None and end_ts < ts <= upper and predicate(row):
            return True
    return False


def _resolve_episode(
    rows: list[dict[str, Any]], end_ts: datetime, flag: str, now: datetime
) -> Optional[str]:
    """confirmed / refuted / None (not yet resolvable) for one finished episode."""
    if _has_row_in_window(rows, end_ts, CONFIRM_WINDOW, _confirms):
        return "confirmed"
    if now - end_ts >= REFUTE_WINDOW:
        blocked = _has_row_in_window(
            rows, end_ts, REFUTE_WINDOW, lambda r: _confirms(r) or flag in _row_flags(r)
        )
        if not blocked:
            return "refuted"
    return None


def _scan_rule(
    rows: list[dict[str, Any]],
    rule_key: str,
    flag: str,
    now: datetime,
) -> list[tuple[str, str, str]]:
    outcomes: list[tuple[str, str, str]] = []
    in_run = False
    run_end: Optional[datetime] = None
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        present = ts is not None and flag in _row_flags(row)
        if present:
            in_run = True
            run_end = ts
            continue
        if in_run and run_end is not None:
            outcome = _resolve_episode(rows, run_end, flag, now)
            if outcome is not None:
                outcomes.append((rule_key, outcome, run_end.isoformat()))
            in_run = False
            run_end = None
    # A run still flag-present at the last row is a data-horizon artifact, not
    # evidence the condition stopped -- leave it alone, unresolved (Замечание №3).
    return outcomes


def scan_device(score_rows: list[dict[str, Any]], *, now: datetime) -> list[tuple[str, str, str]]:
    """Resolve confirmed/refuted rule episodes from one device's OLD->NEW score history.

    Pure and stateless: the caller reverses db.get_score_series (newest-first)
    before calling; this function has NO memory of prior calls and does NOT
    dedup against episodes already recorded on an earlier sweep -- "have we
    already counted this" is a persistence question, answered by the caller's
    storage layer (server.db's rule_episodes table), not by a pure re-evaluation
    of whatever row window it's handed. Returns (rule_key, outcome, end_ts_iso)
    triples -- end_ts_iso (`end_ts.isoformat()`) is the episode's own dedup key
    component the caller needs.
    """
    flag_for_rule = {
        "pending_high": "pending_gt10",
        "media_recurrence": "recurrence",
        "early_chain": "early_events",
    }
    outcomes: list[tuple[str, str, str]] = []
    for rule_key in RULE_KEYS:
        outcomes.extend(_scan_rule(score_rows, rule_key, flag_for_rule[rule_key], now))
    return outcomes


def reinforcement(rule_key: str, stats: dict[str, int]) -> float:
    """Bounded [0.8..1.5] multiplier from one rule's fleet-wide confirm/refute counts.

    rule_key is accepted for signature symmetry with the plan and for a future
    per-rule-different-threshold need; thresholds are currently identical for all
    three rules, so it is never branched on inside this function.
    """
    confirmed = stats.get("confirmed", 0)
    refuted = stats.get("refuted", 0)
    total = confirmed + refuted
    ratio = confirmed / total if total > 0 else 0.0
    if confirmed >= _CEILING_MIN_CONFIRMED and ratio >= _BOOST_MIN_RATIO:
        return _CEILING_MULT
    if confirmed >= _BOOST_MIN_CONFIRMED and ratio >= _BOOST_MIN_RATIO:
        return _BOOST_MULT
    if refuted >= _MUTE_MIN_REFUTED and ratio < _MUTE_MAX_RATIO:
        return _MUTE_MULT
    return _NEUTRAL_MULT
