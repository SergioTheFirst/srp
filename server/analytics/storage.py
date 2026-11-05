"""W4.2 storage health engine (ssd3 Ф2: v3, coordinate-tagged).

The original ``storage_risk`` axis (a single 0-100 "how bad" number) is kept
byte-for-byte for old-style payloads/callers -- the five original rules below
score a pre-Ф1 payload exactly as before, and every ssd3 Ф2 rule (and every
calibration tweak of an original rule) keys exclusively off fields Ф1 (deep
SMART) introduced, so a payload with none of them can never change the
legacy value (T2.4 regression pin). New telemetry additionally feeds a
second, coordinate-tagged pass (K1/K8): every ssd3 rule below declares which
coordinate it judges -- Damage (accumulated, irreversible) or Resilience-loss
(current compensation being spent) -- via ``hit(..., coord=...)``. Those sums
ride alongside the legacy value as ``coords`` in the axis dict (additive; old
readers of ``storage_risk`` never see it, new ones opt in).

Design carried over from the original engine:
  * **SMART / StorageReliabilityCounter is the leading signal.**
  * **Disk latency is a confirmation, never a standalone signal** -- it may
    only amplify a problem SMART already flagged.

Output is still the ``storage_risk`` Score100 axis: untrusted identity
withholds; no SMART data anywhere -> UNKNOWN, never a confident zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, NamedTuple, Optional

from server.analytics.health_ladder import _HARD_MEDIA
from server.analytics.rulestats import RULE_LABELS, reinforcement
from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)
from server.scoring.scores import STALL_SEC

# SMART attributes that, if present, mean we actually have a storage reading to
# judge. A disk row carrying none of these tells us nothing -> UNKNOWN.
_SMART_FIELDS = (
    "reallocated_sectors",
    "read_errors_total",
    "write_errors_total",
    "wear_pct",
    "temperature_c",
    "power_on_hours",
    # ssd3 Ф1: an NVMe-only disk with none of the legacy fields above must
    # still count as "has a SMART reading", not UNKNOWN.
    "smart_predict_fail",
    "nvme_critical_warning",
    "nvme_media_errors",
    "nvme_spare_pct",
    "nvme_percentage_used",
    "read_errors_uncorrected",
    "write_errors_uncorrected",
)

# Placeholder thresholds pending fleet calibration (ssd3 DoD: tuned post-Ф7
# from real false-alarm rates, not guessed once and frozen).
_UNSAFE_SHUTDOWNS_HIGH = 10

# D4 growth: a NEW high-water mark of a damage counter within this window is
# active growth (literature: any rise within 7-30 days is h3 -- Google FAST'07,
# Backblaze); a count that stays at its level is not.
_GROWTH_RECENT = timedelta(days=14)
# ...and it must reach this value: attr 197 from 2, because Crucial MX500-class
# SSDs toggle pending 0<->1 (smartmontools drivedb); every other counter from 1.
_GROWTH_MIN_VALUE = {"197": 2.0}
# One retry/reset is routine on any PC: early_events needs a repeat (D9, the
# events reaching errchain are deduplicated).
_EARLY_EVENTS_MIN = 2

# A median over fewer than this many of a disk's own recent readings cannot
# yet rule out a transient temperature spike -- the legacy temp>70 rule still
# fires (value untouched, T2.4 pin), but confidence is not "high" without a
# confirmed sustained pattern (mirrors disk_fill.py's _MIN_HIGH_CONF_SAMPLES,
# P2-13).
_MIN_HIGH_CONF_SAMPLES = 3


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


def _has_smart(disk: dict) -> bool:
    return any(disk.get(f) is not None for f in _SMART_FIELDS) or bool(disk.get("smart_attrs"))


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class _Growth(NamedTuple):
    """Recent genuine growth on the series disk (D4)."""

    damage: bool  # a damage counter set a new maximum within _GROWTH_RECENT
    spare: bool  # nvme_spare_pct set a new minimum within _GROWTH_RECENT


def _uncorrected(row: dict[str, Any]) -> Optional[float]:
    """Uncorrected read + write I/O errors; None only when the agent sent neither."""
    read = _num(row, "read_errors_uncorrected")
    write = _num(row, "write_errors_uncorrected")
    if read is None and write is None:
        return None
    return (read or 0.0) + (write or 0.0)


def _damage_counters(row: dict[str, Any]) -> dict[str, Optional[float]]:
    """The counters growth watches. The uncorrected pair is the only one on a
    disk with neither ATA attributes nor an NVMe log."""
    attrs = row.get("smart_attrs")
    attrs = attrs if isinstance(attrs, dict) else None
    return {
        **{attr_id: _num(attrs, attr_id) for attr_id in ("5", "187", "197", "198")},
        "nvme_media_errors": _num(row, "nvme_media_errors"),
        "uncorrected": _uncorrected(row),
    }


def _recent_growth(disk_series: list[dict[str, Any]]) -> _Growth:
    """New damage maxima / NVMe spare minima whose latest one falls within
    _GROWTH_RECENT of the newest row of one disk's own series.

    Each counter's FIRST reading only seeds its running extreme, so a count
    that is already high when the window opens, or stays at its level, is not
    growth; neither is a return to a previous maximum. Replaces the old pair
    test, which fired on one increment (or a 197 0<->1 toggle) for the whole
    series window.
    """
    points: list[tuple[datetime, dict[str, Any]]] = []
    for row in disk_series:
        when = _parse_iso(row.get("received_at") or row.get("ts"))
        if when is not None:
            points.append((when, row))
    if not points:
        return _Growth(damage=False, spare=False)
    points.sort(key=lambda p: p[0])
    highs: dict[str, float] = {}
    spare_low: Optional[float] = None
    last_damage: Optional[datetime] = None
    last_spare: Optional[datetime] = None
    for when, row in points:
        for name, value in _damage_counters(row).items():
            if value is None:
                continue
            high = highs.get(name)
            if high is not None and value > high and value >= _GROWTH_MIN_VALUE.get(name, 1.0):
                last_damage = when
            highs[name] = value if high is None else max(high, value)
        spare = _num(row, "nvme_spare_pct")
        if spare is not None:
            if spare_low is not None and spare < spare_low:
                last_spare = when
            spare_low = spare if spare_low is None else min(spare_low, spare)
    recent = points[-1][0] - _GROWTH_RECENT
    return _Growth(
        damage=last_damage is not None and last_damage >= recent,
        spare=last_spare is not None and last_spare >= recent,
    )


def _growth_hits(
    disk_series: list[dict[str, Any]],
    hit: Callable[..., None],
    hit_mult: Callable[..., None],
    reinforcement_for: Callable[[str], float],
) -> None:
    """D4 growth rules of the target disk, through _score_disk's own hit closures: a
    new damage maximum within _GROWTH_RECENT is recurrence; a new NVMe spare
    minimum is the informational spare_consumed flag."""
    growth = _recent_growth(disk_series)
    if growth.damage:
        m = reinforcement_for("media_recurrence")
        hit_mult("рост дефектов диска за последние 14 дней", 1.3 * m, "R", 30 * m, "recurrence")
    if growth.spare:
        # legacy=True only so this zero-delta informational factor does not arm
        # _score_disk's bathtub tie-breaker; health pairs the flag with a hard
        # spare/media flag.
        hit(
            "резерв NVMe расходуется: новый минимум за 14 дней",
            0.0,
            flag="spare_consumed",
            legacy=True,
        )


def _temp_sustained_high(disk: dict, disk_series: Optional[list[dict[str, Any]]]) -> bool:
    """True when THIS disk's own recent history confirms temp > 70 is a
    sustained pattern (median across recent readings), not a single transient
    spike. Gated to disk_series actually belonging to this disk (matched by
    serial_hash -- the same is_target_disk convention the dynamics rules use);
    without that history we cannot rule out a spike, so this returns False rather
    than assume sustained (P2-13). Known limitation shared with every other
    disk_series-gated rule in this file: pipeline.py only fetches a series
    for worst_disk_key()'s pick (ranked by ssd3 D+R, blind to this legacy
    rule), so a genuinely sustained-hot disk that isn't that pick still caps
    at "medium" here -- safe (never wrongly "high"), just not maximally
    informative in that multi-disk case."""
    if not disk_series or disk_series[0].get("serial_hash") != disk.get("serial_hash"):
        return False
    temps = [t for t in (_num(row, "temperature_c") for row in disk_series) if t is not None]
    return len(temps) >= _MIN_HIGH_CONF_SAMPLES and median(temps) > 70


def _score_disk(
    disk: dict,
    heartbeat: Optional[dict],
    *,
    disk_series: Optional[list[dict[str, Any]]] = None,
    chain: Optional[Any] = None,
    trends: Optional[dict[str, Any]] = None,
    rule_stats: Optional[dict[str, dict[str, int]]] = None,
    device_damaged: bool = False,
) -> tuple[float, list[Factor], dict[str, Any]]:
    """Risk 0..100 for one disk (higher = closer to failure) + its (D, R) tags.

    ``disk_series``/``trends`` are the WORST disk's own history/dynamics
    (picked by ``worst_disk_key`` before this runs) -- their rules only fire
    for the disk they were computed from (matched by serial_hash), never for
    a sibling disk being scored in the same pass. ``chain`` (Ф3, not yet
    wired) is device-wide, so its rules apply regardless of which disk.
    """
    value = 0.0
    damage = 0.0
    resilience_loss = 0.0
    factors: list[Factor] = []
    flags: list[str] = []
    multipliers: list[float] = []
    other_new_factor = False

    def hit(
        label: str,
        delta: float,
        coord: Optional[str] = None,
        coord_pts: Optional[float] = None,
        flag: Optional[str] = None,
        *,
        legacy: bool = False,
    ) -> None:
        nonlocal value, damage, resilience_loss, other_new_factor
        value += delta
        factors.append({"label": label, "delta": round(delta, 1)})
        if not legacy:
            other_new_factor = True
        if coord == "D":
            damage += coord_pts if coord_pts is not None else delta
        elif coord == "R":
            resilience_loss += coord_pts if coord_pts is not None else delta
        if flag:
            flags.append(flag)

    def hit_mult(
        label: str, mult: float, coord: str, coord_pts: float, flag: Optional[str] = None
    ) -> None:
        """A dynamics rule: scales the axis via a multiplier (max-wins, applied
        once at the end) instead of a flat addend; the coordinate still gets
        an immediate flat contribution."""
        nonlocal damage, resilience_loss, other_new_factor
        multipliers.append(mult)
        other_new_factor = True
        factors.append({"label": label, "delta": round(coord_pts, 1)})
        if coord == "D":
            damage += coord_pts
        elif coord == "R":
            resilience_loss += coord_pts
        if flag:
            flags.append(flag)

    def _reinforcement_for(rule_key: str) -> float:
        """Reinforcement multiplier for rule_key (1.0 when rule_stats is None/empty/
        below threshold -- see rulestats.reinforcement). When the multiplier is not
        neutral, also records a zero-score lineage factor so the dashboard shows
        WHY a rule's contribution changed, not just that it did."""
        stats = (rule_stats or {}).get(rule_key, {})
        mult = reinforcement(rule_key, stats)
        if mult > 1.0:
            hit(
                f"правило «{RULE_LABELS[rule_key]}» подтверждено парком: "
                f"{stats.get('confirmed', 0)} случаев",
                0.0,
            )
        elif mult < 1.0:
            hit(
                f"правило «{RULE_LABELS[rule_key]}» приглушено: на этом парке не подтверждается",
                0.0,
            )
        return mult

    # ----- legacy rules (values byte-for-byte unchanged; T2.4 regression pin) -----
    realloc = _num(disk, "reallocated_sectors")
    if realloc is not None and realloc > 0:
        hit(
            f"{int(realloc)} переназначенных секторов"
            + (" — диск отказывает" if realloc > 100 else ""),
            60 if realloc > 100 else 35,
            legacy=True,
        )

    read_err = _num(disk, "read_errors_total") or 0.0
    write_err = _num(disk, "write_errors_total") or 0.0
    io_errors = read_err + write_err
    # D11: the totals include ECC-corrected I/O; once the agent sends the
    # uncorrected split, that split is scored instead (D40 below).
    if io_errors > 0 and _uncorrected(disk) is None:
        hit(
            f"{int(io_errors)} накопленных I/O ошибок",
            60 if io_errors > 100 else 40,
            legacy=True,
        )

    # D4: on a Ф1 SATA payload (serial_hash, no NVMe log) the 25/40 wear hits
    # also feed D; legacy=True keeps the value and other_new_factor unchanged.
    wear_coord = (
        "D" if _num(disk, "nvme_percentage_used") is None and disk.get("serial_hash") else None
    )
    wear = _num(disk, "wear_pct")
    if wear is not None:
        if wear > 95:
            hit(f"износ SSD {wear:.0f}% (конец ресурса)", 40, wear_coord, legacy=True)
        elif wear > 85:
            hit(f"износ SSD {wear:.0f}%", 25, wear_coord, legacy=True)
        elif wear > 70:
            hit(f"износ SSD {wear:.0f}%", 12, legacy=True)

    temp = _num(disk, "temperature_c")
    if temp is not None:
        if temp > 70:
            stable = _temp_sustained_high(disk, disk_series)
            hit(
                f"диск {int(temp)}°C (тепловой стресс)",
                15,
                flag=None if stable else "temp_unstable",
                legacy=True,
            )
        elif temp > 60:
            hit(f"диск {int(temp)}°C (нагрев)", 8, legacy=True)

    poh = _num(disk, "power_on_hours")
    if poh is not None:
        if poh > 40000:
            hit(f"Power-on {poh / 1000:.0f}k ч", 8, legacy=True)
        elif poh > 25000:
            hit(f"Power-on {poh / 1000:.0f}k ч", 4, legacy=True)

    # ----- ssd3 Ф2: coordinate-tagged rules (K8) -- exclusively Ф1 fields, -----
    # ----- so a pre-Ф1 payload never triggers any of these.               -----
    if disk.get("smart_predict_fail"):
        hit("прошивка предсказывает отказ диска", 70, "D", 70, "predict_fail")

    cw_raw = disk.get("nvme_critical_warning")
    cw = int(cw_raw) if isinstance(cw_raw, (int, float)) else None
    if cw is not None:
        if cw & 0b00001:
            hit("NVMe: резерв компенсации ниже порога", 70, "R", 70, "cw_spare")
        if cw & 0b00010:
            hit("NVMe: критическая температура", 30, "R", 30)
        if cw & 0b00100:
            hit("NVMe: надёжность подсистемы деградировала", 70, "D", 70, "cw_reliability")
        if cw & 0b01000:
            hit("NVMe: диск в режиме только для чтения", 80, "D", 80, "cw_readonly")
        if cw & 0b10000:
            hit("NVMe: сбой резервного питания энергозависимой памяти", 40, "D", 40)

    attrs = disk.get("smart_attrs")
    attrs = attrs if isinstance(attrs, dict) else {}
    pending = _num(attrs, "197")
    if pending is not None and pending > 10:
        m = _reinforcement_for("pending_high")
        hit(
            f"{int(pending)} секторов в ожидании переназначения",
            60 * m,
            "D",
            60 * m,
            "pending_gt10",
        )
    elif pending == 1:
        # D4: one pending sector is D30 -- MX500-class SSDs toggle 197 0<->1.
        hit("1 сектор в ожидании переназначения", 30, "D", 30, "damage_present")
    elif pending is not None and pending > 0:
        hit(f"{int(pending)} секторов в ожидании переназначения", 45, "D", 45, "damage_present")

    uncorrectable = _num(attrs, "198")
    if uncorrectable is not None and uncorrectable > 0:
        hit(
            f"{int(uncorrectable)} неисправимых секторов (attr 198)",
            60,
            "D",
            60,
            "uncorrectable_198",
        )

    media_err = _num(disk, "nvme_media_errors")
    if media_err is not None and media_err > 0:
        hit(f"{int(media_err)} ошибок носителя NVMe", 45, "D", 45, "damage_present")

    spare = _num(disk, "nvme_spare_pct")
    spare_threshold = _num(disk, "nvme_spare_threshold_pct")
    if spare is not None and spare_threshold is not None and spare < spare_threshold:
        hit(
            f"резерв NVMe {spare:.0f}% ниже порога {spare_threshold:.0f}%",
            70,
            "R",
            70,
            "spare_below_threshold",
        )

    attr5 = _num(attrs, "5")
    if attr5 is not None and attr5 > 100:
        hit(f"{int(attr5)} переназначенных секторов (attr 5)", 50, "D", 50, "damage_present")
    elif attr5 is not None and attr5 > 15:
        # Backblaze attr-5 bucket 16-70: AFR 22.5 %.
        hit(f"{int(attr5)} переназначенных секторов (attr 5)", 40, "D", 40, "damage_present")
    elif attr5 is not None and attr5 > 0:
        hit(f"{int(attr5)} переназначенных секторов (attr 5)", 30, "D", 30, "damage_present")

    attr187 = _num(attrs, "187")
    if attr187 is not None and attr187 > 0:
        hit(
            f"{int(attr187)} зафиксированных неисправимых ошибок (attr 187)",
            40,
            "D",
            40,
            "damage_present",
        )

    attr188 = _num(attrs, "188")
    if attr188 is not None and attr188 > 0:
        # cmd_timeouts corroborates the tail-latency flag in health.
        hit(f"{int(attr188)} таймаутов команд (attr 188)", 20, "R", 20, "cmd_timeouts")

    uncorr_rw = _uncorrected(disk) or 0.0
    if uncorr_rw > 0:
        hit(f"{int(uncorr_rw)} неисправленных ошибок чтения/записи", 40, "D", 40, "damage_present")

    attr196 = _num(attrs, "196")
    # 196 counts the same remap events attr 5 already scores: only a fallback
    # when attr 5 is absent or zero (sim S2b scored 8 remaps twice, D55).
    if attr196 is not None and attr196 > 0 and not attr5:
        hit(f"{int(attr196)} событий переназначения (attr 196)", 25, "D", 25, "damage_present")

    # Gated on nvme_percentage_used presence (a Ф1-only field): a pre-Ф1
    # payload carries wear_pct alone and must not double-fire against the
    # legacy wear rule above (T2.4 regression pin would otherwise break).
    pct_used = _num(disk, "nvme_percentage_used")
    if pct_used is not None:
        wear_level = max(pct_used, wear) if wear is not None else pct_used
        if wear_level > 95:
            hit(f"износ носителя {wear_level:.0f}%", 40, "D", 40)
        elif wear_level > 85:
            hit(f"износ носителя {wear_level:.0f}%", 25, "D", 25)

    unsafe_shutdowns = _num(disk, "nvme_unsafe_shutdowns")
    if (
        unsafe_shutdowns is not None
        and unsafe_shutdowns > _UNSAFE_SHUTDOWNS_HIGH
        and ((media_err or 0) > 0 or (pending or 0) > 0)
    ):
        hit(f"{int(unsafe_shutdowns)} нештатных отключений при живых дефектах", 10, "R", 10)

    # Dynamics (K4): only meaningful for the disk disk_series/trends were
    # actually computed from.
    is_target_disk = bool(
        disk_series and disk_series[0].get("serial_hash") == disk.get("serial_hash")
    )
    if is_target_disk and disk_series:
        _growth_hits(disk_series, hit, hit_mult, _reinforcement_for)

    realloc_t = trends.get("smart_realloc") if trends else None
    pending_t = trends.get("smart_pending") if trends else None
    media_t = trends.get("smart_media_errors") if trends else None
    if is_target_disk and trends:
        if any(
            t is not None and getattr(t, "accelerating", False)
            for t in (pending_t, media_t, realloc_t)
        ):
            hit_mult("ускорение накопления дефектов диска", 1.4, "R", 40, "accel")
        if (
            realloc_t is not None
            and pending_t is not None
            and realloc_t.direction == "worsening"
            and pending_t.direction == "improving"
        ):
            hit(
                "диск маскирует дефекты переназначением (attr 5 растёт, 197 падает)",
                25,
                "R",
                25,
                "remap_masking",
            )

    # Synergy (closed pair list; ×1.3-1.5, R+25 once regardless of how many
    # pairs match). A second documented pair -- media errors + a GROWING
    # unsafe_shutdowns trend -- is intentionally not implemented: Ф2's
    # extractor set has no unsafe_shutdowns trend, and K8 forbids inventing
    # one just to fill this slot; add it if/when that trend exists.
    synergy_mults: list[float] = []
    if ((pending or 0) > 0 or (attr5 or 0) > 0) and (getattr(chain, "stage", 0) or 0) >= 1:
        synergy_mults.append(1.5)
    spare_t = trends.get("nvme_spare") if trends else None
    if (
        is_target_disk
        and spare_t is not None
        and spare_t.direction == "worsening"
        and pct_used is not None
        and pct_used > 85
    ):
        synergy_mults.append(1.3)
    if synergy_mults:
        hit_mult(
            "сочетание истощения компенсации", max(synergy_mults), "R", 25, "compensation_breach"
        )

    chain_stage = getattr(chain, "stage", 0) or 0
    # H6: an isolated Ntfs 55 confirms nothing on its own -- the full stage only when the
    # disk itself shows SMART damage (Ntfs 55 confirmed by SMART), else the disk's stage
    disk_only = getattr(chain, "stage_disk_only", None)
    if disk_only is not None and not (device_damaged or set(flags) & _SMART_DAMAGE_FLAGS):
        chain_stage = disk_only
    if chain_stage >= 3:
        hit("цепочка ошибок дошла до отказа (stage 3)", 25, "R", 45, "chain_stage3")
    elif chain_stage == 2:
        hit_mult(
            "цепочка ошибок: повреждение после ретраев (stage 2)", 1.25, "R", 30, "chain_stage2"
        )

    if (getattr(chain, "fs_isolated", 0) or 0) > 0 and chain_stage < 2:
        hit(
            "порча файловой системы (Ntfs 55) без подтверждения диском",
            0,
            legacy=True,
            flag="fs_corruption",
        )

    burstiness = getattr(chain, "burstiness", None)
    if burstiness is not None and burstiness > 2:
        hit("ошибки идут кластерами (не равномерно)", 10, "R", 10)

    counts = getattr(chain, "counts", None)
    if (
        isinstance(counts, dict)
        and (counts.get("early") or 0) >= _EARLY_EVENTS_MIN
        and (counts.get("damage") or 0) == 0
    ):
        m = _reinforcement_for("early_chain")
        hit("ранние сигналы (ретраи) без видимых повреждений", 0 * m, "R", 15 * m, "early_events")

    # Bathtub context (§1.2: weak on its own) -- only a tie-breaker once
    # something else in THIS (ssd3) pass already fired.
    power_cycles = _num(disk, "nvme_power_cycles")
    if other_new_factor and (
        (poh is not None and poh > 40000) or (power_cycles is not None and power_cycles > 10000)
    ):
        hit("возраст диска (контекст, не самостоятельный фактор)", 5, "D", 5)

    # Confirmation only: latency may amplify a SMART signal, never create one.
    if value > 0:
        latency = max(
            _num(heartbeat, "disk_read_sec") or 0.0, _num(heartbeat, "disk_write_sec") or 0.0
        )
        if latency >= STALL_SEC:  # H9: whole seconds -- only a stall is ever seen
            hit(f"зависание ввода-вывода ≥ 1 с подтверждает сигнал SMART ({latency:.0f} с)", 10)

    value = _clamp(_clamp(value) * (max(multipliers) if multipliers else 1.0))
    coords = {"damage": _clamp(damage), "resilience_loss": _clamp(resilience_loss), "flags": flags}
    return value, factors, coords


def worst_disk_key(historical: Optional[dict[str, Any]]) -> Optional[str]:
    """The worst disk's serial_hash by D+R points of its LATEST reading alone.

    No series/chain/trends (breaks the "trends need the engine, the engine
    needs the worst disk" cycle): the caller uses this to decide which disk's
    series to fetch *before* trends or the full engine ever run. D+R is the
    engine's own primary ranking key (compute_storage_risk), so the series --
    and the growth flags only it can raise -- belong to the disk it reports;
    ranking by D alone lost them for an NVMe whose spare is below threshold
    next to a disk with more static damage. The first disk wins ties.
    """
    disks = (historical or {}).get("storage") or []
    best_key: Optional[str] = None
    best_rank: Optional[float] = None
    for disk in disks:
        if not isinstance(disk, dict) or not disk.get("serial_hash"):
            continue
        _, _, coords = _score_disk(disk, None)
        rank = coords["damage"] + coords["resilience_loss"]
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_key = disk.get("serial_hash")
    return best_key


def _confidence(coords: dict[str, Any]) -> ScoreConfidence:
    """High confidence requires the worst disk's own rules to carry no
    unresolved stability concern -- currently just the temperature rule
    (P2-13): a single transient spike must not claim the same confidence as
    a genuinely sustained problem (mirrors disk_fill.py's sample-count gate)."""
    if "temp_unstable" in coords.get("flags", []):
        return "medium"
    return "high"


def _disk_id(disk: dict[str, Any]) -> Optional[str]:
    """A disk's identity: serial_hash (Ф1+), else its FriendlyName (a model name)."""
    value = disk.get("serial_hash") or disk.get("disk")
    return value if isinstance(value, str) else None


def _has_attr_log(disk: dict[str, Any]) -> bool:
    """SMART attributes or an NVMe health log present -- what a SMART script
    timeout drops while the disk stays listed. smart_predict_fail does not
    count: it comes from another WMI class."""
    attrs = disk.get("smart_attrs")
    return (
        (isinstance(attrs, dict) and bool(attrs))
        or disk.get("nvme_media_errors") is not None
        or disk.get("nvme_spare_pct") is not None
    )


# H6: the disk's own SMART damage -- with it, an Ntfs 55 counts at its full stage
_SMART_DAMAGE_FLAGS = frozenset({"damage_present", "predict_fail", "cw_readonly", *_HARD_MEDIA})


def _has_health_data(disk: dict[str, Any]) -> bool:
    """Data about the disk's HEALTH (spec H5): SMART attributes / an NVMe log, the
    uncorrected read/write pair (the only damage counter without ATA or an NVMe log), or
    a predict-fail verdict. Wear, temperature and flush alone are no health data."""
    return (
        _has_attr_log(disk)
        or _uncorrected(disk) is not None
        or disk.get("smart_predict_fail") is not None
    )


def _lineage(
    disks: list[Any], scored: list[dict[str, Any]], worst: dict[str, Any], coords: dict[str, Any]
) -> dict[str, Any]:
    """The axis lineage with the D3(г) ids for health: a replacement is the worst id
    gone plus a new id; attributes gone from a still-listed disk is a SMART timeout,
    not recovery. health_disk_ids (H5): disks with real health data -- none = phantom."""
    names = (disk.get(key) for disk in scored for key in ("serial_hash", "disk"))
    with_attrs = (_disk_id(disk) for disk in scored if _has_attr_log(disk))
    with_health = (_disk_id(disk) for disk in scored if _has_health_data(disk))
    return {
        "worst_disk": worst.get("disk"),
        "worst_disk_id": _disk_id(worst),
        "disk_ids": list(dict.fromkeys(i for i in names if isinstance(i, str) and i)),
        "attr_disk_ids": list(dict.fromkeys(i for i in with_attrs if i)),
        "health_disk_ids": list(dict.fromkeys(i for i in with_health if i)),
        "disks_with_smart": len(scored),
        "disks_total": len(disks),
        "coords": coords,
    }


def compute_storage_risk(
    historical: Optional[dict[str, Any]],
    heartbeat: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
    disk_series: Optional[list[dict[str, Any]]] = None,
    chain: Optional[Any] = None,
    trends: Optional[dict[str, Any]] = None,
    rule_stats: Optional[dict[str, dict[str, int]]] = None,
    domain_state: Optional[str] = None,
) -> Score100:
    """Deterministic storage-failure risk for one device, worst disk wins.

    Higher = a drive is closer to failure. Gating mirrors W0.5/W4.1: untrusted
    identity withholds entirely; no SMART data on any disk -> UNKNOWN (never a
    confident zero -- a blocked StorageReliability source must not read healthy).

    ``disk_series``/``chain``/``trends`` are optional (ssd3 Ф2+): omitting them
    reproduces the pre-Ф2 value exactly for a pre-Ф1 payload (T2.4 pin).
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
            reason="идентификатор устройства не подтверждён (контракт §7)",
        )

    disks = (historical or {}).get("storage") or []
    worst_value: Optional[float] = None
    worst_rank: Optional[tuple[float, float]] = None
    worst_factors: list[Factor] = []
    worst: dict[str, Any] = {}
    worst_coords: dict[str, Any] = {"damage": 0.0, "resilience_loss": 0.0, "flags": []}
    scored: list[dict[str, Any]] = []
    rows = [d for d in disks if isinstance(d, dict) and _has_smart(d)]
    # H6: an Ntfs 55 names no disk -- SMART damage on ANY scored disk confirms it for all,
    # so a damage-free sibling that outranks the damaged disk cannot take it down to h2
    damaged = bool(getattr(chain, "fs_isolated", 0)) and any(
        _SMART_DAMAGE_FLAGS & set(_score_disk(d, None)[2]["flags"]) for d in rows
    )

    for disk in rows:
        scored.append(disk)
        value, factors, coords = _score_disk(
            disk,
            heartbeat,
            disk_series=disk_series,
            chain=chain,
            trends=trends,
            rule_stats=rule_stats,
            device_damaged=damaged,
        )
        # D8: D+R first -- legacy-only points carry no coordinate, so a sibling's
        # corrected-error totals or heat must not mask a damaged disk; the value
        # breaks ties and the first disk stays on a full tie.
        rank = (coords["damage"] + coords["resilience_loss"], value)
        if worst_rank is None or rank > worst_rank:
            worst_rank = rank
            worst_value = value
            worst_factors = factors
            worst = disk
            worst_coords = coords

    if worst_value is None:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["нет данных SMART / StorageReliability ни для одного диска"],
            reason="нет телеметрии SMART хранилища (UNKNOWN — ложная уверенность недопустима)",
        )

    if domain_state == "unknown":
        # o5-E1, тот же гейт, что в compute_network_risk. Стоит ПОСЛЕ ветки «нет
        # SMART», чтобы старый агент читал «нет телеметрии», а не «провален гейт».
        # Осознанно: вместе с оценкой теряются и событийные флаги цепочки отказов
        # (chain), они лежат в coords этой оси. Здоровье при этом не выдаёт «здоров»
        # -- при отсутствующей координате ущерба вердикт уходит в «нет видимости».
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["источник storage не прошёл проверку доверия"],
            source_lineage={"storage_domain": "unknown"},
            reason="источник хранилища не прошёл гейт доверия — оценка скрыта",
        )

    return make_score100(
        worst_value,
        direction,
        band_for_risk_score(worst_value),
        _confidence(worst_coords),
        factors=worst_factors,
        source_lineage=_lineage(disks, scored, worst, worst_coords),
        # S2: «в норме» needs health data (H5) -- wear/temperature/flush alone prove nothing
        reason=(
            "SMART в норме на всех отчитывающихся дисках"
            if worst_value <= 0 and any(map(_has_health_data, scored))
            else ""
        ),
    )
