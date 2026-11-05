"""W4.2 OS-degradation engine: a deterministic current-state verdict on OS health.

The 4th of the independent domain engines (cctodo §4 W4.2). It judges current OS
stability from the three Windows signals we can measure:

  * **RSI (Reliability Stability Index) is the leading signal.** Windows maintains
    Win32_ReliabilityStabilityMetrics internally; it synthesises system crashes, OS
    failures, app failures, and warnings into a 1.0–10.0 score (lower is worse).
    A low RSI is the clearest single proxy for «this OS is degraded right now».
  * **System crash counts are the direct confirmation.** ``bugchecks_30d`` (BSODs)
    are the strongest evidence — a BSOD is a hard system crash regardless of cause.
    ``dirty_shutdowns_30d`` (event 6008, unexpected shutdown) add moderate weight.
    Per cctodo D6, KP41 specificity ≈ 0 (power/transient noise, not OS failure), so
    it is excluded here. RSI already contains the dip a BSOD causes, so a known BSOD
    count takes that dip (10) off the RSI contribution: one crash is not counted twice.
  * **Boot-rot (avg_boot_ms) is an independent degradation signal.** A boot at or
    above the cohort-relative threshold T (scores.boot_threshold_ms, H15: max(180 s,
    1.5 x the peers' boot p90), the 180 s floor without five peers) is slow (+5), at
    2T very slow (+12) -- accumulated cruft or a failing component. The boot-time
    *slope* is the trajectory engine's job (W4.1); this engine reads the current level.

Hard limits that shape the engine:
  * App crashes are application-level (not OS-level); they appear in source_lineage
    and never add risk. When ≥ 3 of them are the only crashes on record (BSOD and
    WHEA known zero, dirty shutdowns known < 2) they explain a low RSI, which is then
    capped at 10 — RSI from app crashes alone is not OS degradation (D11).
  * Pending restart state is not collected. A machine awaiting a driver or security
    patch reboot is in a degraded state we cannot detect. Permanently disclosed as a
    blind spot in ``missing_evidence``.
  * Confidence caps at *medium*: RSI is an opaque Microsoft heuristic; crash counts
    cannot distinguish hardware fault, driver regression, or OS corruption.

Output is the ``os_degradation_risk`` axis in the W0.5 Score100 envelope
(higher = worse), with the standard gating: untrusted identity withholds; no RSI
and no crash counts → UNKNOWN (never a confident zero from silence).
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
from server.scoring.scores import BOOT_FLOOR_MS

# RSI bands → risk contribution (RSI 1.0–10.0, lower is worse).
# Each constant is the exclusive upper bound of its band (strict <).
# RSI < 2.0 → critical (+60); [2.0, 3.5) → severe (+40); [3.5, 5.0) → high (+22);
# [5.0, 7.0) → watch (+10); >= 7.0 → stable (+0).
_RSI_CRITICAL = 2.0
_RSI_SEVERE = 3.5
_RSI_HIGH = 5.0
_RSI_WATCH = 7.0

# Crash/shutdown thresholds and risk deltas.
_BSOD_SEVERE = 3  # >= this many BSODs -> +30
_BSOD_ONE = 1  # one BSOD -> +15
_DIRTY_MANY = 5  # dirty shutdowns -> +12
_DIRTY_SOME = 2  # -> +6

# D11: RSI already reflects the crashes counted above. A BSOD dips RSI by itself and
# is scored by its own +15/+30, so a known BSOD count takes this off the RSI delta.
_RSI_ONE_CRASH_DIP = 10.0
# RSI lowered by application crashes alone is not OS degradation: the RSI delta is
# capped here only when BSOD and WHEA are known zero, dirty shutdowns are known below
# _DIRTY_SOME and at least _APP_CRASHES_MIN app crashes explain the dip.
_RSI_APP_ONLY_CAP = 10.0
_APP_CRASHES_MIN = 3


_UNTRUSTED_REASON = "идентификатор устройства не подтверждён (контракт §7)"
_PENDING_REBOOT_BLIND_SPOT = (
    "статус ожидающей перезагрузки не собирается — машина, ожидающая перезагрузки "
    "для применения обновлений безопасности или driver-обновлений, находится в "
    "деградированном состоянии, которое здесь не обнаруживается"
)
_CAUSE_BLIND_SPOT = (
    "счётчики сбоев не позволяют различить аппаратную неисправность, регрессию driver "
    "или повреждение OS — определение причины требует анализа каждого события"
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


def _rsi_contribution(rsi: float) -> tuple[float, Optional[Factor]]:
    """Risk delta and factor from the Reliability Stability Index."""
    if rsi < _RSI_CRITICAL:
        delta = 60.0
        label = f"RSI {rsi:.1f}/10 — OS крайне нестабильна (критично)"
    elif rsi < _RSI_SEVERE:
        delta = 40.0
        label = f"RSI {rsi:.1f}/10 — OS серьёзно деградировала"
    elif rsi < _RSI_HIGH:
        delta = 22.0
        label = f"RSI {rsi:.1f}/10 — OS заметно деградировала"
    elif rsi < _RSI_WATCH:
        delta = 10.0
        label = f"RSI {rsi:.1f}/10 — лёгкая нестабильность OS"
    else:
        return 0.0, None
    return delta, {"label": label, "delta": delta}


def _app_crashes_only(
    bugchecks: Optional[float],
    dirty: Optional[float],
    whea: Optional[float],
    app_crashes: Optional[float],
) -> bool:
    """App crashes alone explain a low RSI: every system crash counter is known clean.

    Deliberately narrow (FN critic): one or two app crashes exist on almost every PC,
    so they must not cap a hardware-caused RSI dip. An unknown counter (None) never
    qualifies -- UNKNOWN over false confidence keeps the RSI-led value.
    """
    # Positive conditions only: None and NaN fail every one of them.
    return (
        bugchecks == 0
        and whea == 0
        and dirty is not None
        and dirty < _DIRTY_SOME
        and app_crashes is not None
        and app_crashes >= _APP_CRASHES_MIN
    )


def _rsi_adjusted(
    rsi: float,
    bugchecks: Optional[float],
    dirty: Optional[float],
    whea: Optional[float],
    app_crashes: Optional[float],
) -> tuple[float, Optional[Factor]]:
    """RSI contribution net of what the crash counters already explain (D11).

    A known BSOD count takes the BSOD's own RSI dip off (one crash is not counted
    twice); RSI lowered by app crashes alone is capped and labelled as such.
    """
    delta, factor = _rsi_contribution(rsi)
    if factor is None:
        return 0.0, None
    label = factor["label"]
    if bugchecks is not None and bugchecks >= _BSOD_ONE:
        delta = max(0.0, delta - _RSI_ONE_CRASH_DIP)
    elif _app_crashes_only(bugchecks, dirty, whea, app_crashes):
        delta = min(delta, _RSI_APP_ONLY_CAP)
        label = f"RSI {rsi:.1f}/10 — снижен сбоями приложений (системных сбоев нет)"
    if delta == 0.0:
        return 0.0, None  # the BSOD factor alone explains this RSI dip
    return delta, {"label": label, "delta": delta}


def _crash_contribution(
    bugchecks: Optional[float],
    dirty: Optional[float],
) -> tuple[float, list[Factor]]:
    """Risk delta and factors from system crash and unexpected-shutdown counts."""
    value = 0.0
    factors: list[Factor] = []

    if bugchecks is not None:
        count = int(bugchecks)
        if count >= _BSOD_SEVERE:
            delta = 30.0
            factors.append(
                {
                    "label": f"{count} BSOD за 30 дней — серьёзная нестабильность системы",
                    "delta": delta,
                }
            )
            value += delta
        elif count >= _BSOD_ONE:
            delta = 15.0
            factors.append({"label": f"{count} BSOD за 30 дней", "delta": delta})
            value += delta

    if dirty is not None:
        count = int(dirty)
        if count >= _DIRTY_MANY:
            delta = 12.0
            factors.append(
                {
                    "label": (
                        f"{count} аварийных выключений за 30 дней — частые внезапные перезагрузки"
                    ),
                    "delta": delta,
                }
            )
            value += delta
        elif count >= _DIRTY_SOME:
            delta = 6.0
            factors.append({"label": f"{count} аварийных выключений за 30 дней", "delta": delta})
            value += delta

    return value, factors


def _boot_contribution(
    avg_boot_ms: Optional[float], threshold: float
) -> tuple[float, Optional[Factor]]:
    """Risk delta from the current average boot time: +5 at T, +12 at 2T (H15, the
    cohort-relative scores.boot_threshold_ms)."""
    if avg_boot_ms is None:
        return 0.0, None
    if avg_boot_ms >= 2 * threshold:
        delta = 12.0
        label = (
            f"средняя загрузка {avg_boot_ms / 1000:.0f} с — очень медленно "
            "(деградация OS или отказывающий компонент)"
        )
        return delta, {"label": label, "delta": delta}
    if avg_boot_ms >= threshold:
        delta = 5.0
        return delta, {
            "label": f"средняя загрузка {avg_boot_ms / 1000:.0f} с — медленно",
            "delta": delta,
        }
    return 0.0, None


def compute_os_degradation_risk(
    historical: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
    domain_state: Optional[str] = None,
    boot_threshold: float = BOOT_FLOOR_MS,
) -> Score100:
    """Deterministic OS-degradation risk for one device (current-state verdict).

    Higher = more degraded. RSI is the leading signal; crash counts confirm; boot-rot
    is independent. Gating mirrors W0.5/W4.1: untrusted withholds; no RSI and no
    crash counts → UNKNOWN (never a confident zero from absence of evidence).
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

    rsi = _num(historical, "reliability_stability_index")
    bugchecks = _num(historical, "bugchecks_30d")
    dirty = _num(historical, "dirty_shutdowns_30d")
    avg_boot = _num(historical, "avg_boot_ms")
    app_crashes = _num(historical, "app_crashes_30d")
    whea = _num(historical, "whea_errors_30d")

    # Without RSI or crash counts we have no OS stability signal.
    if rsi is None and bugchecks is None and dirty is None:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["нет телеметрии RSI и счётчиков сбоев"],
            reason="нет телеметрии стабильности OS (UNKNOWN — ложная уверенность недопустима)",
        )

    if domain_state == "unknown":
        # o5-E1, тот же гейт, что в compute_network_risk. Стоит ПОСЛЕ ветки «нет
        # данных», чтобы старый агент читал «нет телеметрии», а не «провален гейт».
        # Осознанно: вместе с оценкой теряется и вклад boot-rot (avg_boot_ms
        # принадлежит домену boot) -- ось называется os_degradation и ведётся RSI,
        # разделять её вклад по доменам здесь дороже, чем польза.
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["источник os_stability не прошёл проверку доверия"],
            source_lineage={"os_stability_domain": "unknown"},
            reason="источник стабильности ОС не прошёл гейт доверия — оценка скрыта",
        )

    factors: list[Factor] = []
    value = 0.0

    if rsi is not None:
        rsi_delta, rsi_factor = _rsi_adjusted(rsi, bugchecks, dirty, whea, app_crashes)
        value += rsi_delta
        if rsi_factor is not None:
            factors.append(rsi_factor)

    crash_delta, crash_factors = _crash_contribution(bugchecks, dirty)
    value += crash_delta
    factors.extend(crash_factors)

    boot_delta, boot_factor = _boot_contribution(avg_boot, boot_threshold)
    value += boot_delta
    if boot_factor is not None:
        factors.append(boot_factor)

    # RSI is an opaque heuristic; crash counts are ambiguous on cause → cap at medium.
    # Two independent evidence streams → medium; single stream → low.
    has_rsi = rsi is not None
    has_crash = bugchecks is not None or dirty is not None
    confidence: ScoreConfidence = "medium" if (has_rsi and has_crash) else "low"

    missing = [_PENDING_REBOOT_BLIND_SPOT, _CAUSE_BLIND_SPOT]
    if rsi is None:
        missing.append("RSI недоступен — индекс стабильности OS не считывается")
    if bugchecks is None:
        missing.append("счётчик BSOD недоступен (требуется доступ к журналу событий)")
    if avg_boot is None:
        missing.append("время загрузки не наблюдалось")

    reason = ""
    if value == 0.0:
        if rsi is not None and rsi >= _RSI_WATCH:
            reason = f"RSI {rsi:.1f}/10 — OS стабильна; сигналов сбоев не обнаружено"
        else:
            reason = "сигналов деградации OS не обнаружено"

    return make_score100(
        _clamp(value),
        direction,
        band_for_risk_score(_clamp(value)),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage={
            "rsi": round(rsi, 1) if rsi is not None else None,
            "bugchecks_30d": int(bugchecks) if bugchecks is not None else None,
            "dirty_shutdowns_30d": int(dirty) if dirty is not None else None,
            "avg_boot_ms": int(avg_boot) if avg_boot is not None else None,
            "app_crashes_30d": int(app_crashes) if app_crashes is not None else None,
        },
        reason=reason,
    )
