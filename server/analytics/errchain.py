"""ssd3 Ф3: event-chain escalation detector.

errchain is the *event-log* projection of the causal ladder (media wear ->
ECC/remap -> latency tail -> visible faults -> crash, ssd3.md К3): it answers
Resilience's question from the compensation side. Retries (early events) are
the system successfully paying to hide damage; once damage events appear
too, and a crash follows within days, compensation has failed -- that IS a
loss of resilience (К7), independent of what static SMART levels say (К4: R
comes from dynamics, and an escalating error chain is dynamics).

Pure, deterministic, no ML (T3.2). The 30-day window is anchored on ``received_at``
(the server's own clock). The agent's ``ts`` only orders events against each other
(damage -> BugCheck, power loss -> Ntfs 55), and only while it is plausible for its
receipt (calib D9): the client clock can drift or lie.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

STORAGE_EARLY = {153, 129}  # retry / reset-to-device -- compensation still working
STORAGE_DAMAGE = {55, 7, 51}  # corruption / bad block / paging error -- damage visible
CRASH = {41, 1001, 6008}  # unclean shutdown / bugcheck / unexpected restart

# calib D9: only the WER-SystemErrorReporting BugCheck completes stage 3. KP41 and 6008
# record a power loss / unclean shutdown: Microsoft Learn calls Event 41 insufficient on
# its own, and bayesian.py rates KP41 specificity at about 0.
_HW_CRASH = frozenset({1001})
_POWER_LOSS = frozenset({41, 6008})

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
# F23: a "fails within 7 days" claim whose BugCheck is older than 7 days is falsified.
_STAGE3_RECENCY_DAYS = 7
# ...unless the crash repeats: two or more disk-attributed BugChecks in the 30-day window
# give P(third) 53-59 % (Nightingale) -> stage 3 whatever the age of the latest one.
_STAGE3_REPEAT_CRASHES = 2
# A ts is plausible within [received_at - 26 h, received_at + 10 min]: the agent's
# 24 h re-send window plus clock drift.
_TS_MAX_AGE = timedelta(hours=26)
_TS_MAX_LEAD = timedelta(minutes=10)
# An Ntfs 55 this soon after a KP41/6008 is the unclean shutdown's own record.
_NTFS_AFTER_POWER_LOSS = timedelta(hours=1)
_POWER_CAUSE_WINDOW = timedelta(hours=24)  # H6: a KP41/6008 within 24 h names power
_FRACTION = re.compile(r"\.(\d+)")

_Stamp = Tuple[datetime, Optional[datetime]]  # (received_at, plausible event ts or None)


@dataclass(frozen=True)
class ErrChain:
    """A Resilience *observer*, never a coordinate itself (К1). ``stage`` 3 is
    a hard-evidence flag for h4 (compensation exhausted, ssd3.md §1.3); the
    rest is R-side evidence already consumed by storage.py's tagged rules.
    """

    # 0 none; 1 retries only; 2 damage present; 3 damage->BugCheck within 7d, that crash
    # at most 7d old or one of >=2 such crashes in the 30d window
    stage: int
    burstiness: Optional[float]  # stdev/mean of storage-error gaps; None if fewer than 4 events
    recurrent_weeks: (
        int  # distinct ISO weeks with >=1 storage error (30d raw, 90d if rollups given)
    )
    counts: dict[str, int]  # {"early", "damage", "crash", "app_hang"}; app_hang is bayes-only
    factors: list[dict]  # Russian-language evidence labels (Ф6/Ф7 dashboard)
    # H4: the events collector reported (ok / EMPTY without a failure reason = a clean
    # journal); None = unknown. Only adds liveness to the O channel, never takes it away.
    delivered: Optional[bool] = None
    # H6: the stage without isolated Ntfs 55 (no 153/129, no 7/51, no BugCheck within 1 h
    # or 7 days after) -- what the disk itself confirms; None = rows before H6
    stage_disk_only: Optional[int] = None
    fs_isolated: int = 0  # isolated Ntfs 55 in the window
    fs_power_likely: bool = False  # a KP41/6008 in 24 h before one, or dirty shutdowns
    # H13: distinct UTC dates (YYYY-MM-DD) with a BugCheck 1001 in the 30-day window
    bugcheck_days: list[str] = field(default_factory=list)
    # H14: this machine's own 153/129 + 7/51 in the window -- no Ntfs 55 (the file system,
    # H6): the ids db._cohort_disk_errors counts for the cohort
    disk_errors: int = 0


def _parse_event_ts(value: object) -> Optional[datetime]:
    """An ISO timestamp (the agent's ``ts`` or the server's ``received_at``) as an aware
    datetime, or None. The agent sends ``TimeCreated.ToUniversalTime().ToString('o')``:
    7 fractional digits plus 'Z', which ``datetime.fromisoformat`` rejects before
    Python 3.11 -- so the fraction is cut or padded to exactly 6 digits first.
    Naive values are taken as UTC.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _plausible_ts(value: object, received: datetime) -> Optional[datetime]:
    """The event's own ts if it parses and lies in [received - 26 h, received + 10 min]."""
    ts = _parse_event_ts(value)
    if ts is None or not -_TS_MAX_AGE <= ts - received <= _TS_MAX_LEAD:
        return None
    return ts


def _event_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kind(source: object, eid: int) -> Optional[str]:
    """The chain role of one event; storage ids count only from the storage providers."""
    if eid in _HW_CRASH:
        return "bugcheck"
    if eid in _POWER_LOSS:
        return "power_loss"
    if eid == APP_HANG_ID:
        return "app_hang"
    if source not in _STORAGE_SOURCES:
        return None
    if eid in STORAGE_EARLY:
        return "early"
    if eid in STORAGE_DAMAGE:
        return "ntfs55" if (source, eid) == ("Ntfs", 55) else "damage"
    return None


def _collect(events: list[dict], window_start: datetime) -> dict[str, list[_Stamp]]:
    """In-window events grouped by chain role, as (received_at, plausible ts) stamps."""
    groups: dict[str, list[_Stamp]] = {}
    for e in events:
        received = _parse_event_ts(e.get("received_at"))
        eid = _event_id(e.get("event_id"))
        if received is None or received < window_start or eid is None:
            continue
        kind = _kind(e.get("source"), eid)
        if kind is not None:
            groups.setdefault(kind, []).append((received, _plausible_ts(e.get("ts"), received)))
    return groups


def _damage_then_crash(damage: _Stamp, crash: _Stamp) -> bool:
    """Damage, then a BugCheck within 7 days. Ordered by the events' own ts when both
    are plausible (one machine clock; strict, so a crash logged first never counts),
    otherwise by received_at, inclusive -- one batch shares one received_at."""
    (d_received, d_ts), (c_received, c_ts) = damage, crash
    max_gap = timedelta(days=_RECURRENCE_MAX_GAP_DAYS)
    if d_ts is not None and c_ts is not None:
        return timedelta(0) < c_ts - d_ts <= max_gap
    return timedelta(0) <= c_received - d_received <= max_gap


def _power_loss_artifact(
    ntfs: _Stamp, power_loss: list[datetime], bugchecks: list[_Stamp], other_damage: list[_Stamp]
) -> bool:
    """An Ntfs 55 logged within 1 h after a KP41/6008 is the unclean shutdown's own
    record, not disk damage -- unless a BugCheck 1001 lies within 1 h of it on either
    side (a BSOD reboot logs KP41, then Ntfs 55, then the 1001 once the dump is saved;
    this covers the whole anchoring hour), or came in the same batch while its own ts is
    not plausible, or other damage lies within 7 days by received_at: on a disk without
    SMART the NTFS record may be the only evidence (F19)."""
    received, ts = ntfs
    if ts is None:
        return False
    hour = _NTFS_AFTER_POWER_LOSS
    if not any(timedelta(0) < ts - p <= hour for p in power_loss):
        return False
    if any(r == received if c is None else abs(ts - c) <= hour for r, c in bugchecks):
        return False
    episode = timedelta(days=_RECURRENCE_MAX_GAP_DAYS)
    return not any(abs(received - r) <= episode for r, _ in other_damage)


def _isolated(
    ntfs: _Stamp, early: list[_Stamp], other_damage: list[_Stamp], bugchecks: list[_Stamp]
) -> bool:
    """An Ntfs 55 nothing confirms (H6): no early retries and no other disk damage in the
    window, no BugCheck within 1 h of it (a 1001 may precede the Ntfs record after a BSOD)
    and none within 7 days after it (the stage-3 rule)."""
    if early or other_damage:
        return False
    received, ts = ntfs
    hour = _NTFS_AFTER_POWER_LOSS
    for r, c in bugchecks:
        near = r == received if (c is None or ts is None) else abs(ts - c) <= hour
        if near or _damage_then_crash(ntfs, (r, c)):
            return False
    return True


def _stage(
    early: list[_Stamp], damage: list[_Stamp], bugchecks: list[_Stamp], now: datetime
) -> int:
    disk_crashes = [c for c in bugchecks if any(_damage_then_crash(d, c) for d in damage)]
    recency = timedelta(days=_STAGE3_RECENCY_DAYS)
    recent = any(now - received <= recency for received, _ in disk_crashes)
    if recent or len(disk_crashes) >= _STAGE3_REPEAT_CRASHES:
        return 3
    if damage:
        return 2
    return 1 if early else 0


def _stage_factors(stage: int, n_early: int, n_damage: int) -> list[dict]:
    if stage == 3:
        return [{"label": "повреждение диска, затем BSOD в течение 7 дней"}]
    if stage == 2:
        return [{"label": f"повреждения диска: {n_damage}x за 30 дней"}]
    if stage == 1:
        return [{"label": f"повторные попытки чтения/записи диска: {n_early}x за 30 дней"}]
    return []


def _burstiness(storage_ts: list[datetime]) -> Optional[float]:
    if len(storage_ts) < _MIN_BURSTINESS_EVENTS:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(storage_ts, storage_ts[1:])]
    mean_gap = statistics.fmean(gaps)
    return (statistics.stdev(gaps) / mean_gap) if mean_gap > 0 else None


def _iso_weeks_from_rollups(rollup_counts: list[dict]) -> set[tuple[int, int]]:
    """ISO (year, week) pairs with >=1 storage early/damage event, read from
    event_rollup_daily rows (event_key='source:event_id') -- lets recurrence
    look back across a 90d rollup window instead of just the 30d raw window
    raw events alone would allow (ssd3 Ф5, T5.4).
    """
    weeks: set[tuple[int, int]] = set()
    for row in rollup_counts:
        key = row.get("event_key")
        day = row.get("day")
        if not key or not day or not row.get("n"):
            continue
        source, _, eid_s = key.partition(":")
        if source not in _STORAGE_SOURCES:
            continue
        try:
            eid = int(eid_s)
        except ValueError:
            continue
        if eid not in STORAGE_EARLY and eid not in STORAGE_DAMAGE:
            continue
        try:
            d = datetime.fromisoformat(day).date()
        except ValueError:
            continue
        iso = d.isocalendar()
        weeks.add((iso[0], iso[1]))
    return weeks


def _fs_view(
    early: list[_Stamp],
    other_damage: list[_Stamp],
    kept_ntfs: list[_Stamp],
    bugchecks: list[_Stamp],
    power_loss_ts: list[datetime],
    dirty_shutdowns: Optional[float],
    now: datetime,
) -> tuple[int, int, bool]:
    """H6: (the stage without isolated Ntfs 55, how many there are, power-likely cause:
    a KP41/6008 within 24 h before one, or dirty shutdowns in the 30 days)."""
    isolated = [n for n in kept_ntfs if _isolated(n, early, other_damage, bugchecks)]
    confirmed = other_damage + [n for n in kept_ntfs if n not in isolated]
    window = _POWER_CAUSE_WINDOW
    after_loss = any(
        ts is not None and any(timedelta(0) < ts - p <= window for p in power_loss_ts)
        for _, ts in isolated
    )
    power = bool(isolated) and ((dirty_shutdowns or 0) >= 1 or after_loss)
    return _stage(early, confirmed, bugchecks, now), len(isolated), power


def _recurrent_weeks(storage_ts: list[datetime], rollup_counts: Optional[list[dict]]) -> int:
    weeks = {(t.isocalendar()[0], t.isocalendar()[1]) for t in storage_ts}
    if rollup_counts:
        weeks |= _iso_weeks_from_rollups(rollup_counts)
    return len(weeks)


def _chain_factors(
    stage: int,
    n_early: int,
    n_damage: int,
    excused: int,
    burstiness: Optional[float],
    recurrent_weeks: int,
) -> list[dict]:
    """Russian evidence labels of the chain (Ф6/Ф7 dashboard)."""
    factors = _stage_factors(stage, n_early, n_damage)
    if excused:
        note = "ошибки NTFS сразу после потери питания не считаются повреждением диска"
        factors.append({"label": f"{note}: {excused}"})
    if burstiness is not None and burstiness > 2:
        factors.append(
            {"label": f"ошибки диска идут кластерами (не равномерно, B={burstiness:.1f})"}
        )
    if recurrent_weeks >= 2:
        factors.append({"label": f"ошибки диска повторяются {recurrent_weeks} разных недель"})
    return factors


def _bugcheck_days(
    bugchecks: list[_Stamp], rollup_counts: Optional[list[dict]], now: datetime
) -> list[str]:
    """H13: distinct UTC dates of BugCheck 1001 in the window -- the event's own ts when
    plausible, else its receipt. The rollups are dated by RECEIPT (store_events folds under
    recv[:10]), so they add only the days the raw window lost: a receipt day a raw BugCheck
    already stands for is skipped, else one crash across UTC midnight is two days."""
    days = {(ts or received).date().isoformat() for received, ts in bugchecks}
    received_days = {received.date().isoformat() for received, _ in bugchecks}
    start = (now - timedelta(days=_WINDOW_DAYS)).date().isoformat()
    for row in rollup_counts or []:
        key, day = str(row.get("event_key") or ""), str(row.get("day") or "")[:10]
        if key.endswith(":1001") and row.get("n") and start <= day and day not in received_days:
            days.add(day)
    return sorted(days)


def _cut_replaced_disk(
    groups: dict[str, list[_Stamp]], disk_since: Optional[datetime]
) -> dict[str, list[_Stamp]]:
    """X4: the replaced only disk's own 153/129 + 7/51 leave the chain (pipeline gates it to
    one fixed disk); crashes, power loss and Ntfs 55 stay. Both the receipt and the plausible
    own ts must be after the cut: an event logged before shutdown can arrive after the swap.
    ponytail: rollup weeks (recurrent_weeks, a label only) keep the old disk's days."""
    if disk_since is None:
        return groups
    cut = ("early", "damage")
    kept = {k: [s for s in groups.get(k, []) if min(t for t in s if t) >= disk_since] for k in cut}
    return {**groups, **kept}


def analyze_events(
    events: list[dict],
    *,
    now: datetime,
    rollup_counts: Optional[list[dict]] = None,
    delivered: Optional[bool] = None,
    dirty_shutdowns: Optional[float] = None,
    disk_since: Optional[datetime] = None,
) -> ErrChain:
    groups = _cut_replaced_disk(_collect(events, now - timedelta(days=_WINDOW_DAYS)), disk_since)
    early, bugchecks = groups.get("early", []), groups.get("bugcheck", [])
    power_loss = groups.get("power_loss", [])
    other_damage, ntfs = groups.get("damage", []), groups.get("ntfs55", [])
    power_loss_ts = [ts for _, ts in power_loss if ts is not None]
    kept_ntfs = [
        n for n in ntfs if not _power_loss_artifact(n, power_loss_ts, bugchecks, other_damage)
    ]
    damage = other_damage + kept_ntfs
    stage = _stage(early, damage, bugchecks, now)
    fs = _fs_view(early, other_damage, kept_ntfs, bugchecks, power_loss_ts, dirty_shutdowns, now)

    storage_ts = sorted(received for received, _ in early + damage)
    burstiness = _burstiness(storage_ts)
    recurrent_weeks = _recurrent_weeks(storage_ts, rollup_counts)
    excused = len(ntfs) - len(kept_ntfs)
    factors = _chain_factors(stage, len(early), len(damage), excused, burstiness, recurrent_weeks)

    counts = {
        "early": len(early),
        "damage": len(damage),
        "crash": len(bugchecks) + len(power_loss),
        "app_hang": len(groups.get("app_hang", [])),
    }
    return ErrChain(
        stage=stage,
        burstiness=burstiness,
        recurrent_weeks=recurrent_weeks,
        counts=counts,
        factors=factors,
        delivered=delivered,
        stage_disk_only=fs[0],
        fs_isolated=fs[1],
        fs_power_likely=fs[2],
        bugcheck_days=_bugcheck_days(bugchecks, rollup_counts, now),
        disk_errors=len(early) + len(other_damage),
    )
