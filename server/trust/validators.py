"""Semantic plausibility validators (server-side judgment).

Stateless checks (range, cross-field) + a frozen/impossible-delta check that uses
ONLY the last-good sample (one row, no full history -- trend-based validation is
deferred to W0.1). Materiality governor: only decision-material sources are checked;
everything else returns UNCHECKED and can never become SUSPECT.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from server.trust.states import SemanticStatus

Result = Tuple[SemanticStatus, Optional[str]]

_OK: Result = (SemanticStatus.PLAUSIBLE, None)


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def validate_scalar_range(source: str, value: Any, lo: float, hi: float) -> Result:
    v = _num(value)
    if v is None:
        return _OK  # absence is a collector concern, not a semantic one
    if v < lo or v > hi:
        return SemanticStatus.IMPLAUSIBLE, f"{source}={v} outside [{lo},{hi}]"
    return _OK


def validate_storage_item(item: dict, last: Optional[dict]) -> Result:
    wear = _num(item.get("wear_pct"))
    if wear is not None and (wear < 0 or wear > 100):
        return SemanticStatus.IMPLAUSIBLE, f"wear_pct={wear}"
    for key in ("reallocated_sectors", "power_on_hours", "read_errors_total", "write_errors_total"):
        cur = _num(item.get(key))
        if cur is not None and cur < 0:
            return SemanticStatus.IMPLAUSIBLE, f"{key}={cur} (negative)"
        if last is not None:
            prev = _num(last.get(key))
            if cur is not None and prev is not None and cur < prev:
                return SemanticStatus.INCONSISTENT, f"{key} dropped {prev}->{cur} (counter reset)"
    return _OK


def validate_battery(bat: dict) -> Result:
    if not bat.get("present"):
        return _OK
    design = _num(bat.get("design_capacity_mwh"))
    full = _num(bat.get("full_charge_capacity_mwh"))
    wear = _num(bat.get("wear_pct"))
    if design is None:
        return SemanticStatus.INCONSISTENT, "battery present without design capacity"
    if full is not None and design > 0 and full > design * 1.05:  # 5% slack for sensor noise
        return SemanticStatus.INCONSISTENT, f"full({full})>design({design})"
    if wear is not None and (wear < 0 or wear > 100):
        return SemanticStatus.IMPLAUSIBLE, f"battery wear_pct={wear}"
    return _OK


def validate_frozen_constant(source: str, value: Any, last_value: Any) -> Result:
    """Flag a should-vary metric that is byte-identical to its previous sample.

    Weak 1-sample signal (one prior only); multi-sample volatility is deferred to
    W0.1 once history exists. Used for the throttle/thermal proxy (OEM fake-constant).
    """
    cur = _num(value)
    prev = _num(last_value)
    if cur is None or prev is None:
        return _OK
    if cur == prev:
        return SemanticStatus.FROZEN, f"{source} constant at {cur} across samples"
    return _OK


# Decision-material sources only (materiality governor, contract sec.9). Everything
# else -> UNCHECKED, and an UNCHECKED source can never become SUSPECT.
MATERIAL_SOURCES = frozenset(
    {
        "storage_reliability",
        "battery",
        "free_space",
        "reliability",
        "boot_time",
        "throttle",
        "event_counts",
    }
)

# Seed known-bad registry (a hook, not a platform): (model_substr, firmware) -> reason.
# Real list curated out-of-band later; this is the wiring point.
_KNOWN_BAD_FIRMWARE = {
    ("BadSSD X1", "EVIL01"): "known-bad firmware (advisory seed)",
}


def _known_bad(item: dict) -> Result:
    model = str(item.get("model") or "")
    fw = str(item.get("firmware") or "")
    for (m, f), reason in _KNOWN_BAD_FIRMWARE.items():
        if m in model and f == fw:
            return SemanticStatus.KNOWN_BAD, f"{reason}: {model}/{fw}"
    return _OK


def validate_source(source: str, reading: dict, last: Optional[dict]) -> Result:
    if source not in MATERIAL_SOURCES:
        return SemanticStatus.UNCHECKED, None
    if source == "storage_reliability":
        kb = _known_bad(reading)
        if kb[0] is not SemanticStatus.PLAUSIBLE:
            return kb
        return validate_storage_item(reading, last)
    if source == "battery":
        return validate_battery(reading)
    if source == "free_space":
        return validate_scalar_range(source, reading.get("value"), 0.0, 100.0)
    if source == "reliability":
        return validate_scalar_range(source, reading.get("value"), 0.0, 10.0)
    if source == "boot_time":
        return validate_scalar_range(source, reading.get("value"), 0.0, 600000.0)
    if source == "throttle":
        last_value = (last or {}).get("value")
        return validate_frozen_constant(source, reading.get("value"), last_value)
    if source == "event_counts":
        # Expects a pre-projected count-only dict (the wiring layer, W0.1, decides what lands here).
        for key, val in reading.items():
            status, reason = validate_scalar_range(key, val, 0.0, 1_000_000.0)
            if status is not SemanticStatus.PLAUSIBLE:
                return status, reason
        return _OK
    return _OK  # pragma: no cover
