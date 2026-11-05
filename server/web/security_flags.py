"""Чипы гигиены безопасности для карточки ПК (агент 0.3.8, «маленькие победы»).

Чистая функция от inventory + последнего heartbeat. null = «неизвестно» -> чипа нет
(UNKNOWN не выдаётся за проблему). В скоринг не входит: это гигиена, не механизм отказа.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

UPTIME_WARN_DAYS = 30
PATCH_WARN_DAYS = 60


def _patch_age_days(value: Any, today: date) -> Optional[int]:
    try:
        return (today - date.fromisoformat(str(value))).days
    except ValueError:
        return None


def _uptime_flag(hb: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    hours = (hb or {}).get("uptime_hours")
    if not isinstance(hours, (int, float)) or hours < UPTIME_WARN_DAYS * 24:
        return []
    days = int(hours // 24)
    return [_chip("warn", f"без перезагрузки {days} дн", "Давно не перезагружался")]


def _patch_flag(inv: dict[str, Any], today: date) -> list[dict[str, str]]:
    age = _patch_age_days(inv.get("last_patch_date"), today) if inv.get("last_patch_date") else None
    if age is None or age < PATCH_WARN_DAYS:
        return []
    return [
        _chip(
            "warn",
            f"обновлений Windows не было {age} дн",
            f"Последнее установленное обновление: {inv['last_patch_date']}",
        )
    ]


def _av_flag(inv: dict[str, Any]) -> list[dict[str, str]]:
    av = inv.get("antivirus")
    if not isinstance(av, list):
        return []  # None = неизвестно; мусор из строк до валидации 0.3.8 -- тоже
    av = [p for p in av if isinstance(p, dict)]
    if not av:
        return [_chip("bad", "антивирус не найден", "Windows не видит ни одного антивируса")]
    if not any(p.get("enabled") for p in av):
        return [_chip("bad", "антивирус выключен", "Ни один антивирус не включён")]
    if any(p.get("enabled") and p.get("up_to_date") is False for p in av):
        return [_chip("warn", "базы антивируса устарели", "Включённый антивирус со старыми базами")]
    return []


def _bitlocker_flag(inv: dict[str, Any]) -> list[dict[str, str]]:
    if inv.get("bitlocker_system") != "off":
        return []
    level = "warn" if inv.get("chassis") == "laptop" else "accent"
    return [_chip(level, "диск не зашифрован", "BitLocker выключен на системном диске")]


def _win11_flag(inv: dict[str, Any]) -> list[dict[str, str]]:
    tpm, ver, sb = inv.get("tpm_present"), inv.get("tpm_version"), inv.get("secure_boot")
    no_tpm2 = tpm is False or (tpm is True and ver is not None and not str(ver).startswith("2"))
    if not (no_tpm2 or sb is False):
        return []
    why = ", ".join(
        part
        for part, bad in (("нет TPM 2.0", no_tpm2), ("Secure Boot выключен", sb is False))
        if bad
    )
    return [_chip("accent", "не готов к Windows 11", why)]


def _activation_flag(inv: dict[str, Any]) -> list[dict[str, str]]:
    if inv.get("windows_activated") is not False:
        return []
    return [_chip("warn", "Windows не активирована", "Нет действующей лицензии Windows")]


def _chip(level: str, text: str, title: str) -> dict[str, str]:
    return {"level": level, "text": text, "title": title}


def security_flags(
    inv: Optional[dict[str, Any]], hb: Optional[dict[str, Any]], today: Optional[date] = None
) -> list[dict[str, str]]:
    """Чипы {level: bad|warn|accent (CSS-класс .chip), text, title}.

    Пусто, если всё в порядке или неизвестно."""
    if not inv:
        return _uptime_flag(hb)
    today = today or date.today()
    return [
        *_av_flag(inv),
        *_patch_flag(inv, today),
        *_uptime_flag(hb),
        *_activation_flag(inv),
        *_bitlocker_flag(inv),
        *_win11_flag(inv),
    ]
