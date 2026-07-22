"""W0.5 Score100: the confidence-gated score envelope (the contract W4 reuses).

A Score100 carries a 0..100 ``value`` (or ``None`` when we cannot responsibly
assert it) plus the band / confidence / factors / missing_evidence / lineage that
let the dashboard show *why*. The governing rule (telemetry-trust-contract.md):
**UNKNOWN over false confidence** -- missing or untrusted telemetry must never
read as a confident healthy score.

This module only defines the shape + the day-1 gating. The W4 axes
(trajectory_risk, fleet_anomaly, operator_urgency) will reuse this same envelope.
No nested confidence calculus -- simple, explicit rules only (contract §13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Direction = Literal["higher_is_better", "higher_is_worse"]
ScoreBand = Literal["good", "watch", "bad", "unknown"]
ScoreConfidence = Literal["high", "medium", "low", "unknown"]

Factor = dict[str, Any]


@dataclass(frozen=True)
class Score100:
    value: Optional[float]  # 0..100, or None when insufficient data / untrusted
    direction: Direction
    band: ScoreBand
    confidence: ScoreConfidence
    factors: list[Factor] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    source_lineage: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def make_score100(
    value: Optional[float],
    direction: Direction,
    band: ScoreBand,
    confidence: ScoreConfidence,
    factors: Optional[list[Factor]] = None,
    missing_evidence: Optional[list[str]] = None,
    source_lineage: Optional[dict[str, Any]] = None,
    reason: str = "",
) -> Score100:
    return Score100(
        value=value,
        direction=direction,
        band=band,
        confidence=confidence,
        factors=list(factors or []),
        missing_evidence=list(missing_evidence or []),
        source_lineage=dict(source_lineage or {}),
        reason=reason,
    )


def score_to_dict(score: Score100) -> dict[str, Any]:
    """JSON-serialisable form for storage inside the scores.risk blob."""
    return {
        "value": score.value,
        "direction": score.direction,
        "band": score.band,
        "confidence": score.confidence,
        "factors": score.factors,
        "missing_evidence": score.missing_evidence,
        "source_lineage": score.source_lineage,
        "reason": score.reason,
    }


def legacy_value(score: Score100) -> Optional[float]:
    """The plain 0..100 number for the legacy scores columns (None when withheld)."""
    return score.value


def band_for_health_score(value: Optional[float]) -> ScoreBand:
    """Higher is better: good >= 70, watch >= 40, else bad; None -> unknown."""
    if value is None:
        return "unknown"
    if value >= 70:
        return "good"
    if value >= 40:
        return "watch"
    return "bad"


def band_for_risk_score(value: Optional[float]) -> ScoreBand:
    """Higher is worse: good < 15, watch < 40, else bad; None -> unknown."""
    if value is None:
        return "unknown"
    if value < 15:
        return "good"
    if value < 40:
        return "watch"
    return "bad"


# --------------------------------------------------------------------------- #
# Internal gating
# --------------------------------------------------------------------------- #
def _domain_state(domains: dict[str, Any], name: str) -> Optional[str]:
    d = domains.get(name)
    return d.get("state") if isinstance(d, dict) else None


_UNTRUSTED_REASON = "идентичность устройства не подтверждена (контракт §7)"

# >=2 blind optional risk domains -> low confidence: a single optional blind spot
# only mildly understates risk, two or more is material (contract §13).
_RISK_LOW_CONF_OPTIONAL_UNKNOWN = 2


def _gate_axis(
    *,
    numeric: float,
    direction: Direction,
    factors: list[Factor],
    is_risk: bool,
    device_trust: str,
    trust: Optional[dict[str, Any]],
    presence_ok: bool,
    presence_missing: list[str],
    required: list[str],
    optional: list[str],
) -> Score100:
    """Apply the W0.5 gating rules to one day-1 axis.

    Order (first match wins): untrusted identity -> withhold; no telemetry for the
    axis -> withhold; no trust evaluated (old agent) -> keep value at low
    confidence; otherwise gate on per-domain trust. A required domain that is
    UNKNOWN withholds a health value (conservative), but only *lowers confidence*
    for risk (a blind spot under-counts risk -- we keep what we saw and flag it).
    """
    band_of = band_for_risk_score if is_risk else band_for_health_score

    # 1. Untrusted identity -> no reliable priors/cohort: withhold entirely.
    if device_trust == "untrusted":
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            factors=factors,
            missing_evidence=["идентичность устройства не подтверждена"],
            source_lineage={"identity": "untrusted"},
            reason=_UNTRUSTED_REASON,
        )

    # 2. No telemetry feeds this axis at all -> cannot assert.
    if not presence_ok:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            factors=factors,
            missing_evidence=list(presence_missing),
            source_lineage={},
            reason="нет телеметрии для этого показателя",
        )

    # 3. Trust not evaluated (old agent, no source_health) -> keep number, low conf.
    if trust is None:
        return make_score100(
            numeric,
            direction,
            band_of(numeric),
            "low",
            factors=factors,
            missing_evidence=["source_health отсутствует"],
            source_lineage={},
            reason="агент не передал source_health",
        )

    # 4. Trust present -> per-domain gating.
    domains = trust.get("domains", {})
    lineage = {
        n: {"state": _domain_state(domains, n)}
        for n in (required + optional)
        if _domain_state(domains, n) is not None
    }
    required_unknown = [n for n in required if _domain_state(domains, n) == "unknown"]
    optional_unknown = [n for n in optional if _domain_state(domains, n) == "unknown"]
    missing = [f"{n} недоступен" for n in (required_unknown + optional_unknown)]

    if required_unknown:
        if is_risk:
            return make_score100(
                numeric,
                direction,
                band_of(numeric),
                "low",
                factors=factors,
                missing_evidence=missing,
                source_lineage=lineage,
                reason="обязательный домен не виден; риск может быть занижен",
            )
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            factors=factors,
            missing_evidence=missing,
            source_lineage=lineage,
            reason="обязательный домен неизвестен",
        )

    if is_risk and len(optional_unknown) >= _RISK_LOW_CONF_OPTIONAL_UNKNOWN:
        confidence: ScoreConfidence = "low"
    elif optional_unknown:
        confidence = "medium"
    else:
        confidence = "high"
    return make_score100(
        numeric,
        direction,
        band_of(numeric),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage=lineage,
        reason="",
    )


# --------------------------------------------------------------------------- #
# Public: observability + the day-1 Score100 map
# --------------------------------------------------------------------------- #
def compute_observability_score(
    trust: Optional[dict[str, Any]], payload_presence: dict[str, Any]
) -> Score100:
    """How well can we actually observe this device (higher = better coverage).

    Penalises UNKNOWN domains, regressed sources, clock drift and untrusted
    identity. With no source_health at all, observability is poor *by definition*.
    """
    direction: Direction = "higher_is_better"
    has_health = payload_presence.get("has_source_health", trust is not None)
    device_trust = payload_presence.get("device_trust", "ok")
    clock_drift = payload_presence.get("clock_drift", False)

    if not has_health or trust is None:
        return make_score100(
            20.0,
            direction,
            band_for_health_score(20.0),
            "low",
            factors=[{"label": "агент не передаёт данные source_health", "delta": -80.0}],
            missing_evidence=["source_health отсутствует"],
            source_lineage={},
            reason="нет source_health -> покрытие нельзя оценить",
        )

    domains = trust.get("domains", {})
    sources = trust.get("sources", {})
    applicable = {
        n: d
        for n, d in domains.items()
        if isinstance(d, dict) and d.get("state") != "not_applicable"
    }
    total = len(applicable)
    if total == 0:
        # No applicable domains -> no coverage ratio to compute. UNKNOWN over a
        # fabricated "0 = bad" reading.
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["нет применимых доменов для оценки покрытия"],
            reason="нет применимых доменов",
        )
    unknown = [n for n, d in applicable.items() if d.get("state") == "unknown"]
    trusted = total - len(unknown)
    value = trusted / total * 100.0

    factors: list[Factor] = [
        {"label": f"покрытие доменов {trusted}/{total}", "delta": round(value, 1)}
    ]
    missing = [f"{n} неизвестен" for n in unknown]

    regressed = sorted(n for n, s in sources.items() if isinstance(s, dict) and s.get("regressed"))
    if regressed:
        penalty = min(len(regressed) * 10.0, 30.0)
        value -= penalty
        factors.append(
            {"label": f"деградировавших источников: {len(regressed)}", "delta": -penalty}
        )
        missing += [f"{n} деградировал" for n in regressed]
    if clock_drift:
        value -= 10.0
        factors.append({"label": "рассинхронизация часов", "delta": -10.0})
    untrusted = device_trust == "untrusted"
    if untrusted:
        # Observability PENALISES untrusted identity (clamp to "bad") rather than
        # withholding like the health axes: its job is to SURFACE that this device's
        # telemetry can't be trusted -- a None would hide that. But we are not
        # confident in anything from an untrusted device, so confidence drops to low.
        value = min(value, 10.0)
        missing.append("идентичность устройства не подтверждена")

    value = max(0.0, min(100.0, value))
    confidence: ScoreConfidence
    if untrusted:
        confidence = "low"
    elif unknown:
        confidence = "medium"
    else:
        confidence = "high"
    return make_score100(
        round(value, 1),
        direction,
        band_for_health_score(value),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage={n: {"state": d.get("state")} for n, d in domains.items()},
        reason=(
            "идентичность не подтверждена -> качество телеметрии не гарантировано"
            if untrusted
            else ""
        ),
    )


def compute_day1_score100(
    day1: dict[str, Any],
    inventory: Optional[dict[str, Any]],
    historical: Optional[dict[str, Any]],
    heartbeat: Optional[dict[str, Any]],
    trust: Optional[dict[str, Any]],
    device_trust: str = "ok",
    clock_drift: bool = False,
) -> dict[str, Score100]:
    """Wrap compute_day1_scores output in the confidence-gated Score100 envelope."""
    f = day1.get("factors", {})
    performance = _gate_axis(
        numeric=day1["performance"],
        direction="higher_is_better",
        factors=f.get("performance", []),
        is_risk=False,
        device_trust=device_trust,
        trust=trust,
        presence_ok=heartbeat is not None,
        presence_missing=["heartbeat (текущие показатели) отсутствует"],
        required=[],
        optional=["thermal", "boot"],
    )
    reliability = _gate_axis(
        numeric=day1["reliability"],
        direction="higher_is_better",
        factors=f.get("reliability", []),
        is_risk=False,
        device_trust=device_trust,
        trust=trust,
        presence_ok=historical is not None,
        presence_missing=["historical (история стабильности) отсутствует"],
        required=["os_stability"],
        optional=[],
    )
    wear = _gate_axis(
        numeric=day1["wear"],
        direction="higher_is_better",
        factors=f.get("wear", []),
        is_risk=False,
        device_trust=device_trust,
        trust=trust,
        presence_ok=(historical is not None or inventory is not None),
        presence_missing=["historical/inventory (данные об износе) отсутствует"],
        required=["storage"],
        optional=[],
    )
    risk_exposure = _gate_axis(
        numeric=day1["risk_exposure"],
        direction="higher_is_worse",
        factors=f.get("risk_exposure", []),
        is_risk=True,
        device_trust=device_trust,
        trust=trust,
        presence_ok=(historical is not None or heartbeat is not None),
        presence_missing=["historical/heartbeat (данные о риске) отсутствует"],
        required=[],
        optional=["disk_fill", "storage", "os_stability", "thermal"],
    )
    observability = compute_observability_score(
        trust,
        {
            "has_source_health": trust is not None,
            "device_trust": device_trust,
            "clock_drift": clock_drift,
        },
    )
    return {
        "performance": performance,
        "reliability": reliability,
        "wear": wear,
        "risk_exposure": risk_exposure,
        "observability": observability,
    }
