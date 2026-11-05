"""Explainable Bayesian risk aggregation — thin prioritizer (W4.3, D5/D6).

W4.3 changes applied here:

  D6 — KP41 demoted to conditional enhancer: Kernel-Power 41 has near-zero
  specificity.  It fires on power cuts, driver hangs, RAM faults and clean
  reboots alike.  It may *amplify* a confirmed power/thermal anchor (CPU
  throttle under load, D10) but must not raise risk on its own.  Demoted
  weight: min(kp * 0.3, 1.0) vs old min(kp * 0.6, 2.5).

  D6 — WHEA removed from power_thermal and memory: corrected hardware errors
  in the WHEA Logger are predominantly firmware / ASPM / PCIe link-training
  noise; they do not reliably predict VRM, PSU, or RAM failure.  Keeping WHEA
  as a direct driver produced systematic false-positive "hardware error" alerts.

  D5 — compute_risk() accepts domain_values (W4.2 engine outputs, 0..100) as
  supplementary log-odds factors, making this a thin prioritizer *over* the
  domain engines rather than a standalone signal processor.

  Scale — overall returned on the same 0..100 scale as risk_exposure and all
  W4.2 axes; eliminates the two-scale confusion for the operator.

  D10 (health calibration) — honest inputs, no in-class double counting: SSD
  wear enters past 70 % and is capped at 1.5 log-odds; uncorrected I/O errors
  beat the ECC-inclusive totals; throttle and the KP41 anchor need a busy CPU;
  with an OS-engine value, stability drops its direct RSI/BSOD factors.

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

from server.scoring.scores import STALL_SEC, device_age_years, loaded_perf

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

# D10: SSD wear enters only past 70 % (the storage engine scores none below it) and
# is capped at 1.5 log-odds -- field studies find no monotonic hazard with wear
# (Schroeder FAST'16, Alter SC'19), so wear alone tops out at 'high', never 'critical'.
_WEAR_START = 70.0
_WEAR_MAX_LO = 1.5
_UNCORRECTED_IO = ("read_errors_uncorrected", "write_errors_uncorrected")
_TOTAL_IO = ("read_errors_total", "write_errors_total")


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


def _withheld(name: str) -> dict[str, Any]:
    """P0-5 (stoperrors.md): a gate-failed domain must never report a number --
    trust was previously attached only cosmetically, after a full probability
    was already computed on the same (untrusted) telemetry."""
    return {
        "name": name,
        "label": _CLASS_LABELS.get(name, name),
        "probability": None,
        "level": "unknown",
        "factors": [],
    }


def _domain_gated(name: str, domain_trust: Optional[dict[str, str]]) -> bool:
    """True iff *name*'s mapped domain is present and explicitly untrusted.

    domain_trust=None means no trust info was available to the caller at all
    (e.g. an old agent with no source_health) -- ungated, unchanged legacy
    behaviour. Once provided, anything other than "trusted" withholds.
    """
    return domain_trust is not None and domain_trust.get(name) != "trusted"


# --------------------------------------------------------------------------- #
def _io_errors(disk: dict) -> int:
    """D10: uncorrected read+write errors when the agent sent either of them.

    ReadErrorsTotal also counts ECC-corrected reads, so the totals are only the
    fallback for legacy agents that never report the uncorrected pair.
    """
    keys = _UNCORRECTED_IO if any(disk.get(k) is not None for k in _UNCORRECTED_IO) else _TOTAL_IO
    return sum(int(disk.get(k) or 0) for k in keys)


# H10: the storage class never ranks below this cycle's storage rung
# (health_dominant.storage_rung): the probability floor of each _level band.
_RUNG_FLOOR = {"h4": 0.50, "h3": 0.25, "h2": 0.10}


def _raw_smart(storage: list) -> list[Factor]:
    """Without a storage-engine value: the raw SMART fallback (wear, uncorrected I/O).
    With one, the engine already sums them -- counted once, like _stability_direct."""
    f: list[Factor] = []
    max_wear = max((s.get("wear_pct") or 0 for s in storage), default=0)
    io_err = sum(_io_errors(s) for s in storage)
    if max_wear > _WEAR_START:
        ramp = min((max_wear - _WEAR_START) / (100.0 - _WEAR_START), 1.0)
        f.append({"label": f"Износ SSD {max_wear:.0f}%", "weight": ramp * _WEAR_MAX_LO})
    if io_err > 0:
        f.append({"label": f"Накопленные ошибки I/O: {io_err}", "weight": 2.6})
    return f


def _age(age: Optional[float], storage: list) -> list[Factor]:
    """Device age and, without a storage-engine value, the disk's power-on hours: ONE
    context factor, at most 0.4 (H10). The engine scores POH itself -- counted once."""
    poh = max((s.get("power_on_hours") or 0 for s in storage), default=0)
    years = max(age or 0.0, poh / 8760.0)
    if years <= 0:
        return []
    what = "Возраст оборудования / наработка диска" if poh else "Возраст оборудования"
    return [{"label": f"{what} ~{years:.0f} лет (контекст)", "weight": min(years * 0.05, 0.4)}]


def _floor(factors: list[Factor], rung: Optional[str]) -> list[Factor]:
    """H10: lift the class to the probability floor of this cycle's storage rung; the
    lift is a labelled term, so the explanation still sums to the posterior."""
    floor = _RUNG_FLOOR.get(rung or "")
    if floor is None:
        return []
    gap = _logit(floor) - (_logit(_BASE_PRIOR["storage"]) + sum(f["weight"] for f in factors))
    if gap <= 0:
        return []
    return [{"label": "Пол по уровню накопителя в вердикте здоровья", "weight": gap + 1e-6}]


def _storage(
    inv: Optional[dict],
    hist: Optional[dict],
    hb: Optional[dict],
    age: Optional[float],
    domain_values: Optional[dict[str, Optional[float]]] = None,
    domain_trust: Optional[dict[str, str]] = None,
    rung: Optional[str] = None,
) -> dict[str, Any]:
    if _domain_gated("storage", domain_trust):
        return _withheld("storage")
    # P2-1: trust validation (validate_storage_item via pipeline._extract_reading)
    # only ever checks storage[0] -- aggregating raw values from disk[1+], which
    # are never trust-gated, would let a garbage/malicious reading on any disk
    # past the first influence risk without ever being validated. Limit
    # aggregation to the one disk that is actually validated.
    storage = ((hist or {}).get("storage") or [])[:1]
    # The W4.2 storage engine (SMART with latency-as-confirmation) at scale 1.0: a
    # 100/100 engine score adds +1.0 log-odds; raw SMART (POH too) only without its value.
    engine = _num(domain_values, "storage_risk")
    if engine is not None and math.isfinite(engine):
        f = _age(age, [])  # the engine scores POH itself: counted once (H10)
        lo = _domain_lo(domain_values, "storage_risk", 1.0)
        if lo > 0.01:
            f.append(
                {"label": f"Движок хранилища (SMART): {engine:.0f}/100", "weight": round(lo, 2)}
            )
    else:
        f = _age(age, storage) + _raw_smart(storage)
    lat = _num(hb, "disk_read_sec")
    if lat is not None and lat >= STALL_SEC:  # H9: an I/O stall, the only visible latency
        f.append({"label": f"Зависание чтения диска ≥ 1 с ({lat:.0f} с)", "weight": 0.9})
    return _finish("storage", _BASE_PRIOR["storage"], f + _floor(f, rung))


def _power_thermal(
    hist: Optional[dict],
    hb: Optional[dict],
    domain_values: Optional[dict[str, Optional[float]]] = None,
    domain_trust: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Power / thermal risk class.

    KP41 (W4.3 D6): conditional enhancer only.  Kernel-Power 41 fires on power
    cuts, driver hangs, RAM faults and clean reboots alike — specificity ≈ 0.
    It may amplify a throttle-under-load anchor but must not drive risk
    independently.  Reduced weight: min(kp * 0.3, 1.0).  D10: a dirty shutdown
    is no anchor — 6008 and KP41 are logged for the same unclean shutdown.

    WHEA (W4.3 D6): removed.  Corrected hardware errors are predominantly
    firmware / ASPM / PCIe link-training noise, not early-warning of VRM or PSU
    failure.  Keeping WHEA here produced systematic false-positive alerts.

    domain_values (W4.2 thermal engine): reserved for future integration with
    a dedicated thermal-monitoring engine (similar to _storage and _stability).
    Not yet wired up; currently always None. See cctodo.md/backlog for details.
    """
    if _domain_gated("power_thermal", domain_trust):
        return _withheld("power_thermal")
    f: list[Factor] = []

    perf = loaded_perf(hb)
    if perf is not None:
        if perf < 85:
            f.append({"label": f"Троттлинг CPU ({perf:.0f}% номинала)", "weight": 1.2})
        elif perf < 95:
            f.append({"label": f"Лёгкий троттлинг CPU ({perf:.0f}%)", "weight": 0.5})

    ds = _num(hist, "dirty_shutdowns_30d")
    if ds:
        f.append({"label": f"{int(ds)}x грязное выключение (6008)", "weight": min(ds * 0.4, 1.5)})

    # KP41 as conditional enhancer: only with a throttle-under-load anchor (D6, D10).
    kp = _num(hist, "kernel_power_41_30d")
    _has_anchor = perf is not None and perf < 85
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


def _stability_direct(
    inv: Optional[dict],
    hist: Optional[dict],
    domain_values: Optional[dict[str, Optional[float]]],
) -> list[Factor]:
    """Direct telemetry factors of the stability class.

    D10: RSI and BSOD are counted once.  With an OS-engine value its term already
    sums RSI, BSOD, 6008 and boot time, so the direct RSI/BSOD factors are skipped;
    without one (None or non-finite = UNKNOWN, as in _domain_lo) they stay as the
    fallback.
    """
    os_v = _num(domain_values, "os_degradation_risk")
    direct = os_v is None or not math.isfinite(os_v)
    f: list[Factor] = []
    rsi = _num(hist, "reliability_stability_index") if direct else None
    if rsi is not None:
        if rsi < 5:
            f.append({"label": f"Низкий индекс стабильности ({rsi:.1f}/10)", "weight": 1.5})
        elif rsi < 7:
            f.append({"label": f"Умеренный индекс стабильности ({rsi:.1f}/10)", "weight": 0.6})
    bc = _num(hist, "bugchecks_30d") if direct else None
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
    return f


def _stability(
    inv: Optional[dict],
    hist: Optional[dict],
    domain_values: Optional[dict[str, Optional[float]]] = None,
    app_hang_count_30d: Optional[int] = None,
    domain_trust: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    if _domain_gated("stability", domain_trust):
        return _withheld("stability")
    f = _stability_direct(inv, hist, domain_values)

    # Supplementary from W4.2 OS-degradation engine (RSI-led, crash confirmation) and
    # disk-fill engine (servicing collapse → update failures → compounded instability).
    lo_os = _domain_lo(domain_values, "os_degradation_risk", 1.2)
    if lo_os > 0.01 and domain_values:
        v_raw = domain_values.get("os_degradation_risk", 0.0)
        f.append({"label": f"Движок деградации OS: {v_raw:.0f}/100", "weight": round(lo_os, 2)})

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


_ALL_CLASS_NAMES = ("storage", "power_thermal", "memory", "stability")


def compute_risk(
    inventory: Optional[dict],
    historical: Optional[dict],
    heartbeat: Optional[dict],
    *,
    domain_values: Optional[dict[str, Optional[float]]] = None,
    app_hang_count_30d: Optional[int] = None,
    domain_trust: Optional[dict[str, str]] = None,
    device_untrusted: bool = False,
    storage_floor_state: Optional[str] = None,
) -> dict[str, Any]:
    """Return per-class posteriors with explanations, sorted by probability.

    When domain_values is provided (W4.2 engine outputs, 0..100 scale), each
    applicable class receives a supplementary log-odds contribution from its
    corresponding domain engine — making this a thin prioritizer over the
    domain layer rather than a standalone signal processor (D5).

    domain_trust (P0-5, stoperrors.md) is keyed by CLASS name (storage/
    power_thermal/stability -- "memory" has no trust domain in v1, see
    pipeline._CLASS_DOMAIN) and gates the corresponding class to
    probability=None/level="unknown" unless its state is exactly "trusted".
    None (not a dict) means no trust info was available -- ungated, matching
    pre-P0-5 behaviour for an old agent with no source_health.

    device_untrusted (P0-5 review follow-up) is the identity-level gate -- a
    strict superset of domain_trust: when true, EVERY class withholds,
    including memory (which has no domain to gate it individually). Without
    this, an untrusted-identity device still leaked a confident memory
    probability/overall/top -- the same "number beside trust:unknown" bug
    domain_trust exists to close, just one class over.

    overall is on the same 0..100 scale as risk_exposure and all W4.2 axes;
    class-level "probability" stays in 0..1 for the dashboard progress bars.
    overall/top are None when every class is withheld (never a fabricated
    0.0 read as "confirmed zero risk").
    """
    if device_untrusted:
        classes = [_withheld(name) for name in _ALL_CLASS_NAMES]
        return {"classes": classes, "top": None, "overall": None}

    age = device_age_years(inventory)
    classes = [
        _storage(
            inventory,
            historical,
            heartbeat,
            age,
            domain_values,
            domain_trust,
            storage_floor_state,
        ),
        _power_thermal(historical, heartbeat, domain_values, domain_trust),
        _memory(historical, heartbeat),
        _stability(inventory, historical, domain_values, app_hang_count_30d, domain_trust),
    ]

    # Withheld (probability=None) classes sort last -- never crash on None vs
    # float, never surface as "top" with a fabricated number (P0-5).
    classes.sort(key=lambda c: (c["probability"] is None, -(c["probability"] or 0.0)))
    top_class = classes[0] if classes and classes[0]["probability"] is not None else None
    return {
        "classes": classes,
        "top": top_class["name"] if top_class else None,
        # W4.3: 0..100 to match risk_exposure and all W4.2 domain axes.
        # class-level "probability" stays 0..1 for dashboard percentage bars.
        "overall": round(top_class["probability"] * 100, 1) if top_class else None,
    }
