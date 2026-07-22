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
        hit(f"Троттлинг CPU ({perf:.0f}% номинала)", -_clamp((95 - perf) * 1.5, 0, 30))

    avail = _num(hb, "mem_avail_mb")
    if avail is not None:
        if avail < 512:
            hit("Критически мало свободной RAM (<512 МБ)", -20)
        elif avail < 1024:
            hit("Мало свободной RAM (<1 ГБ)", -12)
        elif avail < 2048:
            hit("Дефицит памяти (<2 ГБ свободно)", -6)

    pf = _num(hb, "pagefile_pct")
    if pf is not None:
        if pf > 80:
            hit("Активное использование файла подкачки (трэшинг)", -15)
        elif pf > 50:
            hit("Повышенное использование файла подкачки", -8)

    for key, label in (("disk_read_sec", "чтения"), ("disk_write_sec", "записи")):
        lat = _num(hb, key)
        if lat is not None:
            if lat > 0.05:
                hit(f"Высокая задержка {label} диска ({lat * 1000:.0f} мс)", -15)
            elif lat > 0.020:
                hit(f"Повышенная задержка {label} диска ({lat * 1000:.0f} мс)", -8)

    boot = _num(hist, "avg_boot_ms")
    if boot is not None:
        if boot > 90000:
            hit(f"Очень медленная загрузка ({boot / 1000:.0f} с)", -15)
        elif boot > 60000:
            hit(f"Медленная загрузка ({boot / 1000:.0f} с)", -10)
        elif boot > 40000:
            hit(f"Загрузка дольше целевой ({boot / 1000:.0f} с)", -5)

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
            hit(f"Очень низкий индекс стабильности Windows ({rsi:.1f}/10)", -25)
        elif rsi < 5:
            hit(f"Низкий индекс стабильности ({rsi:.1f}/10)", -15)
        elif rsi < 7:
            hit(f"Умеренный индекс стабильности ({rsi:.1f}/10)", -7)

    kp = _num(hist, "kernel_power_41_30d")
    if kp:
        hit(f"{int(kp)}x внезапное отключение (KP41, 30д)", -_clamp(kp * 7, 0, 35))

    bc = _num(hist, "bugchecks_30d")
    if bc:
        hit(f"{int(bc)}x BSOD (BugCheck 1001, 30д)", -_clamp(bc * 12, 0, 40))

    ds = _num(hist, "dirty_shutdowns_30d")
    if ds:
        hit(f"{int(ds)}x грязное выключение (6008, 30д)", -_clamp(ds * 5, 0, 25))

    ac = _num(hist, "app_crashes_30d")
    if ac:
        hit(f"{int(ac)}x падение приложений (1000, 30д)", -_clamp(ac * 1, 0, 10))

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
        hit(f"Износ SSD {max_wear:.0f}%", -_clamp(max_wear, 0, 70))
    if max_realloc:
        hit(
            f"Переназначено секторов: {int(max_realloc)} (HDD)",
            -40 if max_realloc > 100 else -20,
        )
    if max_poh is not None:
        if max_poh > 40000:
            hit(f"Наработка диска {max_poh / 1000:.0f}k ч", -15)
        elif max_poh > 25000:
            hit(f"Наработка диска {max_poh / 1000:.0f}k ч", -8)

    age = device_age_years(inv)
    if age is not None:
        if age > 7:
            hit(f"Возраст оборудования ~{age:.0f} лет", -15)
        elif age > 5:
            hit(f"Возраст оборудования ~{age:.0f} лет", -8)
        elif age > 3:
            hit(f"Возраст оборудования ~{age:.0f} лет", -3)

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
            hit(f"Системный диск: {free:.0f}% свободно (риск каскада)", 30)
        elif free < 10:
            hit(f"Системный диск: {free:.0f}% свободно", 18)
        elif free < 15:
            hit(f"Системный диск: {free:.0f}% свободно", 8)

    whea = _num(hist, "whea_errors_30d")
    if whea:
        hit(
            f"{int(whea)}x скорректированная аппаратная ошибка (WHEA, 30д)",
            25 if whea > 10 else 12,
        )

    if inv and inv.get("pending_reboot"):
        hit("Ожидается перезагрузка (обновления зависли)", 10)

    dpc = _num(inv, "driver_problem_count")
    if dpc:
        hit(f"{int(dpc)} устройство(а) с ошибкой драйвера", _clamp(dpc * 5, 0, 20))

    storage = (hist or {}).get("storage") or []
    disk_errors = 0
    for s in storage:
        disk_errors += int(s.get("read_errors_total") or 0)
        disk_errors += int(s.get("write_errors_total") or 0)
    if disk_errors > 0:
        hit(f"Накопленные ошибки I/O диска: {disk_errors}", 25)

    nic = _num(hb, "nic_errors")
    if nic and nic > 0:
        hit("Ошибки пакетов NIC", 5)

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
