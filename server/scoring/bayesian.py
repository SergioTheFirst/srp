"""Explainable Bayesian risk aggregation — thin prioritizer (W4.3, D5/D6).

W4.3 changes applied here:

  D6 — KP41 demoted to conditional enhancer: Kernel-Power 41 has near-zero
  specificity.  It fires on power cuts, driver hangs, RAM faults and clean
  reboots alike.  It may *amplify* a confirmed power/thermal anchor (CPU
  throttle OR dirty shutdown already present) but must not raise risk on its
  own.  Demoted weight: min(kp * 0.3, 1.0) vs old min(kp * 0.6, 2.5).

  D6 — WHEA removed from power_thermal and memory: corrected hardware errors
  in the WHEA Logger are predominantly firmware / ASPM / PCIe link-training
  noise; they do not reliably predict VRM, PSU, or RAM failure.  Keeping WHEA
  as a direct driver produced systematic false-positive "hardware error" alerts.

  D5 — compute_risk() accepts domain_values (W4.2 engine outputs, 0..100) as
  supplementary log-odds factors, making this a thin prioritizer *over* the
  domain engines rather than a standalone signal processor.

  Scale — overall returned on the same 0..100 scale as risk_exposure and all
  W4.2 axes; eliminates the two-scale confusion for the operator.

Per failure class we work in log-odds: a prior (base rate adjusted for age /
media type / known-bad) plus an additive contribution per piece of evidence.
The posterior probability is sigmoid(prior + Σ evidence), and because every
term carries a label, the result is explainable *by construction* — the
dashboard shows exactly which factors drove the number.

Weights are hand-set and deliberately uncalibrated: no failure labels yet.
These are reasonable priors to be replaced by survival models + isotonic
calibration once the label loop produces ground truth (Part 3 C3.8).
"""

from __future__ import annotations

import math
from typing import Any, Optional

from server.scoring.scores import device_age_years

Factor = dict[str, Any]  # {"label": str, "weight": float}

_CLASS_LABELS = {
    "storage": "Накопитель (SSD/HDD)",
    "power_thermal": "Питание / перегрев",
    "memory": "Память (RAM)",
    "stability": "Стабильность ОС/ПО",
}

_BASE_PRIOR = {
    "storage": 0.05,
    "power_thermal": 0.03,
    "memory": 0.02,
    "stability": 0.05,
}


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x < 0:
        z = math.exp(x)
        return z / (1 + z)
    return 1 / (1 + math.exp(-x))


def _level(p: float) -> str:
    if p >= 0.50:
        return "critical"
    if p >= 0.25:
        return "high"
    if p >= 0.10:
        return "elevated"
    return "low"


def _num(d: Optional[dict], key: str) -> Optional[float]:
    if not d:
        return None
    v = d.get(key)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _domain_lo(
    domain_values: Optional[dict[str, Optional[float]]],
    key: str,
    scale: float,
) -> float:
    """Map a W4.2 domain engine score (0..100) to supplementary log-odds.

    Returns 0.0 when the domain value is missing (UNKNOWN) or not provided.
    The scale factor sets the maximum log-odds contribution at score = 100.
    Non-finite values (NaN, Inf) are treated as UNKNOWN to avoid false alarms.
    """
    if not domain_values:
        return 0.0
    v = domain_values.get(key)
    if v is None:
        return 0.0
    fv = float(v)
    if not math.isfinite(fv):
        return 0.0
    fv = max(0.0, min(100.0, fv))
    return (fv / 100.0) * scale


def _finish(name: str, prior_p: float, factors: list[Factor]) -> dict[str, Any]:
    prior_lo = _logit(prior_p)
    posterior_lo = prior_lo + sum(f["weight"] for f in factors)
    prob = _sigmoid(posterior_lo)
    explanation = [{"label": "Базовый риск класса (prior)", "weight": round(prior_lo, 2)}]
    explanation += [
        {"label": f["label"], "weight": round(f["weight"], 2)}
        for f in sorted(factors, key=lambda f: f["weight"], reverse=True)
    ]
    return {
        "name": name,
        "label": _CLASS_LABELS.get(name, name),
        "probability": round(prob, 3),
        "level": _level(prob),
        "factors": explanation,
    }


# --------------------------------------------------------------------------- #
def _storage(
    inv: Optional[dict],
    hist: Optional[dict],
    hb: Optional[dict],
    age: Optional[float],
    domain_values: Optional[dict[str, Optional[float]]] = None,
) -> dict[str, Any]:
    f: list[Factor] = []
    if age:
        f.append({"label": f"Возраст оборудования ~{age:.0f} лет", "weight": min(age * 0.05, 0.8)})

    # Supplementary from W4.2 storage engine: SMART verdict with latency-as-
    # confirmation guard. Scale 1.0 means a 100/100 engine score adds +1.0 log-odds.
    lo = _domain_lo(domain_values, "storage_risk", 1.0)
    if lo > 0.01 and domain_values:
        v_raw = domain_values.get("storage_risk", 0.0)
        f.append({"label": f"Storage engine (SMART): {v_raw:.0f}/100", "weight": round(lo, 2)})

    storage = (hist or {}).get("storage") or []
    max_wear = max((s.get("wear_pct") or 0 for s in storage), default=0)
    max_realloc = max((s.get("reallocated_sectors") or 0 for s in storage), default=0)
    max_poh = max((s.get("power_on_hours") or 0 for s in storage), default=0)
    io_err = sum(
        int(s.get("read_errors_total") or 0) + int(s.get("write_errors_total") or 0)
        for s in storage
    )
    if max_wear > 0:
        f.append({"label": f"Износ SSD {max_wear:.0f}%", "weight": (max_wear / 100) * 3.5})
    if max_realloc > 100:
        f.append({"label": f"Переназначено секторов: {int(max_realloc)}", "weight": 3.2})
    elif max_realloc > 0:
        f.append({"label": f"Переназначено секторов: {int(max_realloc)}", "weight": 2.0})
    if io_err > 0:
        f.append({"label": f"Накопленные ошибки I/O: {io_err}", "weight": 2.6})
    if max_poh > 40000:
        f.append({"label": f"Наработка диска {max_poh / 1000:.0f}k ч", "weight": 0.8})

    lat = _num(hb, "disk_read_sec")
    if lat is not None and lat > 0.03:
        f.append({"label": f"Высокая задержка чтения ({lat * 1000:.0f} мс)", "weight": 0.9})
    return _finish("storage", _BASE_PRIOR["storage"], f)


def _power_thermal(
    hist: Optional[dict],
    hb: Optional[dict],
    domain_values: Optional[dict[str, Optional[float]]] = None,
) -> dict[str, Any]:
    """Power / thermal risk class.

    KP41 (W4.3 D6): conditional enhancer only.  Kernel-Power 41 fires on power
    cuts, driver hangs, RAM faults and clean reboots alike — specificity ≈ 0.
    It may amplify an existing anchor (CPU throttle OR dirty shutdown) but must
    not drive risk independently.  Reduced weight: min(kp * 0.3, 1.0).

    WHEA (W4.3 D6): removed.  Corrected hardware errors are predominantly
    firmware / ASPM / PCIe link-training noise, not early-warning of VRM or PSU
    failure.  Keeping WHEA here produced systematic false-positive alerts.
    """
    f: list[Factor] = []

    perf = _num(hb, "cpu_perf_pct")
    if perf is not None:
        if perf < 85:
            f.append({"label": f"Троттлинг CPU ({perf:.0f}% номинала)", "weight": 1.2})
        elif perf < 95:
            f.append({"label": f"Лёгкий троттлинг CPU ({perf:.0f}%)", "weight": 0.5})

    ds = _num(hist, "dirty_shutdowns_30d")
    if ds:
        f.append({"label": f"{int(ds)}x грязное выключение (6008)", "weight": min(ds * 0.4, 1.5)})

    # KP41 as conditional enhancer: only when a confirmed power/thermal anchor already
    # exists.  Without an anchor, KP41 alone means nothing diagnostically (D6).
    kp = _num(hist, "kernel_power_41_30d")
    _has_anchor = (perf is not None and perf < 85) or (ds is not None and ds > 0)
    if kp and _has_anchor:
        f.append(
            {
                "label": f"{int(kp)}x внезапное отключение KP41 (усилитель)",
                "weight": min(kp * 0.3, 1.0),
            }
        )

    return _finish("power_thermal", _BASE_PRIOR["power_thermal"], f)


def _memory(
    hist: Optional[dict],
    hb: Optional[dict],
) -> dict[str, Any]:
    """Memory (RAM) risk class.

    WHEA (W4.3 D6): removed as a direct driver.  Corrected hardware errors from
    the WHEA Logger are overwhelmingly firmware / ASPM / PCIe noise and do not
    reliably predict DRAM failure.  Remaining signals (BSODs + memory pressure)
    have higher specificity for actual RAM issues.
    """
    f: list[Factor] = []
    bc = _num(hist, "bugchecks_30d")
    if bc:
        f.append({"label": f"{int(bc)}x BSOD — часть приходится на память", "weight": 1.2})
    pf = _num(hb, "pagefile_pct")
    avail = _num(hb, "mem_avail_mb")
    if pf is not None and avail is not None and pf > 80 and avail < 1024:
        f.append({"label": "Тразинг: pagefile>80% при низкой свободной RAM", "weight": 0.8})
    return _finish("memory", _BASE_PRIOR["memory"], f)


def _stability(
    inv: Optional[dict],
    hist: Optional[dict],
    domain_values: Optional[dict[str, Optional[float]]] = None,
    app_hang_count_30d: Optional[int] = None,
) -> dict[str, Any]:
    f: list[Factor] = []
    rsi = _num(hist, "reliability_stability_index")
    if rsi is not None:
        if rsi < 5:
            f.append({"label": f"Низкий индекс стабильности ({rsi:.1f}/10)", "weight": 1.5})
        elif rsi < 7:
            f.append({"label": f"Умеренный индекс стабильности ({rsi:.1f}/10)", "weight": 0.6})
    bc = _num(hist, "bugchecks_30d")
    if bc:
        f.append({"label": f"{int(bc)}x BSOD (30д)", "weight": min(bc * 0.8, 2.5)})
    ac = _num(hist, "app_crashes_30d")
    if ac and ac > 5:
        f.append({"label": f"{int(ac)}x падение приложений (30д)", "weight": 0.8})
    if inv and inv.get("pending_reboot"):
        f.append({"label": "Ожидается перезагрузка (обновления зависли)", "weight": 0.4})
    dpc = _num(inv, "driver_problem_count")
    if dpc:
        f.append(
            {"label": f"{int(dpc)} устройство(а) с ошибкой драйвера", "weight": min(dpc * 0.6, 1.8)}
        )

    # Supplementary from W4.2 OS-degradation engine (RSI-led, crash confirmation) and
    # disk-fill engine (servicing collapse → update failures → compounded instability).
    lo_os = _domain_lo(domain_values, "os_degradation_risk", 1.2)
    if lo_os > 0.01 and domain_values:
        v_raw = domain_values.get("os_degradation_risk", 0.0)
        f.append({"label": f"OS-degradation engine: {v_raw:.0f}/100", "weight": round(lo_os, 2)})

    lo_df = _domain_lo(domain_values, "disk_fill_risk", 0.4)
    if lo_df > 0.01 and domain_values:
        v_raw = domain_values.get("disk_fill_risk", 0.0)
        f.append(
            {
                "label": f"Disk-fill engine (обслуживание): {v_raw:.0f}/100",
                "weight": round(lo_df, 2),
            }
        )

    # ssd3 Ф4: software-aging engine (session-scoped handle/memory leak) --
    # same supplementary pattern as the other W4.2 engines above.
    lo_aging = _domain_lo(domain_values, "software_aging_risk", 0.8)
    if lo_aging > 0.01 and domain_values:
        v_raw = domain_values.get("software_aging_risk", 0.0)
        f.append(
            {
                "label": f"Software-aging engine (утечки): {v_raw:.0f}/100",
                "weight": round(lo_aging, 2),
            }
        )

    # ssd3 Ф3 (T3.3): "Application Hang" (1002) is deliberately excluded from
    # errchain's storage causal chain (server/analytics/errchain.py) -- it is
    # a generic instability symptom, scored here instead, alongside the BSOD
    # /driver-error counts above. Same scale-to-weight shape as _domain_lo;
    # capped low (0.6, reached at 10+ hangs/30d) since one or two hangs a
    # month is unremarkable noise, not a stability signal.
    if app_hang_count_30d:
        hang_lo = min(app_hang_count_30d / 10.0, 1.0) * 0.6
        f.append(
            {
                "label": f"{int(app_hang_count_30d)}x зависание приложения (30д)",
                "weight": round(hang_lo, 2),
            }
        )

    return _finish("stability", _BASE_PRIOR["stability"], f)


def compute_risk(
    inventory: Optional[dict],
    historical: Optional[dict],
    heartbeat: Optional[dict],
    *,
    domain_values: Optional[dict[str, Optional[float]]] = None,
    app_hang_count_30d: Optional[int] = None,
) -> dict[str, Any]:
    """Return per-class posteriors with explanations, sorted by probability.

    When domain_values is provided (W4.2 engine outputs, 0..100 scale), each
    applicable class receives a supplementary log-odds contribution from its
    corresponding domain engine — making this a thin prioritizer over the
    domain layer rather than a standalone signal processor (D5).

    overall is on the same 0..100 scale as risk_exposure and all W4.2 axes;
    class-level "probability" stays in 0..1 for the dashboard progress bars.
    """
    age = device_age_years(inventory)
    classes = [
        _storage(inventory, historical, heartbeat, age, domain_values),
        _power_thermal(historical, heartbeat, domain_values),
        _memory(historical, heartbeat),
        _stability(inventory, historical, domain_values, app_hang_count_30d),
    ]

    classes.sort(key=lambda c: c["probability"], reverse=True)
    top_prob = classes[0]["probability"] if classes else 0.0
    return {
        "classes": classes,
        "top": classes[0]["name"] if classes else None,
        # W4.3: 0..100 to match risk_exposure and all W4.2 domain axes.
        # class-level "probability" stays 0..1 for dashboard percentage bars.
        "overall": round(top_prob * 100, 1),
    }
