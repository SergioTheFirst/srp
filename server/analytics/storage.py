"""W4.2 storage health engine: a deterministic SMART verdict for one device.

The first of the independent domain engines (cctodo §4 W4.2): "multi-engine, not
unified core". Where the day-1 ``wear`` score lumps disk + battery + age into one
number, this engine speaks *only* about storage and does it rigorously:

  * **SMART / StorageReliabilityCounter is the leading signal** -- reallocated
    sectors and cumulative read/write errors are near-certain failure precursors;
    SSD wear% and temperature are graded contributors.
  * **Disk latency is a confirmation, never a standalone signal.** High latency is
    causally confounded (Defender, OneDrive, BitLocker, low RAM, thermal), so it
    may only *amplify* a problem SMART already flagged -- it can never raise
    storage risk on its own. This is what stops latency-driven false alarms.

Output is the ``storage_risk`` axis in the W0.5 Score100 envelope (higher = worse)
with the same gating: untrusted identity withholds; no SMART data -> UNKNOWN, never
a confident zero. Pure arithmetic over the latest historical reading (D4, no ML).
The wear *trend* (slope/ETA) lives in the W4.1 trajectory engine; this engine is
the current-state verdict.
"""

from __future__ import annotations

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


def _has_smart(disk: dict) -> bool:
    return any(disk.get(f) is not None for f in _SMART_FIELDS)


def _score_disk(disk: dict, heartbeat: Optional[dict]) -> tuple[float, list[Factor]]:
    """Risk 0..100 for one disk from its SMART reading; higher = closer to failure."""
    value = 0.0
    factors: list[Factor] = []

    def hit(label: str, delta: float) -> None:
        nonlocal value
        value += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    realloc = _num(disk, "reallocated_sectors")
    if realloc is not None and realloc > 0:
        # A growing reallocated count is the classic pre-failure signal.
        hit(
            f"{int(realloc)} reallocated sector(s)" + (" — drive failing" if realloc > 100 else ""),
            60 if realloc > 100 else 35,
        )

    read_err = _num(disk, "read_errors_total") or 0.0
    write_err = _num(disk, "write_errors_total") or 0.0
    io_errors = read_err + write_err
    if io_errors > 0:
        hit(f"{int(io_errors)} cumulative I/O error(s)", 60 if io_errors > 100 else 40)

    wear = _num(disk, "wear_pct")
    if wear is not None:
        if wear > 95:
            hit(f"SSD wear {wear:.0f}% (end of rated life)", 40)
        elif wear > 85:
            hit(f"SSD wear {wear:.0f}%", 25)
        elif wear > 70:
            hit(f"SSD wear {wear:.0f}%", 12)

    temp = _num(disk, "temperature_c")
    if temp is not None:
        if temp > 70:
            hit(f"Drive {int(temp)}°C (thermal stress)", 15)
        elif temp > 60:
            hit(f"Drive {int(temp)}°C (warm)", 8)

    poh = _num(disk, "power_on_hours")
    if poh is not None:
        if poh > 40000:
            hit(f"Power-on {poh / 1000:.0f}k h", 8)
        elif poh > 25000:
            hit(f"Power-on {poh / 1000:.0f}k h", 4)

    # Confirmation only: latency may amplify a SMART signal, never create one.
    if value > 0:
        latency = max(
            _num(heartbeat, "disk_read_sec") or 0.0, _num(heartbeat, "disk_write_sec") or 0.0
        )
        if latency > _LATENCY_HIGH_SEC:
            hit(f"high disk latency confirms SMART signal ({latency * 1000:.0f} ms)", 10)

    return _clamp(value), factors


def compute_storage_risk(
    historical: Optional[dict[str, Any]],
    heartbeat: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
) -> Score100:
    """Deterministic storage-failure risk for one device, worst disk wins.

    Higher = a drive is closer to failure. Gating mirrors W0.5/W4.1: untrusted
    identity withholds entirely; no SMART data on any disk -> UNKNOWN (never a
    confident zero -- a blocked StorageReliability source must not read healthy).
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
            reason="device identity untrusted (contract §7)",
        )

    disks = (historical or {}).get("storage") or []
    worst_value: Optional[float] = None
    worst_factors: list[Factor] = []
    worst_disk: Optional[str] = None
    smart_disks = 0

    for disk in disks:
        if not isinstance(disk, dict) or not _has_smart(disk):
            continue
        smart_disks += 1
        value, factors = _score_disk(disk, heartbeat)
        if worst_value is None or value > worst_value:
            worst_value, worst_factors, worst_disk = value, factors, disk.get("disk")

    if worst_value is None:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["no SMART / StorageReliability data for any disk"],
            reason="no storage SMART telemetry (UNKNOWN over false confidence)",
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
        },
        reason="" if worst_value > 0 else "SMART nominal on all reporting disks",
    )
