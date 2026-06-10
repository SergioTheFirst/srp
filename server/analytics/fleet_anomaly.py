"""W4.2 fleet-anomaly engine: detect coordinated fleet events vs device-specific faults.

The 5th (final) independent domain engine (cctodo §4 W4.2). Its purpose is noise
reduction: surface fleet-wide patterns so operators investigate the right scope (a
bad Windows Update affecting 30 machines vs a single failing drive).

Two fleet-level signals:

  * **Bad patch / driver rollout (cohort BSOD rate):** When ≥30% of devices sharing
    the same model report BSODs in 30 days, simultaneous hardware failure is far less
    likely than a fleet-wide software event — a Windows Update, driver rollout, or
    firmware change.  RSI degradation across the cohort is a weaker corroborating
    signal (OS instability can also precede a patch).

  * **Site-wide KP41 cluster (site KP41 rate):** kernel_power_41_30d events cluster
    on devices at the same site because they share the same electrical infrastructure.
    When ≥40% of devices at a site show elevated KP41 counts, the characteristic
    pattern is a building power blip (UPS trip, generator test, transient brownout)
    rather than N concurrent PSU failures.  Per cctodo D6 KP41 specificity is near
    zero for INDIVIDUAL device attribution, but a SITE CLUSTER is a reliable signal.

Cohort key: model (from devices table, stored on every inventory ingest).
Site key: site_code (from devices table).
Minimum cohort for a verdict: 2 devices. Smaller → UNKNOWN.
Confidence caps at medium: statistical power is limited at small fleet sizes, and the
cohort may be heterogeneous (same model, different age/config) even when large.

Hard limits:
  * os_build is NOT used as part of the cohort key (it is not a direct devices-table
    column — it would require JSON extraction from inventory payloads).  This widens
    the cohort slightly but keeps the query simple.  Disclosed in missing_evidence.
  * site_code absent → site cluster cannot be evaluated; scored on cohort only.
  * device_trust="untrusted" → withheld (contract §7).
"""

from __future__ import annotations

from typing import Any, Optional

from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

# --- thresholds: cohort BSOD rate ---
_BSOD_HIGH = 0.40  # ≥40% of cohort with ≥1 BSOD → +35
_BSOD_MOD = 0.25  # ≥25%                          → +20
_BSOD_LOW = 0.10  # ≥10%                          → +8

# --- thresholds: cohort RSI degradation (RSI < 5.0 = "low") ---
_RSI_HIGH = 0.40  # ≥40% of cohort with RSI low   → +20
_RSI_MOD = 0.25  # ≥25%                          → +10

# --- thresholds: site KP41 cluster ---
_KP41_HIGH = 0.50  # ≥50% of site → strong cluster → +40
_KP41_MOD = 0.30  # ≥30% of site → moderate       → +20
_KP41_NOISE = 0.20  # < 20% → noise, add nothing

# minimum number of devices in site before cluster is meaningful
_MIN_SITE_FOR_CLUSTER = 3

# minimum cohort size to produce any verdict
_MIN_COHORT = 2

# cohort size at which confidence is promoted from low to medium
_COHORT_MEDIUM = 5

_UNTRUSTED_REASON = "идентификатор устройства не подтверждён (контракт §7)"
_BLIND_SPOT_BUILD = (
    "os_build не используется как ключ когорты — когорта только по модели; устройства с разными "
    "сборками OS, но одной моделью группируются вместе (может расширять или размывать паттерны)"
)
_BLIND_SPOT_CAUSE = (
    "флотовый паттерн основан на корреляции — корреляция показателей сбоев/RSI в когорте модели "
    "не доказывает общую первопричину; требуется ручной анализ логов patch/driver"
)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _site_kp41_contribution(
    site_kp41_pct: float,
    site_size: int,
) -> tuple[float, Optional[Factor]]:
    """Risk delta from a site-wide KP41 cluster."""
    if site_size < _MIN_SITE_FOR_CLUSTER:
        return 0.0, None
    if site_kp41_pct >= _KP41_HIGH:
        delta = 40.0
        label = (
            f"{site_kp41_pct:.0%} устройств площадки имеют повышенный KP41 "
            f"({site_size} устр.) — кластер питания площадки, не индивидуальные сбои ПК"
        )
        return delta, {"label": label, "delta": delta}
    if site_kp41_pct >= _KP41_MOD:
        delta = 20.0
        label = (
            f"{site_kp41_pct:.0%} устройств площадки имеют повышенный KP41 "
            f"({site_size} устр.) — возможное событие электропитания здания"
        )
        return delta, {"label": label, "delta": delta}
    return 0.0, None


def _cohort_bsod_contribution(
    bsod_pct: float,
    cohort_size: int,
) -> tuple[float, Optional[Factor]]:
    """Risk delta from elevated BSOD rate across the model cohort."""
    if bsod_pct >= _BSOD_HIGH:
        delta = 35.0
        label = (
            f"{bsod_pct:.0%} устройств когорты модели ({cohort_size}) имеют BSOD — "
            f"вероятно плохой driver или обновление OS, не одновременные аппаратные сбои"
        )
        return delta, {"label": label, "delta": delta}
    if bsod_pct >= _BSOD_MOD:
        delta = 20.0
        label = (
            f"{bsod_pct:.0%} устройств когорты модели ({cohort_size}) имеют BSOD — "
            f"повышенный уровень сбоев флота (возможная проблема patch/driver)"
        )
        return delta, {"label": label, "delta": delta}
    if bsod_pct >= _BSOD_LOW:
        delta = 8.0
        label = (
            f"{bsod_pct:.0%} устройств когорты модели ({cohort_size}) имеют BSOD — "
            f"незначительное повышение (наблюдение)"
        )
        return delta, {"label": label, "delta": delta}
    return 0.0, None


def _cohort_rsi_contribution(
    rsi_low_pct: float,
    cohort_size: int,
) -> tuple[float, Optional[Factor]]:
    """Risk delta from widespread RSI degradation across the model cohort."""
    if rsi_low_pct >= _RSI_HIGH:
        delta = 20.0
        label = (
            f"{rsi_low_pct:.0%} устройств когорты ({cohort_size}) имеют RSI < 5.0 — "
            f"паттерн деградации OS по всему флоту"
        )
        return delta, {"label": label, "delta": delta}
    if rsi_low_pct >= _RSI_MOD:
        delta = 10.0
        label = (
            f"{rsi_low_pct:.0%} устройств когорты ({cohort_size}) имеют низкий RSI — "
            f"повышенная нестабильность когорты"
        )
        return delta, {"label": label, "delta": delta}
    return 0.0, None


def compute_fleet_anomaly_risk(
    cohort_stats: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
) -> Score100:
    """Fleet-event detection for one device.

    Detects coordinated fleet patterns (bad patch, driver rollout, site power cluster)
    that would otherwise appear as individual hardware failures.  Higher value means
    more evidence of a fleet-wide event that requires fleet-scope investigation.

    Args:
        cohort_stats: Aggregated fleet stats from db.get_fleet_cohort_stats().
                      None → UNKNOWN.  cohort_size < 2 → UNKNOWN.
        device_trust: 'untrusted' withholds all values (contract §7).
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

    if cohort_stats is None:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["данные когорты флота недоступны"],
            reason="нет данных флота (UNKNOWN — ложная уверенность недопустима)",
        )

    cohort_size = int(cohort_stats.get("cohort_size") or 0)
    if cohort_size < _MIN_COHORT:
        if cohort_size == 0:
            _reason = (
                "модель неизвестна или нет исторических данных — когорта не может быть сформирована"
            )
            _missing = [
                "модель отсутствует в таблице устройств — ключ когорты недоступен",
                _BLIND_SPOT_BUILD,
            ]
        else:
            _device_word = "устройство" if cohort_size == 1 else "устройств"
            _reason = (
                f"только {cohort_size} {_device_word} с этой моделью "
                f"— нужно ≥{_MIN_COHORT} для сравнения"
            )
            _missing = [
                f"размер когорты {cohort_size} — нужно ≥{_MIN_COHORT} для сравнения по флоту",
                _BLIND_SPOT_BUILD,
            ]
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=_missing,
            source_lineage={"cohort_size": cohort_size},
            reason=_reason,
        )

    bsod_pct = float(cohort_stats.get("cohort_bsod_pct") or 0.0)
    kp41_pct = float(
        cohort_stats.get("cohort_kp41_pct") or 0.0
    )  # lineage only — not scored (low specificity without site context)
    rsi_low_pct = float(cohort_stats.get("cohort_rsi_low_pct") or 0.0)
    site_size = int(cohort_stats.get("site_size") or 0)
    site_kp41_pct = float(cohort_stats.get("site_kp41_pct") or 0.0)

    factors: list[Factor] = []
    value = 0.0

    site_delta, site_factor = _site_kp41_contribution(site_kp41_pct, site_size)
    value += site_delta
    if site_factor is not None:
        factors.append(site_factor)

    bsod_delta, bsod_factor = _cohort_bsod_contribution(bsod_pct, cohort_size)
    value += bsod_delta
    if bsod_factor is not None:
        factors.append(bsod_factor)

    rsi_delta, rsi_factor = _cohort_rsi_contribution(rsi_low_pct, cohort_size)
    value += rsi_delta
    if rsi_factor is not None:
        factors.append(rsi_factor)

    confidence: ScoreConfidence = "medium" if cohort_size >= _COHORT_MEDIUM else "low"

    missing = [_BLIND_SPOT_BUILD, _BLIND_SPOT_CAUSE]

    if value == 0.0:
        reason = (
            f"флотовые паттерны не обнаружены — когорта из {cohort_size} устройств показывает "
            f"нормальный уровень сбоев и стабильную OS; KP41 площадки ниже порога"
        )
    else:
        reason = "флотовые паттерны обнаружены — см. факторы выше"

    return make_score100(
        _clamp(value),
        direction,
        band_for_risk_score(_clamp(value)),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage={
            "cohort_size": cohort_size,
            "cohort_bsod_pct": round(bsod_pct, 3),
            "cohort_kp41_pct": round(kp41_pct, 3),
            "cohort_rsi_low_pct": round(rsi_low_pct, 3),
            "site_size": site_size,
            "site_kp41_pct": round(site_kp41_pct, 3),
        },
        reason=reason,
    )
