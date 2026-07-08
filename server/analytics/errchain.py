"""ssd3 Ф3: event-chain escalation detector.

errchain is the *event-log* projection of the causal ladder (media wear ->
ECC/remap -> latency tail -> visible faults -> crash, ssd3.md К3): it answers
Resilience's question from the compensation side. Retries (early events) are
the system successfully paying to hide damage; once damage events appear
too, and a crash follows within days, compensation has failed -- that IS a
loss of resilience (К7), independent of what static SMART levels say (К4: R
comes from dynamics, and an escalating error chain is dynamics).

Pure, deterministic, no ML (T3.2). Time is anchored on ``received_at`` (the
server's own clock) -- never the agent's ``ts``, which can drift or lie.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

STORAGE_EARLY = {153, 129}  # retry / reset-to-device -- compensation still working
STORAGE_DAMAGE = {55, 7, 51}  # corruption / bad block / paging error -- damage visible
CRASH = {41, 1001, 6008}  # unclean shutdown / bugcheck / unexpected restart

# Providers the agent's whitelist (client/collectors/events.py, T3.1) attaches
# to STORAGE_EARLY/STORAGE_DAMAGE ids. CRASH ids are matched regardless of
# source (e.g. System-log id 6008 carries no specific provider filter).
_STORAGE_SOURCES = {"disk", "storahci", "stornvme", "Ntfs"}

APP_HANG_ID = 1002  # Application Hang -- deliberately NOT part of the chain;
# T3.3 forwards its 30d count to bayesian.py's stability class only.

# T3.1 also collects disk/157 (surprise-removal) for raw-event visibility; no
# coordinate has been announced for it yet (К8), so it is intentionally left
# unclassified here rather than guessed into a set.

_WINDOW_DAYS = 30
_RECURRENCE_MAX_GAP_DAYS = 7
_MIN_BURSTINESS_EVENTS = 4


@dataclass(frozen=True)
class ErrChain:
    """A Resilience *observer*, never a coordinate itself (К1). ``stage`` 3 is
    a hard-evidence flag for h4 (compensation exhausted, ssd3.md §1.3); the
    rest is R-side evidence already consumed by storage.py's tagged rules.
    """

    stage: int  # 0 none; 1 retries only; 2 damage present; 3 damage->crash within 7d
    burstiness: Optional[float]  # stdev/mean of storage-error gaps; None if fewer than 4 events
    recurrent_weeks: int  # distinct ISO weeks with >=1 storage error in the 30d window
    counts: dict[str, int]  # {"early", "damage", "crash", "app_hang"}; app_hang is bayes-only
    factors: list[dict]  # Russian-language evidence labels (Ф6/Ф7 dashboard)


def _parse_received_at(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def analyze_events(events: list[dict], *, now: datetime) -> ErrChain:
    window_start = now - timedelta(days=_WINDOW_DAYS)
    early: list[datetime] = []
    damage: list[datetime] = []
    crash: list[datetime] = []
    app_hang = 0

    for e in events:
        ts = _parse_received_at(e.get("received_at"))
        if ts is None or ts < window_start:
            continue
        eid_raw = e.get("event_id")
        if eid_raw is None:
            continue
        try:
            eid = int(eid_raw)
        except (TypeError, ValueError):
            continue
        source = e.get("source")
        if eid in CRASH:
            crash.append(ts)
        elif eid == APP_HANG_ID:
            app_hang += 1
        elif source in _STORAGE_SOURCES and eid in STORAGE_EARLY:
            early.append(ts)
        elif source in _STORAGE_SOURCES and eid in STORAGE_DAMAGE:
            damage.append(ts)

    factors: list[dict] = []
    stage = 0
    if damage and any(
        0 <= (c - d).total_seconds() <= _RECURRENCE_MAX_GAP_DAYS * 86400
        for d in damage
        for c in crash
    ):
        stage = 3
        factors.append({"label": "повреждение диска сменилось крахом системы в течение 7 дней"})
    elif damage:
        stage = 2
        factors.append({"label": f"повреждения диска: {len(damage)}x за 30 дней"})
    elif early:
        stage = 1
        factors.append(
            {"label": f"повторные попытки чтения/записи диска: {len(early)}x за 30 дней"}
        )

    storage_ts = sorted(early + damage)
    burstiness: Optional[float] = None
    if len(storage_ts) >= _MIN_BURSTINESS_EVENTS:
        gaps = [(b - a).total_seconds() for a, b in zip(storage_ts, storage_ts[1:])]
        mean_gap = statistics.fmean(gaps)
        burstiness = (statistics.stdev(gaps) / mean_gap) if mean_gap > 0 else None
    if burstiness is not None and burstiness > 2:
        factors.append(
            {"label": f"ошибки диска идут кластерами (не равномерно, B={burstiness:.1f})"}
        )

    recurrent_weeks = len({(t.isocalendar()[0], t.isocalendar()[1]) for t in storage_ts})
    if recurrent_weeks >= 2:
        factors.append({"label": f"ошибки диска повторяются {recurrent_weeks} разных недель"})

    counts = {"early": len(early), "damage": len(damage), "crash": len(crash), "app_hang": app_hang}
    return ErrChain(
        stage=stage,
        burstiness=burstiness,
        recurrent_weeks=recurrent_weeks,
        counts=counts,
        factors=factors,
    )
