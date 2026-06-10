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
    it is excluded here.
  * **Boot-rot (avg_boot_ms) is an independent degradation signal.** A machine
    that takes 2+ minutes to boot has accumulated cruft or has a failing component.
    The boot-time *slope* is the trajectory engine's job (W4.1); this engine reads
    the current-state level.

Hard limits that shape the engine:
  * App crashes are application-level (not OS-level); they appear in source_lineage
    but do not drive the risk score — excluding them keeps the signal clean.
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

# Boot-time thresholds (milliseconds).
_BOOT_SLOW = 60_000  # 60 s -> +5
_BOOT_VERY_SLOW = 120_000  # 2 min -> +12

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


def _boot_contribution(avg_boot_ms: Optional[float]) -> tuple[float, Optional[Factor]]:
    """Risk delta from the current average boot time."""
    if avg_boot_ms is None:
        return 0.0, None
    if avg_boot_ms >= _BOOT_VERY_SLOW:
        delta = 12.0
        label = (
            f"средняя загрузка {avg_boot_ms / 1000:.0f} с — очень медленно "
            "(деградация OS или отказывающий компонент)"
        )
        return delta, {"label": label, "delta": delta}
    if avg_boot_ms >= _BOOT_SLOW:
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

    factors: list[Factor] = []
    value = 0.0

    if rsi is not None:
        rsi_delta, rsi_factor = _rsi_contribution(rsi)
        value += rsi_delta
        if rsi_factor is not None:
            factors.append(rsi_factor)

    crash_delta, crash_factors = _crash_contribution(bugchecks, dirty)
    value += crash_delta
    factors.extend(crash_factors)

    boot_delta, boot_factor = _boot_contribution(avg_boot)
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
