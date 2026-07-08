"""W4.2 storage health engine (ssd3 Ф2: v3, coordinate-tagged).

The original ``storage_risk`` axis (a single 0-100 "how bad" number) is kept
byte-for-byte for old-style payloads/callers -- the five original rules below
are untouched, and every ssd3 Ф2 rule keys exclusively off fields Ф1 (deep
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
from typing import Any, Optional

from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    band_for_risk_score,
    make_score100,
)

# Latency this high (seconds/op) *confirms* an existing SMART problem; on its own
# it means nothing here (see module docstring). Matches scores.py's "high" band.
_LATENCY_HIGH_SEC = 0.05

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
_RECURRENCE_MIN_GAP = timedelta(days=7)


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


def _has_recurrence(disk_series: list[dict[str, Any]]) -> bool:
    """media/attr197/attr5 grew between two readings >=7 days apart, within
    one disk's own series -- the same failure mode resurfacing in a
    different week (§1.2: Facebook-fleet SSD recurrence ~99.8%) is a
    stronger signal than any single count.
    """

    def _attrs_num(row: dict[str, Any], attr_id: str) -> Optional[float]:
        attrs = row.get("smart_attrs")
        return _num(attrs if isinstance(attrs, dict) else None, attr_id)

    points: list[tuple[datetime, dict[str, Any]]] = []
    for row in disk_series:
        when = _parse_iso(row.get("received_at") or row.get("ts"))
        if when is not None:
            points.append((when, row))
    points.sort(key=lambda p: p[0])

    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if (points[j][0] - points[i][0]) < _RECURRENCE_MIN_GAP:
                continue
            older, newer = points[i][1], points[j][1]
            pairs = (
                (_num(older, "nvme_media_errors"), _num(newer, "nvme_media_errors")),
                (_attrs_num(older, "197"), _attrs_num(newer, "197")),
                (_attrs_num(older, "5"), _attrs_num(newer, "5")),
            )
            if any(a is not None and b is not None and b > a for a, b in pairs):
                return True
    return False


def _score_disk(
    disk: dict,
    heartbeat: Optional[dict],
    *,
    disk_series: Optional[list[dict[str, Any]]] = None,
    chain: Optional[Any] = None,
    trends: Optional[dict[str, Any]] = None,
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

    # ----- legacy rules (byte-for-byte unchanged; T2.4 regression pin) -----
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
    if io_errors > 0:
        hit(
            f"{int(io_errors)} накопленных I/O ошибок",
            60 if io_errors > 100 else 40,
            legacy=True,
        )

    wear = _num(disk, "wear_pct")
    if wear is not None:
        if wear > 95:
            hit(f"износ SSD {wear:.0f}% (конец ресурса)", 40, legacy=True)
        elif wear > 85:
            hit(f"износ SSD {wear:.0f}%", 25, legacy=True)
        elif wear > 70:
            hit(f"износ SSD {wear:.0f}%", 12, legacy=True)

    temp = _num(disk, "temperature_c")
    if temp is not None:
        if temp > 70:
            hit(f"диск {int(temp)}°C (тепловой стресс)", 15, legacy=True)
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
        hit(f"{int(pending)} секторов в ожидании переназначения", 60, "D", 60, "pending_gt10")
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
    elif attr5 is not None and attr5 > 0:
        hit(f"{int(attr5)} переназначенных секторов (attr 5)", 30, "D", 30, "damage_present")

    attr187 = _num(attrs, "187")
    if attr187 is not None and attr187 > 0:
        hit(
            f"{int(attr187)} зафиксированных неисправимых ошибок (attr 187)",
            35,
            "D",
            35,
            "damage_present",
        )

    attr188 = _num(attrs, "188")
    if attr188 is not None and attr188 > 0:
        hit(f"{int(attr188)} таймаутов команд (attr 188)", 20, "R", 20)

    uncorr_rw = (_num(disk, "read_errors_uncorrected") or 0.0) + (
        _num(disk, "write_errors_uncorrected") or 0.0
    )
    if uncorr_rw > 0:
        hit(f"{int(uncorr_rw)} неисправленных ошибок чтения/записи", 40, "D", 40, "damage_present")

    attr196 = _num(attrs, "196")
    if attr196 is not None and attr196 > 0:
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
    if is_target_disk and disk_series and _has_recurrence(disk_series):
        hit_mult("рецидив дефектов диска (промежуток ≥7 дней)", 1.3, "R", 30, "recurrence")

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
    if chain_stage >= 3:
        hit("цепочка ошибок дошла до отказа (stage 3)", 25, "R", 45, "chain_stage3")
    elif chain_stage == 2:
        hit_mult(
            "цепочка ошибок: повреждение после ретраев (stage 2)", 1.25, "R", 30, "chain_stage2"
        )

    burstiness = getattr(chain, "burstiness", None)
    if burstiness is not None and burstiness > 2:
        hit("ошибки идут кластерами (не равномерно)", 10, "R", 10)

    counts = getattr(chain, "counts", None)
    if (
        isinstance(counts, dict)
        and (counts.get("early") or 0) > 0
        and (counts.get("damage") or 0) == 0
    ):
        hit("ранние сигналы (ретраи) без видимых повреждений", 0, "R", 15, "early_events")

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
        if latency > _LATENCY_HIGH_SEC:
            hit(f"высокая задержка диска подтверждает сигнал SMART ({latency * 1000:.0f} мс)", 10)

    value = _clamp(_clamp(value) * (max(multipliers) if multipliers else 1.0))
    coords = {"damage": _clamp(damage), "resilience_loss": _clamp(resilience_loss), "flags": flags}
    return value, factors, coords


def worst_disk_key(historical: Optional[dict[str, Any]]) -> Optional[str]:
    """The worst disk's serial_hash by D-points of its LATEST reading alone.

    No series/chain/trends (breaks the "trends need the engine, the engine
    needs the worst disk" cycle): the caller uses this to decide which disk's
    series to fetch *before* trends or the full engine ever run.
    """
    disks = (historical or {}).get("storage") or []
    best_key: Optional[str] = None
    best_damage: Optional[float] = None
    for disk in disks:
        if not isinstance(disk, dict) or not disk.get("serial_hash"):
            continue
        _, _, coords = _score_disk(disk, None)
        damage = coords["damage"]
        if best_damage is None or damage > best_damage:
            best_damage = damage
            best_key = disk.get("serial_hash")
    return best_key


def compute_storage_risk(
    historical: Optional[dict[str, Any]],
    heartbeat: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
    disk_series: Optional[list[dict[str, Any]]] = None,
    chain: Optional[Any] = None,
    trends: Optional[dict[str, Any]] = None,
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
    worst_factors: list[Factor] = []
    worst_disk: Optional[str] = None
    worst_coords: dict[str, Any] = {"damage": 0.0, "resilience_loss": 0.0, "flags": []}
    smart_disks = 0

    for disk in disks:
        if not isinstance(disk, dict) or not _has_smart(disk):
            continue
        smart_disks += 1
        value, factors, coords = _score_disk(
            disk, heartbeat, disk_series=disk_series, chain=chain, trends=trends
        )
        if worst_value is None or value > worst_value:
            worst_value = value
            worst_factors = factors
            worst_disk = disk.get("disk")
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

    return make_score100(
        worst_value,
        direction,
        band_for_risk_score(worst_value),
        "high",
        factors=worst_factors,
        source_lineage={
            "worst_disk": worst_disk,
            "disks_with_smart": smart_disks,
            "disks_total": len(disks),
            "coords": worst_coords,
        },
        reason="" if worst_value > 0 else "SMART в норме на всех отчитывающихся дисках",
    )
