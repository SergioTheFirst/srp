"""Metric extractors for ``trends.compute_trends``: one reading per spread
historical / heartbeat series row (None = no reading). Split out of trends.py
(800-line cap); trends re-imports them, so the old import paths keep working."""

from __future__ import annotations

from statistics import median
from typing import Any, Callable, Optional

from server.analytics.disk_bus import disk_reading, fixed_disks
from server.scoring.scores import loaded_perf


# --------------------------------------------------------------------------- #
# Metric extractors (over the spread historical / heartbeat series rows)
# --------------------------------------------------------------------------- #
def _disk_wear_level(disk: dict[str, Any]) -> Optional[float]:
    """Ф2: NVMe's own percentage_used is at least as precise as generic wear_pct
    when both are reported; take whichever is higher (None-safe max)."""
    wear = disk_reading(disk).get("wear_pct")  # S2: the counter's 0 is no wear reading
    used = disk.get("nvme_percentage_used")
    vals = [float(v) for v in (wear, used) if v is not None]
    return max(vals) if vals else None


def _max_storage_wear(row: dict[str, Any]) -> Optional[float]:
    worst: Optional[float] = None
    for disk in fixed_disks(row.get("storage")):  # 0.3.4: a USB stick's wear is not the PC's
        w = _disk_wear_level(disk)
        if w is not None:
            worst = w if worst is None else max(worst, w)
    return worst


def _disk_smart_attr(attr_id: str) -> Callable[[dict[str, Any]], Optional[float]]:
    """Extractor over a single disk's own append-only series (disk_readings)."""

    def _extract(row: dict[str, Any]) -> Optional[float]:
        attrs = row.get("smart_attrs")
        v = attrs.get(attr_id) if isinstance(attrs, dict) else None
        if v is None and attr_id == "5":
            v = row.get("reallocated_sectors")  # same physical counter, legacy field
        return float(v) if v is not None else None

    return _extract


def _disk_nvme_spare_pct(row: dict[str, Any]) -> Optional[float]:
    v = row.get("nvme_spare_pct")
    return float(v) if v is not None else None


def _disk_nvme_media_errors(row: dict[str, Any]) -> Optional[float]:
    v = row.get("nvme_media_errors")
    return float(v) if v is not None else None


def _boot_ms(row: dict[str, Any]) -> Optional[float]:
    v = row.get("avg_boot_ms")
    return float(v) if v is not None else None


def _free_space_pct(row: dict[str, Any]) -> Optional[float]:
    v = row.get("free_space_pct")
    return float(v) if v is not None else None


def _cpu_perf_pct(row: dict[str, Any]) -> Optional[float]:
    return loaded_perf(row)  # H3: idle SpeedStep is no throttle point


def _gateway_latency_ms(row: dict[str, Any]) -> Optional[float]:
    """Median gateway-probe latency of one snapshot; full-loss probes carry none."""
    vals = [
        float(q["latency_ms"])
        for q in row.get("network_quality") or []
        if isinstance(q, dict)
        and q.get("target_kind") == "gateway"
        and q.get("latency_ms") is not None
        and (q.get("loss_pct") or 0.0) < 100.0
    ]
    return median(vals) if vals else None


def _disk_tail_ratio(row: dict[str, Any]) -> Optional[float]:
    """Ф4 (K4/K7): p95/p50 read-latency ratio -- the "cost of operation" shape,
    a robust-variance stand-in for tail growth at a calm average (a system
    paying with retries/ECC widens its tail long before the mean moves).

    Noisy from a single heartbeat's 8-sample micro-series (``disk_lat_samples``
    below 4 means the PS side never had enough valid deltas) -- only trusted as
    a trend over MANY heartbeats, never a single reading.
    """
    samples = row.get("disk_lat_samples")
    if samples is None or samples < 4:
        return None
    p50 = row.get("disk_read_ms_p50")
    p95 = row.get("disk_read_ms_p95")
    if p50 is None or p95 is None or p50 == 0:
        return None
    return float(p95) / float(p50)
