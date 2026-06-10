"""W4.2 disk-fill / servicing-collapse engine: a deterministic current-state verdict.

The 3rd of the independent domain engines (cctodo §4 W4.2). It judges system
free-space depletion and the Windows-servicing collapse it causes downstream:

  * **Free-space level is the leading signal**, but graded on the *median* of the
    recent heartbeat window, not the latest reading. This is the engine's defining
    job -- **distinguishing a cleanup rebound from true depletion**. A Windows
    Update stages several GB, free space dips, the update installs and cleanup
    reclaims it: a single low sample. The median ignores that one-off dip (rebound),
    yet moves when a drive sits persistently full (true depletion). The fresh dip
    is never hidden -- it is surfaced as a possible transient for the operator.
  * **Servicing failures are the downstream confirmation.** Repeated
    WindowsUpdateClient install/download failures with a low disk confirm the fill
    is breaking servicing (amplify); with a *healthy* disk they still flag a real
    "machine not patching" risk at lower weight, because the cause is uncertain
    (a partition we do not measure, or update corruption) -- we never claim a
    disk-fill cause we cannot see.

Output is the ``disk_fill_risk`` axis in the W0.5 Score100 envelope (higher = worse)
with the same gating: untrusted identity withholds; no free-space telemetry *and* no
servicing signal -> UNKNOWN (never a confident zero). Because free space is a direct
measurement, a healthy system drive *is* a confident all-clear (unlike the battery
engine's swelling blind spot). Pure arithmetic over the recent window (D4, no ML);
the depletion *slope/ETA* lives in the W4.1 trajectory engine, not here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Optional

from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

# Free-space bands (system drive, % free) -> current-state risk. A nearly-full
# system drive breaks updates, the pagefile, temp files, hibernation and logging;
# Windows wants ~20% headroom, so the bands warn well before zero.
_FREE_CRITICAL = 3.0  # -> 75: pagefile / updates / temp all at risk
_FREE_SEVERE = 8.0  # -> 55
_FREE_HIGH = 15.0  # -> 30
_FREE_WATCH = 20.0  # -> 18

# Repeated Windows Update install/download failures = a servicing collapse, not a
# routine one-off failure. Below this we treat update failures as noise.
_SERVICING_MIN_FAILURES = 3
# WindowsUpdateClient failure event ids (install / install / download failure).
_WU_FAILURE_IDS = frozenset({20, 25, 31})
_WU_PROVIDER = "windowsupdateclient"  # matched case-insensitively as a substring

# A median over fewer than this many samples cannot yet rule out a transient -> the
# verdict stands but at reduced confidence.
_MIN_HIGH_CONF_SAMPLES = 3

# Current-state recency: ignore free-space readings older than this (relative to the
# newest sample). A drive that was full months ago but is clean now must not be
# dragged into a false depletion alarm by stale lows -- the depletion *history* is
# the trajectory engine's job, not this current-state verdict.
_RECENCY_DAYS = 14.0

_UNTRUSTED_REASON = "идентификатор устройства не подтверждён (контракт §7)"
_SYSTEM_DRIVE_BLIND_SPOT = (
    "наблюдается только свободное место на системном диске (другие тома не измеряются)"
)
_SERVICING_BLIND_SPOT = (
    "состояние обслуживания Windows определяется только по событиям сбоев WindowsUpdateClient"
)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _num(d: Optional[dict], key: str) -> Optional[float]:
    if not d:
        return None
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _free_space_samples(series: list[dict[str, Any]]) -> list[float]:
    """Recent free-space % readings, newest-first (None / non-numeric dropped).

    When the rows carry server-receipt timestamps (the production path), the window
    is bounded to the last ``_RECENCY_DAYS`` relative to the newest sample so a
    current-state median cannot be distorted by months-old readings. Rows without a
    parseable timestamp (e.g. direct unit-test dicts) keep the caller's order, which
    ``db.get_recent_heartbeats`` guarantees is newest-first.
    """
    rows: list[tuple[Optional[datetime], float]] = []
    for row in series or []:
        v = _num(row, "free_space_pct")
        if v is None:
            continue
        rows.append((_parse_iso(row.get("received_at") or row.get("ts")), v))

    dated = [(w, v) for (w, v) in rows if w is not None]
    if dated:
        newest = max(w for (w, _) in dated)
        cutoff = newest - timedelta(days=_RECENCY_DAYS)
        return [v for (w, v) in sorted(dated, key=lambda p: p[0], reverse=True) if w >= cutoff]
    return [v for (_, v) in rows]


def _servicing_failures(events: list[dict[str, Any]]) -> int:
    """Count WindowsUpdateClient install/download failures.

    Matched by provider *and* event id -- a bare id 20 from another provider (e.g.
    "disk") is not a Windows Update failure, so identity is by source, not number.
    """
    count = 0
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        src = ev.get("source")
        eid = ev.get("event_id")
        if not isinstance(src, str) or _WU_PROVIDER not in src.lower():
            continue
        try:
            eid_int = int(eid) if eid is not None else None
        except (TypeError, ValueError):
            eid_int = None
        if eid_int in _WU_FAILURE_IDS:
            count += 1
    return count


def _free_space_risk(typical: float) -> tuple[float, Optional[Factor]]:
    """Current-state risk 0..100 from the persistent (median) free-space level."""
    if typical < _FREE_CRITICAL:
        return 75.0, {
            "label": f"системный диск: {typical:.0f}% свободно — критически заполнен",
            "delta": 75.0,
        }
    if typical < _FREE_SEVERE:
        return 55.0, {
            "label": f"системный диск: {typical:.0f}% свободно — критический уровень",
            "delta": 55.0,
        }
    if typical < _FREE_HIGH:
        return 30.0, {"label": f"системный диск: {typical:.0f}% свободно — мало", "delta": 30.0}
    if typical < _FREE_WATCH:
        return 18.0, {
            "label": f"системный диск: {typical:.0f}% свободно — заполняется",
            "delta": 18.0,
        }
    return 0.0, None


def compute_disk_fill_risk(
    heartbeat_series: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    device_trust: str = "ok",
) -> Score100:
    """Deterministic disk-fill / servicing-collapse risk for one device.

    Higher = a fuller system drive and/or a collapsing Windows-servicing pipeline.
    Grades on the *median* recent free-space level (a cleanup rebound cannot move
    the median); servicing failures confirm/amplify. Gating mirrors W0.5/W4.1:
    untrusted withholds; no free-space data *and* no servicing collapse -> UNKNOWN.
    """
    direction: Direction = "higher_is_worse"

    if device_trust == "untrusted":
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["идентификация не подтверждена"],
            source_lineage={"identity": "untrusted"},
            reason=_UNTRUSTED_REASON,
        )

    samples = _free_space_samples(heartbeat_series)
    servicing_count = _servicing_failures(events)
    collapse = servicing_count >= _SERVICING_MIN_FAILURES

    if not samples and not collapse:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["нет телеметрии свободного места на системном диске"],
            reason="нет телеметрии свободного места (UNKNOWN — ложная уверенность недопустима)",
        )

    current: Optional[float] = samples[0] if samples else None
    typical: Optional[float] = median(samples) if samples else None

    factors: list[Factor] = []
    notes: list[str] = []
    value = 0.0
    disk_low = False
    if typical is not None:
        disk_value, disk_factor = _free_space_risk(typical)
        value += disk_value
        disk_low = disk_value > 0.0
        if disk_factor is not None:
            factors.append(disk_factor)

    if collapse:
        if disk_low:
            delta = 20.0
            label = f"Windows Update: сбои ({servicing_count}) — мало места мешает обслуживанию"
        else:
            delta = 28.0
            label = f"Windows Update: многократные сбои ({servicing_count}) — машина не обновляется"
        value += delta
        factors.append({"label": label, "delta": delta})

    # The fresh dip is never hidden: latest below the watch line while the persistent
    # level is healthy = a probable Windows-servicing cleanup in progress, not depletion.
    if current is not None and typical is not None and current < _FREE_WATCH <= typical:
        notes.append(
            f"последнее значение: {current:.0f}% свободно, нестабильно "
            "(возможная очистка/временное — см. тренд)"
        )

    confidence = _confidence(len(samples), disk_low, collapse)

    missing = [_SYSTEM_DRIVE_BLIND_SPOT, _SERVICING_BLIND_SPOT]
    if not samples:
        missing.append("свободное место на системном диске не наблюдалось")
    missing.extend(notes)

    return make_score100(
        _clamp(value),
        direction,
        band_for_risk_score(_clamp(value)),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage={
            "free_space_current": current,
            "free_space_typical": round(typical, 1) if typical is not None else None,
            "n_samples": len(samples),
            "servicing_failures": servicing_count,
            "disk_low": disk_low,
        },
        reason=_verdict_reason(value, samples, collapse),
    )


def _confidence(n_samples: int, disk_low: bool, collapse: bool) -> ScoreConfidence:
    """Free space is directly measured (high) once we have enough samples to rule out
    a transient; a servicing-only / disk-OK collapse is cause-uncertain (medium)."""
    if n_samples == 0:
        return "medium"  # servicing-only signal, no free-space measurement
    if collapse and not disk_low:
        return "medium"  # disk fine, updates failing for an uncertain reason
    return "high" if n_samples >= _MIN_HIGH_CONF_SAMPLES else "medium"


def _verdict_reason(value: float, samples: list[float], collapse: bool) -> str:
    if value > 0.0:
        return ""
    if samples and not collapse:
        return "системный диск: достаточно свободного места; сбоев Windows Update не обнаружено"
    return ""
