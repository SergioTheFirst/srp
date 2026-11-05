"""Health state ladder (spec 2026-10-30, stage 2a): the flag sets, the storage
Resilience channel and the h0..h4 rung -- pure, no imports from health.py, so
health.py and health_dominant.py both import it without a cycle (H10 storage_rung).
"""

from __future__ import annotations

from typing import Optional

_SPARE_SURCHARGE = 25.0
_SPARE_FLAG_ETA_DAYS = 90.0  # a spare ETA beyond 90 days is informative, not compensation
_TAIL_SURCHARGE = 20.0
_H2_DAMAGE = 40.0  # band "bad" boundary: heavy stable damage is h2, not h1 (D4)
_TRAJ_MULT = 0.85  # trajectory risk -> R loss (health._resilience, storage_rung)

# Hard Damage flags (storage coords carry D and R flags in one flat list; these
# are the D ones -- everything else on the list is a Resilience flag).
_HARD_D_FLAGS = frozenset(
    {
        "predict_fail",
        "cw_reliability",
        "cw_readonly",
        "pending_gt10",
        "uncorrectable_198",
        "damage_present",
    }
)
# State ladder by absolute field risk (D4; first match wins in _rung, top to bottom).
_H4_FLAGS = frozenset({"predict_fail", "cw_readonly", "chain_stage3"})
# Hard media mechanisms; synonyms collapse to one (cw_spare == spare_below_threshold).
# Backblaze 2014: stable 197 in 6-16 -> P30 ~4.7 %; 198 in 0-2 -> P7 ~1.5 %; attr5 1-4
# is the zero bucket. Two nonzero attributes ~57 %/yr (Backblaze 2016) -> h4.
_HARD_MEDIA = {
    "pending_gt10": "pending",
    "uncorrectable_198": "offline_uncorrectable",
    "cw_spare": "spare",
    "spare_below_threshold": "spare",
    "cw_reliability": "nvme_reliability",
}
# RAIDShield: "within days" only with fast growth -> hard media + growth is h4.
_GROWTH_FLAGS = frozenset({"accel", "recurrence"})
_H3_FLAGS = frozenset(_HARD_MEDIA) | _GROWTH_FLAGS | {"chain_stage2"}
_H2_FLAGS = frozenset(
    {"remap_masking", "spare_depleting", "tail_ratio_worsening", "early_events", "fs_corruption"}
)  # fs_corruption: an isolated Ntfs 55 (H6)
_H1_FLAGS = frozenset({"aging_leak", "aging_accelerating"})  # software aging: at most h1 (D2)
# What turns a worsening latency tail into the h2 flag. A stable small damage count
# (damage_present alone) is not corroboration: it would make tail noise h2 episodes.
_TAIL_CORROBORATION = (
    frozenset(_HARD_MEDIA)
    | _GROWTH_FLAGS
    | _H4_FLAGS
    | {"cmd_timeouts", "early_events", "remap_masking", "chain_stage2", "spare_consumed"}
)


def _storage_resilience(st: dict, trends: dict) -> tuple[float, str, list, list]:
    coords = st.get("coords") or {}
    loss = float(coords.get("resilience_loss") or 0.0)
    raw_flags = set(coords.get("flags") or [])
    flags = [f for f in (coords.get("flags") or []) if f not in _HARD_D_FLAGS]
    spare = trends.get("nvme_spare") or {}
    eta = spare.get("eta_days")
    if spare.get("direction") == "worsening" and eta is not None and eta <= _SPARE_FLAG_ETA_DAYS:
        flags.append("spare_depleting")
        if "compensation_breach" not in raw_flags:  # one spare trend is counted once
            loss += _SPARE_SURCHARGE
    if (trends.get("disk_tail_ratio") or {}).get("direction") == "worsening":
        # significance comes with the trend contract; the h2 flag needs corroboration
        loss += _TAIL_SURCHARGE
        if raw_flags & _TAIL_CORROBORATION:
            flags.append("tail_ratio_worsening")
    return min(loss, 100.0), st.get("confidence", "unknown"), st.get("factors") or [], flags


# H8: the rung's own source -- what the dominant, the action and the journal read.
# Ties (each would set the rung alone): storage evidence first -- the disk's own R loss
# and heavy damage outrank the soft H2 flags, which outrank the trajectory; R losses
# storage > trajectory > aging; at h1 D storage > damage_present > D os > aging flags.
_R_ORDER = ("storage", "trajectory", "aging")


def _flag(flags: set, pool: frozenset) -> Optional[str]:
    hits = sorted(flags & pool)
    return f"flag:{hits[0]}" if hits else None


def _part(parts: dict, order: tuple, floor: float) -> Optional[str]:
    return next((k for k in order if (parts.get(k) or 0.0) >= floor), None)


def rung_source(
    state: str, d_parts: dict, r_parts: dict, flags: set, st_damage: float
) -> Optional[str]:
    """Which rule of ``_rung`` set ``state`` (H8): flag:<name>, rloss:<channel>,
    heavy_storage, d:<channel>; None for h0. An isolated Ntfs 55 (fs_corruption) names
    the h2 only when nothing else would (H6: it set the level alone)."""
    if state == "h4":
        return _flag(flags, _H4_FLAGS) or _flag(flags, frozenset(_HARD_MEDIA))
    if state == "h3":
        rl = _part(r_parts, _R_ORDER, 70.0)
        return _flag(flags, _H3_FLAGS) or (f"rloss:{rl}" if rl else None)
    if state == "h2":
        if (r_parts.get("storage") or 0.0) >= 40.0:
            return "rloss:storage"  # a soft flag never softens the disk's own R loss
        if st_damage >= _H2_DAMAGE and "damage_present" in flags:
            return "heavy_storage"
        rl = _part(r_parts, _R_ORDER, 40.0)
        found = _flag(flags, _H2_FLAGS - {"fs_corruption"}) or (f"rloss:{rl}" if rl else None)
        return found or _flag(flags, frozenset({"fs_corruption"}))
    if state == "h1":
        if (d_parts.get("storage") or 0.0) >= 15.0:
            return "d:storage"
        if "damage_present" in flags:
            return "flag:damage_present"
        return "d:os" if (d_parts.get("os") or 0.0) >= 15.0 else _flag(flags, _H1_FLAGS)
    return None


def _rung(d_val: float, rv: Optional[float], flags: set, st_damage: float) -> str:
    media = {_HARD_MEDIA[f] for f in flags if f in _HARD_MEDIA}
    active = bool(flags & _GROWTH_FLAGS) or "spare_consumed" in flags  # growth / fresh low
    if flags & _H4_FLAGS or len(media) >= 2 or (media and active):
        return "h4"
    if flags & _H3_FLAGS or (rv is not None and rv <= 30):
        return "h3"
    heavy = st_damage >= _H2_DAMAGE and "damage_present" in flags
    if flags & _H2_FLAGS or (rv is not None and rv <= 60) or heavy:
        return "h2"
    if d_val >= 15 or "damage_present" in flags or flags & _H1_FLAGS:
        return "h1"
    return "h0"
