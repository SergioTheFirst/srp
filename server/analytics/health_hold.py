"""Hysteresis, disk identity and horizon for ``server.analytics.health`` (spec D3, D5).

PURE: no I/O, no clock read (``now`` always comes from the caller), and no import
of ``health`` -- health imports this module. Every input is an already-serialised
``dict``: the storage axis of ``score100`` and the previous stored health blob.

A held state steps down exactly one level after RELEASE_HOURS of OBSERVED time
below it; any cycle whose raw state is back at (or above) the held one resets the
clock. A cycle that cannot see the storage (gated axis, axis gone on a machine that
had a disk, a listed disk that lost its SMART attributes, the worst disk dropped from
the SMART rows while the inventory still lists it or no inventory list at all) freezes
the release and carries the disk identity forward; a held h1+ stays visible even when
such a cycle has no damage channel at all (raw 'unknown', D8). ``had_disk`` is sticky;
rows stored before disk ids existed infer it from their storage verdict. A replacement
(the old worst id gone from the SMART rows and the inventory AND a new id listed)
permits a full reset; a disk that merely vanished does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Single source of the state order (db.py keeps its documented mirror).
STATE_ORDER = {"h0": 0, "h1": 1, "h2": 2, "h3": 3, "h4": 4}
RANK_STATE = {v: k for k, v in STATE_ORDER.items()}

# 12 agent cycles of 4 h: a one-cycle blip cannot release and a daily recurrence keeps
# the hold. h4 -> h0 takes >= 8 observed days -- longer than the 7-day stage-3 recency
# and comparable to recurrence clustering (Nightingale: 86 % of recurrences within
# 10 days of uptime).
RELEASE_HOURS = 48.0
# 1.5x the 4 h cadence: nights and weekends without telemetry do not count, and 1-4
# recomputes of one cycle add ~4 h, not 4x.
MAX_GAP_HOURS = 6.0

_NOTE_HELD = (
    "состояние удержано: снижение подтверждается после {need:.0f} ч наблюдения"
    " без ухудшения (набрано {got:.0f} ч)"
)
_NOTE_BLIND = "SMART временно недоступен — снижение не подтверждается"
_NOTE_OFFLINE = "свежих данных хранилища нет — отсчёт снижения на паузе"
_NOTE_UNKNOWN = "видимость хранилища потеряна, причина не установлена — отсчёт на паузе"
_NOTE_UNKNOWN_AGAIN = (
    "повторная потеря видимости хранилища без установленной причины — отсчёт снижения начат заново"
)
_NOTE_VANISHED = "диск пропал из перечня SMART — снижение подтверждается по времени"

_STATE_WINDOWS = {
    "h4": (7, "предотказ — действовать в течение 7 дней (срок реакции, не прогноз отказа)"),
    "h3": (
        30,
        "ускоренная деградация — действовать в течение 30 дней (срок реакции, не прогноз отказа)",
    ),
    "h2": (90, "компенсация — действовать в течение 90 дней (срок реакции, не прогноз отказа)"),
}
_ETA_BUCKETS = (7, 30, 90, 180)
# Depletion trends that may forecast a limit. storage_wear is excluded on purpose:
# rated endurance is not failure (NVMe 2.0c: 100 % used "may not indicate" one).
_ETA_BASIS = {
    "disk_fill": "свободное место на системном диске до 5%",
    "nvme_spare": "резерв NVMe до порога производителя",
}


# --------------------------------------------------------------------------- #
# Disk identity / visibility
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DiskView:
    present: bool  # the storage axis has a value
    gated: bool  # the trust gate hid the axis
    ids: frozenset[str]  # serial hashes + FriendlyNames of the scored disks
    attr_ids: frozenset[str]  # disks that reported SMART attributes / an NVMe log
    worst_id: Optional[str]
    inventory: frozenset[str] = frozenset()  # Get-PhysicalDisk: serial hash, else FriendlyName
    # trust of the storage_reliability source (lineage storage_source, spec H1); None = no hint
    source_state: Optional[str] = None
    source_collector: Optional[str] = None
    source_ran: bool = False  # the historical script returned other data (empty = no disks)
    # H5: disks with real health data; None = a lineage from before H5 (no phantom call)
    health_ids: Optional[frozenset[str]] = None


def _id_set(values: object) -> frozenset[str]:
    seq = values if isinstance(values, (list, tuple, set, frozenset)) else ()
    return frozenset(str(x) for x in seq if x)


def disk_view(storage_axis: dict) -> DiskView:
    lineage = storage_axis.get("source_lineage")
    lineage = lineage if isinstance(lineage, dict) else {}
    present = storage_axis.get("value") is not None
    gated = lineage.get("storage_domain") == "unknown"
    visible = present and not gated
    # old lineage (no disk_ids) -> the FriendlyName rule; None never enters the set
    ids = _id_set(lineage.get("disk_ids") or [lineage.get("worst_disk")])
    attr_ids = _id_set(lineage.get("attr_disk_ids"))
    worst = lineage.get("worst_disk_id") or lineage.get("worst_disk")
    source = lineage.get("storage_source")
    source = source if isinstance(source, dict) else {}
    return DiskView(
        present=present,
        gated=gated,
        ids=ids if visible else frozenset(),
        attr_ids=attr_ids if visible else frozenset(),
        worst_id=str(worst) if worst else None,
        inventory=_id_set(lineage.get("inventory_disk_ids")),  # added by pipeline
        source_state=_opt_str(source.get("state")),
        source_collector=_opt_str(source.get("collector_status")),
        source_ran=source.get("historical_ran") is True,
        health_ids=(
            _id_set(lineage.get("health_disk_ids"))
            if visible and "health_disk_ids" in lineage
            else None
        ),
    )


def _opt_str(value: object) -> Optional[str]:
    return str(value) if value else None


def _prev_disk_id(prev: dict) -> Optional[str]:
    v = prev.get("worst_disk_id") or prev.get("worst_disk")
    return str(v) if v else None


def _had_disk(view: DiskView, prev: dict) -> bool:
    """A disk was ever listed (sticky). A row stored before disk ids existed (no
    'disk_ids' key) and latched on storage counts too: an old SMART-less row stores
    worst_disk None, and without it the release clock would run while blind."""
    old_storage_row = "disk_ids" not in prev and prev.get("dominant") == "storage"
    return bool(view.ids or prev.get("had_disk") or _prev_disk_id(prev) or old_storage_row)


def _blind_rule(view: DiskView, prev: dict) -> tuple[Optional[str], bool]:
    """(rule that made the cycle blind: gated | listed | attrs | lost | None, worst gone)."""
    prev_id = _prev_disk_id(prev)
    # a listed disk lost its SMART attributes / NVMe log: the SMART script timed out
    # (client/collectors/smart.py), and "smart" is optional for the storage domain
    attrs_lost = bool((_id_set(prev.get("attr_disk_ids")) - view.attr_ids) & view.ids)
    gone = view.present and not view.gated and bool(prev_id) and prev_id not in view.ids
    # The agent drops a disk that stops answering the reliability/SMART query from its
    # storage rows (client/collectors/historical.py) while Get-PhysicalDisk still lists
    # it; with no inventory list a gone disk is unknowable -- blind either way.
    listed = gone and (not view.inventory or prev_id in view.inventory)
    # no axis + the old worst disk missing from a non-empty FIXED inventory (a USB disk
    # scored before 0.3.4, or a disk that is gone): vanished, not "SMART lost" -- else the
    # hold froze forever on a PC whose only SMART rows came from a USB disk
    absent = bool(prev_id) and bool(view.inventory) and prev_id not in view.inventory
    lost = _had_disk(view, prev) and not absent
    if view.gated:
        return "gated", gone
    if listed:
        return "listed", gone
    if view.present:
        return ("attrs" if attrs_lost else None), gone
    return ("lost" if lost else None), gone


def visibility(view: DiskView, prev: Optional[dict]) -> tuple[bool, bool, bool]:
    """(blind, replaced, vanished) of this cycle against the previous verdict; a blind
    cycle is never a replacement nor a vanished disk."""
    if not prev:
        return False, False, False
    rule, gone = _blind_rule(view, prev)
    blind = rule is not None
    if blind or not gone:
        return blind, False, False
    prev_ids = _id_set(prev.get("disk_ids"))
    # a NEW id must appear; old rows without disk_ids keep the FriendlyName rule
    replaced = bool(view.ids) and (bool(view.ids - prev_ids) if prev_ids else True)
    return False, replaced, bool(prev_ids) and not replaced


def smart_hidden(view: DiskView) -> bool:
    """SMART may exist but is hidden (H6: a gated axis keeps the full chain stage). Zero
    SMART rows write no storage_domain marker (storage.py answers «нет SMART» before the
    gate), so the storage_reliability hint decides, as in blind_cause: a failed read
    (timeout / blocked / absent, or EMPTY with the historical script silent) hides SMART;
    only EMPTY after the script ran -- the disks answered nothing -- is no SMART at all."""
    failed = view.source_state == "unavailable" and not (
        view.source_collector == "empty" and view.source_ran
    )
    return view.gated or failed


def damage_seen(flags: list, prev: Optional[dict], replaced: bool) -> bool:
    """H8: damage_present this cycle, or seen by the previous cycle of the episode (its D
    flags, or the held_source it set) -- a CHECK flag that keeps the level is no hold, so
    a SMART-blind cycle or a 197 toggle must not lose it; a replaced disk starts clean."""
    p = prev or {}
    was = p.get("held_source") == "flag:damage_present" or "damage_present" in (
        (p.get("damage") or {}).get("flags") or ()
    )
    return "damage_present" in flags or (was and not replaced)


def blind_cause(view: DiskView, prev: Optional[dict]) -> Optional[str]:
    """Why the disks are not seen (spec H1): loss | offline | unknown; None when seen.

    loss -- SMART really went away (a listed disk dropped from the rows or lost its
    attributes, or the historical script ran and the disks returned nothing); offline -- the gate
    hid the axis because no fresh historical arrived (STALE is only set after a good
    reading); unknown -- any other blindness (suspect, timeout/blocked/absent, no hint).
    """
    if not prev:
        return None
    rule, _ = _blind_rule(view, prev)
    if rule is None:
        return None
    if rule in ("listed", "attrs"):
        return "loss"
    if rule == "lost":  # 'empty' alone may be PowerShell printing nothing at all
        return "loss" if view.source_collector == "empty" and view.source_ran else "unknown"
    return "offline" if view.source_state == "stale" else "unknown"


# H5 (review 3e): "never had health data" is per DISK, not per device -- a SMART disk
# swapped for (or pulled next to) a wear/temperature-only one says nothing about it.
PHANTOM_MISSING = "SMART/NVMe-лог не получен — оценка только по износу и температуре"


def _health_ids_before(prev: Optional[dict]) -> frozenset[str]:
    """Disks that reported health data on an earlier cycle (sticky per disk; rows before
    H5 seed it from the attribute ids, which an attrs-lost episode carries forward)."""
    p = prev or {}
    return _id_set(p.get("health_seen_ids")) | _id_set(p.get("attr_disk_ids"))


def phantom(view: DiskView, prev: Optional[dict]) -> bool:
    """Phantom SMART (H5): the disks are SEEN (not a blind cycle -- that is H1's loss /
    offline / unknown) and none listed now has health data, and none of the disks still
    installed (scored rows or the inventory) ever had: a SMART disk whose row dropped or
    whose data timed out stays itself (spec 2026-10-09 §5.16, review 3e)."""
    seen = view.present and not view.gated and view.health_ids == frozenset()
    if not seen or visibility(view, prev)[0]:
        return False
    return not (_health_ids_before(prev) & (view.ids | view.inventory))


def identity(view: DiskView, prev: Optional[dict], blind: bool) -> dict:
    """Disk identity to persist. A cycle that cannot see the disks carries the previous
    identity forward, so the next cycle still knows a disk (and its attributes)
    existed: an attrs-lost freeze lasts until the attributes come back."""
    p = prev or {}
    before = _health_ids_before(p)
    visible = view.present and not view.gated and not blind
    installed = view.ids | view.inventory  # a disk out of the rows but still listed stays
    seen_ids = (before & installed) | (view.health_ids or frozenset()) if visible else before
    had = {"had_disk": _had_disk(view, p), "health_seen_ids": sorted(seen_ids)}
    if visible:
        return {
            "worst_disk_id": view.worst_id,
            "disk_ids": sorted(view.ids),
            "attr_disk_ids": sorted(view.attr_ids),
            **had,
        }
    return {
        "worst_disk_id": _prev_disk_id(p),
        "disk_ids": sorted(_id_set(p.get("disk_ids"))),
        "attr_disk_ids": sorted(_id_set(p.get("attr_disk_ids"))),
        **had,
    }


# --------------------------------------------------------------------------- #
# Hysteresis
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Hold:
    state: str
    held: bool  # the state is kept above the raw one
    below_h: Optional[float]  # observed hours below the held state
    below_seen_at: Optional[str]  # ISO time of the last counted observation
    pause_used: bool = False  # this release window spent its one unknown-blind pause (H1)
    restarted: bool = False  # a repeated unknown episode reset the clock this cycle


def _gap_hours(now: datetime, seen: object) -> float:
    try:
        then = datetime.fromisoformat(str(seen).strip().replace("Z", "+00:00"))
        hours = (now - then).total_seconds() / 3600.0
    except (TypeError, ValueError):  # unparseable / naive-vs-aware -> nothing observed
        return 0.0
    return min(max(hours, 0.0), MAX_GAP_HOURS)


def _blind_hold(prev_state: str, prev: dict, cause: Optional[str]) -> Hold:
    """Keep the held state on a blind cycle (H1): offline and the first unknown episode
    of a release window pause the clock (no hours, seen_at None -> the next seen cycle
    adds 0 h); a SMART loss -- or no cause given -- and a repeated unknown episode reset
    it. Every path keeps the state, so a pause can only delay a release, never add one."""
    used = bool(prev.get("unknown_pause_used"))
    below = prev.get("below_h")
    if cause == "offline":
        return Hold(prev_state, True, below, None, pause_used=used)
    if cause == "unknown":
        # the previous cycle was blind for any reason: the same blind stretch (an offline
        # morning whose first historical fails turns unknown a minute later -- D24)
        if prev.get("blind_cause") or not used:
            return Hold(prev_state, True, below, None, pause_used=True)
        return Hold(prev_state, True, None, None, pause_used=True, restarted=True)
    return Hold(prev_state, True, None, None, pause_used=used)


def hold_state(
    raw: str,
    prev: Optional[dict],
    *,
    now: Optional[datetime],
    blind: bool,
    replaced: bool,
    cause: Optional[str] = None,
) -> Hold:
    free = Hold(raw, False, None, None)
    if not prev:
        return free
    prev_state = prev.get("state")
    if prev_state == "unknown":  # a blind cycle must not erase the memory
        prev_state = prev.get("last_known_state")
    if prev_state not in STATE_ORDER:
        return free
    used = bool(prev.get("unknown_pause_used"))
    if raw == "unknown":  # D8: a held h1+ stays visible on a blind cycle
        if blind and prev_state != "h0":
            return _blind_hold(prev_state, prev, cause)
        # the K5 gate shows unknown; the observed hours wait for the next seen cycle
        return Hold(raw, False, prev.get("below_h"), None, pause_used=used)
    if STATE_ORDER[raw] >= STATE_ORDER[prev_state]:
        return free  # worsening or equal is free; the clock resets
    if blind:
        return _blind_hold(prev_state, prev, cause)
    if replaced:
        return free  # new hardware -> full reset
    if now is None:  # never release without a clock
        return Hold(prev_state, True, prev.get("below_h"), prev.get("below_seen_at"), used)
    observed = float(prev.get("below_h") or 0.0) + _gap_hours(now, prev.get("below_seen_at"))
    if observed < RELEASE_HOURS:
        return Hold(prev_state, True, round(observed, 1), now.isoformat(), pause_used=used)
    new = RANK_STATE[STATE_ORDER[prev_state] - 1]  # exactly one level down
    if STATE_ORDER[new] > STATE_ORDER[raw]:
        return Hold(new, True, 0.0, now.isoformat())
    return Hold(new, False, None, None)


def held_mechanism(
    dominant: Optional[str],
    systemic: bool,
    disk_flag: bool,
    from_hold: bool,
    prev: Optional[dict],
) -> tuple[Optional[str], Optional[str]]:
    """(dominant to show, mechanism to persist as held_dominant) -- D7.

    The mechanism that set a held level is kept apart from what a cycle shows: a live
    disk flag names the disk, a systemic cycle shows 'systemic', otherwise a held level
    shows the mechanism that set it. A label shown mid-hold never overwrites that
    mechanism, so the hold does not read a passing 'systemic' back once it is over.
    from_hold: the cycle's level comes from a hold (even when the systemic floor lifts it
    this cycle); a level the floor raises from a free state belongs to 'systemic'."""
    p = prev or {}
    mech = p["held_dominant"] if "held_dominant" in p else p.get("dominant")  # pre-split rows
    show = mech if (from_hold and mech and not systemic and not disk_flag) else dominant
    fresh = "storage" if disk_flag else show  # a disk flag set it even under a systemic label
    return show, (mech if from_hold else fresh)


def _blind_note(cause: Optional[str], restarted: bool) -> str:
    if cause == "offline":
        return _NOTE_OFFLINE
    if cause == "unknown":
        return _NOTE_UNKNOWN_AGAIN if restarted else _NOTE_UNKNOWN
    return _NOTE_BLIND


def hold_notes(
    hold: Hold, blind: bool, vanished: bool, *, cause: Optional[str] = None
) -> list[dict]:
    """Russian evidence notes (delta 0) for the verdict's state_evidence and factors."""
    held = _NOTE_HELD.format(need=RELEASE_HOURS, got=hold.below_h or 0.0)
    notes = (
        (hold.held and not blind, held),
        (blind, _blind_note(cause, hold.restarted)),
        (vanished, _NOTE_VANISHED),
    )
    return [{"label": text, "delta": 0} for on, text in notes if on]


# --------------------------------------------------------------------------- #
# Horizon: reaction window by state, or a trend forecast that names its basis
# --------------------------------------------------------------------------- #
def _forecast(trends: dict) -> Optional[tuple[Optional[int], float, str]]:
    """The soonest worsening depletion ETA: (bucket -- None past the last one, eta, trend key)."""
    best: Optional[tuple[Optional[int], float, str]] = None
    for key in _ETA_BASIS:
        t = trends.get(key) or {}
        eta = t.get("eta_days")
        if t.get("direction") != "worsening" or not isinstance(eta, (int, float)):
            continue
        if best is None or eta < best[1]:
            best = (next((b for b in _ETA_BUCKETS if eta <= b), None), float(eta), key)
    return best


_LOOSEN_MARGIN = 1.25  # H7: a looser forecast bucket only past 1.25x the held one


def _held_bucket(
    fc: Optional[tuple[Optional[int], float, str]], prev: Optional[dict]
) -> Optional[tuple[Optional[int], float, str]]:
    """H7 hysteresis: a tighter bucket at once; a looser one -- or none, past the last
    bucket (ETA > 180 d) -- only once the ETA clears 1.25x the previous bucket, and only
    while a worsening trend forecasts now AND a forecast held before (none now drops it
    at once: a cleanup must not keep «предел достигнут»)."""
    p = prev or {}
    old = p.get("horizon_days")
    if fc is None or p.get("horizon_kind") != "eta" or not isinstance(old, int):
        return fc
    if fc[0] is not None and fc[0] <= old:
        return fc
    return fc if fc[1] > _LOOSEN_MARGIN * old else (old, fc[1], fc[2])


def forecast_key(trends: dict, prev: Optional[dict] = None) -> Optional[str]:
    """The depletion trend behind a shown forecast horizon (disk_fill / nvme_spare), else
    None -- the same held bucket ``horizon`` shows for a state without a window."""
    fc = _held_bucket(_forecast(trends), prev)
    return fc[2] if fc and fc[0] is not None else None


def horizon(
    state: str, trends: dict, *, prev: Optional[dict] = None
) -> tuple[Optional[int], str, Optional[str]]:
    """(days, Russian reason, kind 'state' | 'eta' | None). The forecast wins when its
    bucket is <= the state window (ties go to the evidence-based reason) or when the
    state has no window."""
    window = _STATE_WINDOWS.get(state)
    fc = _held_bucket(_forecast(trends), prev)
    days = fc[0] if fc else None
    if fc is not None and days is not None and (window is None or days <= window[0]):
        _, eta, key = fc
        basis = _ETA_BASIS[key]
        if eta <= 0:
            reached = "предел уже достигнут — действовать в течение 7 дней"
            return days, f"прогноз по тренду ({basis}): {reached}", "eta"
        return days, f"прогноз по тренду ({basis}): предел в пределах {days} дней", "eta"
    if window is not None:
        return window[0], window[1], "state"
    return None, "", None
