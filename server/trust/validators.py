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


_SMART_MONOTONIC_ATTRS = ("5", "197", "198")


def validate_smart_item(item: dict, last: Optional[dict]) -> Result:
    """Semantic check for the deep-SMART optional storage member (ssd3 Ф1).

    Range-checks percentages/temperature, then -- only against a last_good for
    the SAME serial_hash -- checks that damage counters never decrease. A
    different serial_hash means the disk was replaced: a lower reading there
    is a legitimate reset, not a suspect source (K2: state is not history).

    Known ceiling: serial_hash is agent-self-reported with no independent
    hardware root of trust, so an agent that varies it every sample would
    never trip the rollback check. Harmless today -- "smart" only ever
    contributes/drops in the optional storage-domain slot, it can't gate or
    weight the domain (see resolve_domain_trust) -- but Ф2 wires these same
    counters into the storage risk engine's score directly, so a churn-rate
    check (or cross-anchoring to the same envelope's storage_reliability
    reading) becomes worth adding there.
    """
    for key in ("nvme_spare_pct", "nvme_spare_threshold_pct", "nvme_percentage_used"):
        status, reason = validate_scalar_range(f"smart.{key}", item.get(key), 0.0, 100.0)
        if status is not SemanticStatus.PLAUSIBLE:
            return status, reason
    temp = _num(item.get("temperature_c"))
    if temp is not None and (temp < -10 or temp > 100):
        return SemanticStatus.IMPLAUSIBLE, f"smart.temperature_c={temp}"
    for key in ("nvme_media_errors", "nvme_unsafe_shutdowns", "power_on_hours"):
        cur = _num(item.get(key))
        if cur is not None and cur < 0:
            return SemanticStatus.IMPLAUSIBLE, f"smart.{key}={cur} (negative)"

    same_disk = (
        last is not None
        and item.get("serial_hash") is not None
        and last.get("serial_hash") == item.get("serial_hash")
    )
    if not same_disk:
        return _OK
    for key in ("nvme_media_errors", "nvme_unsafe_shutdowns", "power_on_hours"):
        cur = _num(item.get(key))
        prev = _num((last or {}).get(key))
        if cur is not None and prev is not None and cur < prev:
            return SemanticStatus.INCONSISTENT, f"smart.{key} dropped {prev}->{cur} (counter reset)"
    attrs = item.get("smart_attrs")
    last_attrs = (last or {}).get("smart_attrs")
    if isinstance(attrs, dict) and isinstance(last_attrs, dict):
        for attr_id in _SMART_MONOTONIC_ATTRS:
            cur_a = _num(attrs.get(attr_id))
            prev_a = _num(last_attrs.get(attr_id))
            if cur_a is not None and prev_a is not None and cur_a < prev_a:
                return (
                    SemanticStatus.INCONSISTENT,
                    f"smart.attr[{attr_id}] dropped {prev_a}->{cur_a} (counter reset)",
                )
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
        "free_space",
        "reliability",
        "boot_time",
        "throttle",
        "event_counts",
        "network",
        "smart",
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


def validate_network(reading: dict) -> Result:
    """Stateless range checks over the quality probes + Wi-Fi signal (Phase 2).

    The network source feeds the network_risk axis (decision-material), so
    garbage must not pass: loss outside 0..100, negative/absurd latency or a
    signal% outside 0..100 mark the source IMPLAUSIBLE.
    """
    for q in reading.get("quality") or []:
        if not isinstance(q, dict):
            continue
        for key, lo, hi in (("loss_pct", 0.0, 100.0), ("latency_ms", 0.0, 60000.0)):
            status, reason = validate_scalar_range(f"network.{key}", q.get(key), lo, hi)
            if status is not SemanticStatus.PLAUSIBLE:
                return status, reason
    for sig in reading.get("signal_pcts") or []:
        status, reason = validate_scalar_range("network.signal_pct", sig, 0.0, 100.0)
        if status is not SemanticStatus.PLAUSIBLE:
            return status, reason
    return _OK


def validate_source(source: str, reading: dict, last: Optional[dict]) -> Result:
    if source not in MATERIAL_SOURCES:
        return SemanticStatus.UNCHECKED, None
    if source == "network":
        return validate_network(reading)
    if source == "storage_reliability":
        kb = _known_bad(reading)
        if kb[0] is not SemanticStatus.PLAUSIBLE:
            return kb
        return validate_storage_item(reading, last)
    if source == "smart":
        return validate_smart_item(reading, last)
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
