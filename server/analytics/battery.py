"""W4.2 battery health engine: a deterministic capacity-fade verdict for one device.

The 2nd of the independent domain engines (cctodo §4 W4.2). Like the storage engine,
it speaks only about its own domain and does it honestly. Windows/WMI exposes a narrow
window onto battery health:

  * **Capacity fade is the leading signal.** DesignedCapacity vs FullChargedCapacity
    gives wear% (the agent reports it, and we re-derive it when only the raw mWh
    capacities are present). A worn battery has lost usable charge -- that is real,
    measurable degradation.
  * **Cycle count is context, never the verdict.** A high cycle count ages a battery
    but, on its own, does not fail it (a premium cell at 1000 cycles can still hold
    90% capacity). So cycles only grade -- they may nudge risk, never dominate it.

The constraint that shapes the whole engine: **WMI cannot see physical swelling,
internal resistance, or charge habits.** Capacity fade is about age/usage; *swelling
is a separate, sometimes-sudden failure mode we do not measure* ("swelling != age").
So this engine never issues a confident all-clear: even a pristine battery caps at
*medium* confidence and always carries the swelling blind spot in ``missing_evidence``.

Output is the ``battery_risk`` axis in the W0.5 Score100 envelope (higher = worse)
with the same gating: untrusted identity withholds; no battery present (a desktop) ->
not applicable; battery present but no usable metric -> UNKNOWN (never a confident
zero). Pure arithmetic over the latest historical reading (D4, no ML). The wear
*trend*/ETA lives in the W4.1 trajectory engine; this engine is the current-state verdict.
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

# Capacity is the only failure mode WMI lets us measure; swelling, internal
# resistance and charge habits are invisible. We say so on every present-battery
# verdict so a healthy capacity reading is never mistaken for a safety clearance.
_SWELLING_BLIND_SPOT = "physical swelling / safety not observable (WMI exposes capacity only)"

_UNTRUSTED_REASON = "device identity untrusted (contract §7)"


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


def _wear_pct(bat: dict) -> tuple[Optional[float], Optional[str]]:
    """Battery wear% and where it came from: reported, derived from mWh, or absent.

    Prefer the agent's reported ``wear_pct``; otherwise re-derive it from the raw
    DesignedCapacity / FullChargedCapacity the same way the agent does. ``full`` of
    0 (a dead battery) legitimately yields 100% wear; ``full`` above ``design`` (a
    fresh cell / firmware quirk) clamps to 0 rather than a negative (healthy) risk.
    """
    reported = _num(bat, "wear_pct")
    if reported is not None:
        return _clamp(reported), "reported"
    design = _num(bat, "design_capacity_mwh")
    full = _num(bat, "full_charge_capacity_mwh")
    if design is not None and design > 0 and full is not None:
        # Round to 1 dp to match the agent's own wear formula (historical.py), so a
        # derived wear and a reported wear never straddle a grading boundary.
        return _clamp(round((1.0 - full / design) * 100.0, 1)), "derived"
    return None, None


def _grade(wear: Optional[float], cycles: Optional[float]) -> tuple[float, list[Factor]]:
    """Risk 0..100 from capacity fade (leading) + cycle count (context only)."""
    value = 0.0
    factors: list[Factor] = []

    def hit(label: str, delta: float) -> None:
        nonlocal value
        value += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    if wear is not None:
        if wear >= 50:
            hit(f"battery {wear:.0f}% capacity loss — severely degraded", 65)
        elif wear >= 35:
            hit(f"battery {wear:.0f}% capacity loss — significant", 45)
        elif wear >= 20:
            hit(f"battery {wear:.0f}% capacity loss (service recommended)", 22)
        elif wear >= 12:
            hit(f"battery {wear:.0f}% capacity loss", 8)

    # Context only: a high cycle count ages a battery but never fails it alone.
    if cycles is not None:
        if cycles >= 1000:
            hit(f"{int(cycles)} charge cycles (heavily used)", 12)
        elif cycles >= 500:
            hit(f"{int(cycles)} charge cycles", 6)

    return _clamp(value), factors


def compute_battery_risk(
    historical: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
) -> Score100:
    """Deterministic battery capacity-fade risk for one device.

    Higher = more degraded. Gating mirrors W0.5/W4.1: untrusted identity withholds;
    no battery present (a desktop) -> not applicable (distinct from a blind spot);
    a present battery with no usable capacity or cycle reading -> UNKNOWN (never a
    confident zero). Confidence caps at *medium* whenever a battery is present,
    because WMI cannot see swelling -- a clean capacity reading is not an all-clear.
    """
    direction: Direction = "higher_is_worse"

    if device_trust == "untrusted":
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["identity trust failed"],
            source_lineage={"identity": "untrusted"},
            reason=_UNTRUSTED_REASON,
        )

    bat = (historical or {}).get("battery")
    if not isinstance(bat, dict):
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["no battery telemetry"],
            reason="no battery telemetry (UNKNOWN over false confidence)",
        )

    if not bat.get("present"):
        # A desktop genuinely has no battery: not applicable, not a blind spot.
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            source_lineage={"battery_present": False},
            reason="no battery present (desktop — not applicable)",
        )

    wear, wear_source = _wear_pct(bat)
    cycles = _num(bat, "cycle_count")

    if wear is None and cycles is None:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["no battery capacity or cycle data"],
            source_lineage={"battery_present": True},
            reason="battery present but no usable metric (UNKNOWN over false confidence)",
        )

    value, factors = _grade(wear, cycles)

    # Capacity is a direct measurement -> medium (swelling caps it below high);
    # cycles alone are a weak proxy -> low.
    confidence: ScoreConfidence = "medium" if wear is not None else "low"

    missing = [_SWELLING_BLIND_SPOT]
    if wear is None:
        missing.append("battery capacity (design/full) unavailable — cycles only")
    if cycles is None:
        missing.append("cycle count unavailable")

    return make_score100(
        value,
        direction,
        band_for_risk_score(value),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage={
            "battery_present": True,
            "wear_source": wear_source,
            "wear_pct": round(wear, 1) if wear is not None else None,
            "cycle_count": int(cycles) if cycles is not None else None,
        },
        reason="capacity nominal; swelling not observable" if value == 0.0 else "",
    )
