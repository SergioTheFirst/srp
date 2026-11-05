"""Dominant mechanism and systemic detection (spec 2026-10-30, stage 2a: moved
out of health.py unchanged), and the storage-alone rung the bayes storage class
floors at (H10). Pure; health.py and pipeline import it.
"""

from __future__ import annotations

from typing import Optional

from server.analytics.health_hold import disk_view, smart_hidden, visibility
from server.analytics.health_ladder import (
    _HARD_D_FLAGS,
    _TRAJ_MULT,
    _rung,
    _storage_resilience,
)
from server.analytics.trends import _domain_risk

# score100 axis name -> short dominant machine-key.
_AXIS_TO_DOMINANT = {
    "storage_risk": "storage",
    "software_aging_risk": "aging",
    "os_degradation_risk": "os",
    "disk_fill_risk": "disk_fill",
    "network_risk": "network",
    "trajectory_risk": "trajectory",
}
_SEVERITY = {"network_risk": 0.6, "trajectory_risk": 0.85}


def _ax(axes: dict, name: str) -> dict:
    a = axes.get(name)
    return a if isinstance(a, dict) else {}


def _ax_val(axes: dict, name: str) -> Optional[float]:
    v = _ax(axes, name).get("value")
    return float(v) if isinstance(v, (int, float)) else None


# --------------------------------------------------------------------------- #
# Step 9: Dominant / systemic
# --------------------------------------------------------------------------- #
# Trend key -> physical mechanism. Three SMART counters of ONE dying disk are one
# mechanism, not three: "systemic" means several independent things degrade at once.
_TREND_MECHANISM = {
    "storage_wear": "wear",  # rated endurance, not a degradation mechanism
    "nvme_spare": "storage",
    "smart_pending": "storage",
    "smart_realloc": "storage",
    "smart_media_errors": "storage",
    "disk_tail_ratio": "storage",
    "disk_fill": "disk_fill",
    # H2 (F-04): avg_boot_ms is a rolling median of <= 20 boots, autocorrelated by build --
    # visible as a trend, never counted as a mechanism (comes back with last_boot_ms, H17)
    "boot_time": "boot",
    "throttle": "thermal",
    "gateway_latency": "network",
}
# "Systemic" counts only degradation mechanisms -- exactly the causes the systemic
# action names (power, overheating, an update, AV scans). Disk fill, network,
# software aging and wear are operational and keep their own action (D6).
_COUNTED_MECHANISMS = frozenset({"storage", "thermal", "os"})
# A worsening trend is material when its boundary (if any) is within a year;
# significance and materiality of "worsening" itself come with the trend contract.
# A thresholded depletion trend needs that boundary projected: an ETA the 72 h span
# gate still withholds (C1) is no mechanism yet. Counters keep the direction alone.
_MATERIAL_ETA_DAYS = 365.0
_DEPLETION_TRENDS = frozenset({"disk_fill", "nvme_spare", "storage_wear"})


def _material_worsening(key: str, t: Optional[dict]) -> bool:
    t = t or {}
    eta = t.get("eta_days")
    if eta is None:
        return t.get("direction") == "worsening" and key not in _DEPLETION_TRENDS
    return t.get("direction") == "worsening" and float(eta) <= _MATERIAL_ETA_DAYS


def _material_mechanisms(trends: dict) -> set[str]:
    material = (key for key, t in trends.items() if _material_worsening(key, t))
    return {_TREND_MECHANISM.get(key, key) for key in material}


# Mechanism -> the axis that carries it, so a trend-driven state (Resilience
# surcharges for a depleting spare / tail ratio) still names its mechanism even
# while the axis's own band reads "good".
_MECHANISM_AXIS = {
    "storage": "storage_risk",
    "disk_fill": "disk_fill_risk",
    "os": "os_degradation_risk",
    "network": "network_risk",
}


# score100 axis name -> counted degradation mechanism (for _is_systemic dedup).
# trajectory_risk is not an axis mechanism: its domains are already counted by
# their own trends (one filling disk must not read as two mechanisms).
_AXIS_MECHANISM = {"storage_risk": "storage", "os_degradation_risk": "os"}
# A "trajectory" dominant is re-attributed to the depletion trend that drives it.
_TRAJECTORY_DRIVERS = {"disk_fill": "disk_fill", "nvme_spare": "storage"}


def _is_systemic(axes: dict, trends: dict) -> bool:
    axis_mechs = {
        mech
        for name, mech in _AXIS_MECHANISM.items()
        if _ax_val(axes, name) is not None and _ax(axes, name).get("band") in ("watch", "bad")
    }
    return len((axis_mechs | _material_mechanisms(trends)) & _COUNTED_MECHANISMS) >= 3


def _trajectory_driver(trends: dict, value: float) -> str:
    """The worsening disk_fill / nvme_spare trend that produced the trajectory ``value``
    (C1: the riskiest domain; the smallest ETA on a tie), else trajectory: wear keeps it."""
    drivers = []
    for key, short in _TRAJECTORY_DRIVERS.items():
        t = trends.get(key) or {}
        eta = t.get("eta_days")
        if eta is None or t.get("direction") != "worsening":
            continue
        if _domain_risk(key, float(eta)) >= value:
            drivers.append((float(eta), short))
    return min(drivers)[1] if drivers else "trajectory"


def _dominant(axes: dict, trends: dict) -> tuple[Optional[str], bool]:
    if _is_systemic(axes, trends):
        return "systemic", True
    live = {_MECHANISM_AXIS[m] for m in _material_mechanisms(trends) if m in _MECHANISM_AXIS}
    best_key: Optional[str] = None
    best_score = -1.0  # a qualifying axis wins even at value 0 (trend-driven state)
    for name, short in _AXIS_TO_DOMINANT.items():
        v = _ax_val(axes, name)
        # A "good"-band axis is background, not a mechanism: naming it would put
        # "накопитель" on a healthy machine whose wear ticked from 11 to 12 %.
        # Unless its own trends are materially worsening (that IS the mechanism).
        if v is None or (_ax(axes, name).get("band") not in ("watch", "bad") and name not in live):
            continue
        score = v * _SEVERITY.get(name, 1.0)
        if score > best_score:
            best_score, best_key = score, short
    if best_key == "trajectory":
        return _trajectory_driver(trends, _ax_val(axes, "trajectory_risk") or 0.0), False
    return best_key, False


def chain_stage(axis: dict, errchain: Optional[dict], prev: Optional[dict]) -> Optional[int]:
    """D8/H6: the event-log stage the verdict keeps on R itself (None: the axis carries it).
    No value: the disk-only stage for a device that never had a disk, the full one when
    SMART is hidden (gated, or a failed read) or the disk went out of sight; a SMART-blind
    cycle on an isolated-Ntfs axis: the full one."""
    chain = errchain if isinstance(errchain, dict) else {}
    stage = chain.get("stage")
    if not isinstance(stage, int):
        return None
    if isinstance(axis.get("value"), (int, float)):
        fs = "fs_corruption" in ((axis.get("coords") or {}).get("flags") or [])
        return stage if fs and visibility(disk_view(axis), prev)[0] else None
    disk_only = chain.get("stage_disk_only")
    hidden = smart_hidden(disk_view(axis))
    if not (hidden or (prev or {}).get("had_disk")) and isinstance(disk_only, int):
        return disk_only
    return stage


def storage_rung(
    storage_axis: dict,
    trends: dict,
    errchain: Optional[dict] = None,
    prev: Optional[dict] = None,
) -> Optional[str]:
    """H10: the rung the storage channel alone sets THIS cycle -- ``_rung`` over its D,
    its R loss (``_storage_resilience`` plus the nvme_spare share of the trajectory), its
    flags without fs_corruption (the file system, not the disk -- H6) and the chain stage
    the verdict keeps on R itself (``chain_stage``); None without a value or a chain. The
    bayes storage class floors at it, so the ranker never reads a disk «low» while this
    cycle's storage evidence sets h3/h4. A held level is not floored (spec H10: the live
    rung): through a release window or a blind hold the class reads the fresh evidence."""
    axis = storage_axis if isinstance(storage_axis, dict) else {}
    stage = chain_stage(axis, errchain, prev) or 0
    chain = {"chain_stage3" if stage >= 3 else "chain_stage2"} if stage >= 2 else set()
    if axis.get("value") is None:
        return _rung(0.0, None, chain, 0.0) if chain else None
    coords = axis.get("coords") or {}
    loss, _conf, _facts, r_flags = _storage_resilience(axis, trends)
    spare = trends.get("nvme_spare") or {}
    eta = spare.get("eta_days")
    if spare.get("direction") == "worsening" and isinstance(eta, (int, float)):
        loss = max(loss, _domain_risk("nvme_spare", float(eta)) * _TRAJ_MULT)
    flags = {f for f in coords.get("flags") or [] if f in _HARD_D_FLAGS} | set(r_flags)
    damage = float(coords.get("damage") or 0.0)
    return _rung(damage, 100.0 - loss, (flags | chain) - {"fs_corruption"}, damage)
