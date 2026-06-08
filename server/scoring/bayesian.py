"""Explainable Bayesian risk aggregation (Part 3 C3.5).

Per failure class we work in log-odds: a prior (base rate adjusted for age /
media type / known-bad) plus an additive contribution per piece of evidence.
The posterior probability is sigmoid(prior + Σ evidence), and because every
term carries a label, the result is explainable *by construction* -- the
dashboard shows exactly which factors drove the number.

Weights here are hand-set and deliberately uncalibrated: the MVP has no failure
labels yet, so these are reasonable priors to be replaced by survival models +
isotonic calibration once the label loop produces ground truth (Part 3 C3.8).
"""

from __future__ import annotations

import math
from typing import Any, Optional

from server.scoring.scores import device_age_years

Factor = dict[str, Any]  # {"label": str, "weight": float}

_CLASS_LABELS = {
    "storage": "Накопитель (SSD/HDD)",
    "battery": "Батарея ноутбука",
    "power_thermal": "Питание / перегрев",
    "memory": "Память (RAM)",
    "stability": "Стабильность ОС/ПО",
}

_BASE_PRIOR = {
    "storage": 0.05,
    "battery": 0.04,
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
def _storage(inv, hist, hb, age) -> dict[str, Any]:
    f: list[Factor] = []
    if age:
        f.append({"label": f"Возраст оборудования ~{age:.0f} лет", "weight": min(age * 0.05, 0.8)})

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


def _battery(hist, age) -> Optional[dict[str, Any]]:
    bat = (hist or {}).get("battery")
    if not bat or not bat.get("present"):
        return None  # desktop: class not applicable
    f: list[Factor] = []
    if age:
        f.append({"label": f"Возраст ~{age:.0f} лет", "weight": min(age * 0.04, 0.6)})
    bw = bat.get("wear_pct")
    if bw is not None:
        bw = float(bw)
        if bw > 40:
            f.append({"label": f"Износ батареи {bw:.0f}% (риск вздутия)", "weight": 2.8})
        elif bw > 25:
            f.append({"label": f"Износ батареи {bw:.0f}%", "weight": 1.6})
        elif bw > 15:
            f.append({"label": f"Износ батареи {bw:.0f}%", "weight": 0.7})
    cyc = bat.get("cycle_count")
    if cyc is not None and float(cyc) > 800:
        f.append({"label": f"Циклов заряда: {int(cyc)}", "weight": 0.8})
    return _finish("battery", _BASE_PRIOR["battery"], f)


def _power_thermal(hist, hb) -> dict[str, Any]:
    f: list[Factor] = []
    kp = _num(hist, "kernel_power_41_30d")
    if kp:
        f.append(
            {
                "label": f"{int(kp)}x внезапное отключение (Kernel-Power 41)",
                "weight": min(kp * 0.6, 2.5),
            }
        )
    whea = _num(hist, "whea_errors_30d")
    if whea:
        f.append(
            {"label": f"{int(whea)}x аппаратная ошибка WHEA", "weight": 2.2 if whea > 10 else 1.5}
        )
    perf = _num(hb, "cpu_perf_pct")
    if perf is not None:
        if perf < 85:
            f.append({"label": f"Троттлинг CPU ({perf:.0f}% номинала)", "weight": 1.2})
        elif perf < 95:
            f.append({"label": f"Лёгкий троттлинг CPU ({perf:.0f}%)", "weight": 0.5})
    ds = _num(hist, "dirty_shutdowns_30d")
    if ds:
        f.append({"label": f"{int(ds)}x грязное выключение (6008)", "weight": min(ds * 0.4, 1.5)})
    return _finish("power_thermal", _BASE_PRIOR["power_thermal"], f)


def _memory(hist, hb) -> dict[str, Any]:
    f: list[Factor] = []
    whea = _num(hist, "whea_errors_30d")
    if whea:
        f.append({"label": f"Коррект. ошибки (WHEA {int(whea)}) — могут быть RAM", "weight": 1.0})
    bc = _num(hist, "bugchecks_30d")
    if bc:
        f.append({"label": f"{int(bc)}x BSOD — часть приходится на память", "weight": 1.2})
    pf = _num(hb, "pagefile_pct")
    avail = _num(hb, "mem_avail_mb")
    if pf is not None and avail is not None and pf > 80 and avail < 1024:
        f.append({"label": "Тразинг: pagefile>80% при низкой свободной RAM", "weight": 0.8})
    return _finish("memory", _BASE_PRIOR["memory"], f)


def _stability(inv, hist) -> dict[str, Any]:
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
    return _finish("stability", _BASE_PRIOR["stability"], f)


def compute_risk(
    inventory: Optional[dict],
    historical: Optional[dict],
    heartbeat: Optional[dict],
) -> dict[str, Any]:
    """Return per-class posteriors with explanations, sorted by probability."""
    age = device_age_years(inventory)
    classes = [
        _storage(inventory, historical, heartbeat, age),
        _power_thermal(historical, heartbeat),
        _memory(historical, heartbeat),
        _stability(inventory, historical),
    ]
    battery = _battery(historical, age)
    if battery is not None:
        classes.append(battery)

    classes.sort(key=lambda c: c["probability"], reverse=True)
    return {
        "classes": classes,
        "top": classes[0]["name"] if classes else None,
        "overall": classes[0]["probability"] if classes else 0.0,
    }
