"""Day-1 scores (Part 1 §9).

Four numbers an engineer can read the instant the agent is installed, derived
from the machine's *own* past + current vitals -- no fleet, no training needed:

  performance    0..100, higher = snappier now
  reliability    0..100, higher = more stable historically
  wear           0..100, higher = more remaining life
  risk_exposure  0..100, higher = MORE exposed (the one inverted score)

Each score is heuristic and, crucially, *explainable*: every function returns
the factors that moved it, so the dashboard can show "why". Missing signals are
skipped (neutral), never treated as zero -- an office PC that blocks a source
must not look healthier than one that reports a real problem.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

Factor = dict[str, Any]  # {"label": str, "delta": float}


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


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%Y%m%d"):
        try:
            if fmt is None:
                return datetime.fromisoformat(s)
            return datetime.strptime(s[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None


def device_age_years(inventory: Optional[dict]) -> Optional[float]:
    """Best-effort age from BIOS release or OS install date."""
    if not inventory:
        return None
    now = datetime.now(timezone.utc)
    best: Optional[float] = None
    for key in ("bios_release_date", "os_install_date"):
        dt = _parse_date(inventory.get(key))
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        years = (now - dt).days / 365.25
        if years >= 0 and (best is None or years > best):
            best = years
    return round(best, 1) if best is not None else None


# --------------------------------------------------------------------------- #
def _performance(hb: Optional[dict], hist: Optional[dict]) -> tuple[float, list[Factor]]:
    val = 100.0
    factors: list[Factor] = []

    def hit(label: str, delta: float) -> None:
        nonlocal val
        val += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    perf = _num(hb, "cpu_perf_pct")  # throttle-residency proxy for thermal health
    if perf is not None and perf < 95:
        hit(f"CPU throttling ({perf:.0f}% of nominal)", -_clamp((95 - perf) * 1.5, 0, 30))

    avail = _num(hb, "mem_avail_mb")
    if avail is not None:
        if avail < 512:
            hit("Critically low free RAM (<512 MB)", -20)
        elif avail < 1024:
            hit("Low free RAM (<1 GB)", -12)
        elif avail < 2048:
            hit("Memory pressure (<2 GB free)", -6)

    pf = _num(hb, "pagefile_pct")
    if pf is not None:
        if pf > 80:
            hit("Heavy pagefile use (thrashing)", -15)
        elif pf > 50:
            hit("Elevated pagefile use", -8)

    for key, label in (("disk_read_sec", "read"), ("disk_write_sec", "write")):
        lat = _num(hb, key)
        if lat is not None:
            if lat > 0.05:
                hit(f"High disk {label} latency ({lat*1000:.0f} ms)", -15)
            elif lat > 0.020:
                hit(f"Elevated disk {label} latency ({lat*1000:.0f} ms)", -8)

    boot = _num(hist, "avg_boot_ms")
    if boot is not None:
        if boot > 90000:
            hit(f"Very slow boot ({boot/1000:.0f}s)", -15)
        elif boot > 60000:
            hit(f"Slow boot ({boot/1000:.0f}s)", -10)
        elif boot > 40000:
            hit(f"Boot above target ({boot/1000:.0f}s)", -5)

    return _clamp(val), factors


def _reliability(hist: Optional[dict]) -> tuple[float, list[Factor]]:
    val = 100.0
    factors: list[Factor] = []

    def hit(label: str, delta: float) -> None:
        nonlocal val
        val += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    rsi = _num(hist, "reliability_stability_index")  # 0..10
    if rsi is not None:
        if rsi < 3:
            hit(f"Very low Windows stability index ({rsi:.1f}/10)", -25)
        elif rsi < 5:
            hit(f"Low stability index ({rsi:.1f}/10)", -15)
        elif rsi < 7:
            hit(f"Moderate stability index ({rsi:.1f}/10)", -7)

    kp = _num(hist, "kernel_power_41_30d")
    if kp:
        hit(f"{int(kp)}x unexpected shutdown (Kernel-Power 41, 30d)", -_clamp(kp * 7, 0, 35))

    bc = _num(hist, "bugchecks_30d")
    if bc:
        hit(f"{int(bc)}x BSOD (BugCheck 1001, 30d)", -_clamp(bc * 12, 0, 40))

    ds = _num(hist, "dirty_shutdowns_30d")
    if ds:
        hit(f"{int(ds)}x dirty shutdown (6008, 30d)", -_clamp(ds * 5, 0, 25))

    ac = _num(hist, "app_crashes_30d")
    if ac:
        hit(f"{int(ac)}x app crash (1000, 30d)", -_clamp(ac * 1, 0, 10))

    return _clamp(val), factors


def _wear(inv: Optional[dict], hist: Optional[dict]) -> tuple[float, list[Factor]]:
    val = 100.0
    factors: list[Factor] = []

    def hit(label: str, delta: float) -> None:
        nonlocal val
        val += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    storage = (hist or {}).get("storage") or []
    max_wear = None
    max_realloc = None
    max_poh = None
    for s in storage:
        w = s.get("wear_pct")
        if w is not None:
            max_wear = w if max_wear is None else max(max_wear, w)
        r = s.get("reallocated_sectors")
        if r is not None:
            max_realloc = r if max_realloc is None else max(max_realloc, r)
        p = s.get("power_on_hours")
        if p is not None:
            max_poh = p if max_poh is None else max(max_poh, p)

    if max_wear is not None and max_wear > 0:
        hit(f"SSD wear {max_wear:.0f}%", -_clamp(max_wear, 0, 70))
    if max_realloc:
        hit(
            f"{int(max_realloc)} reallocated sectors (HDD)",
            -40 if max_realloc > 100 else -20,
        )
    if max_poh is not None:
        if max_poh > 40000:
            hit(f"Drive power-on {max_poh/1000:.0f}k h", -15)
        elif max_poh > 25000:
            hit(f"Drive power-on {max_poh/1000:.0f}k h", -8)

    bat = (hist or {}).get("battery")
    if bat and bat.get("present") and bat.get("wear_pct") is not None:
        bw = float(bat["wear_pct"])
        if bw > 0:
            hit(f"Battery wear {bw:.0f}%", -_clamp(bw * 0.6, 0, 40))

    age = device_age_years(inv)
    if age is not None:
        if age > 7:
            hit(f"Hardware age ~{age:.0f}y", -15)
        elif age > 5:
            hit(f"Hardware age ~{age:.0f}y", -8)
        elif age > 3:
            hit(f"Hardware age ~{age:.0f}y", -3)

    return _clamp(val), factors


def _risk_exposure(
    inv: Optional[dict], hist: Optional[dict], hb: Optional[dict]
) -> tuple[float, list[Factor]]:
    val = 0.0  # starts clean; risk accumulates
    factors: list[Factor] = []

    def hit(label: str, delta: float) -> None:
        nonlocal val
        val += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    free = _num(hb, "free_space_pct")
    if free is not None:
        if free < 5:
            hit(f"System drive {free:.0f}% free (cascade risk)", 30)
        elif free < 10:
            hit(f"System drive {free:.0f}% free", 18)
        elif free < 15:
            hit(f"System drive {free:.0f}% free", 8)

    whea = _num(hist, "whea_errors_30d")
    if whea:
        hit(f"{int(whea)}x corrected HW error (WHEA, 30d)", 25 if whea > 10 else 12)

    if inv and inv.get("pending_reboot"):
        hit("Pending reboot (servicing stuck)", 10)

    dpc = _num(inv, "driver_problem_count")
    if dpc:
        hit(f"{int(dpc)} device(s) with driver fault", _clamp(dpc * 5, 0, 20))

    storage = (hist or {}).get("storage") or []
    disk_errors = 0
    for s in storage:
        disk_errors += int(s.get("read_errors_total") or 0)
        disk_errors += int(s.get("write_errors_total") or 0)
    if disk_errors > 0:
        hit(f"{disk_errors} cumulative disk I/O error(s)", 25)

    nic = _num(hb, "nic_errors")
    if nic and nic > 0:
        hit("NIC packet errors", 5)

    return _clamp(val), factors


def compute_day1_scores(
    inventory: Optional[dict],
    historical: Optional[dict],
    heartbeat: Optional[dict],
) -> dict[str, Any]:
    perf, perf_f = _performance(heartbeat, historical)
    rel, rel_f = _reliability(historical)
    wear, wear_f = _wear(inventory, historical)
    risk, risk_f = _risk_exposure(inventory, historical, heartbeat)
    return {
        "performance": round(perf, 1),
        "reliability": round(rel, 1),
        "wear": round(wear, 1),
        "risk_exposure": round(risk, 1),
        "factors": {
            "performance": perf_f,
            "reliability": rel_f,
            "wear": wear_f,
            "risk_exposure": risk_f,
        },
    }
