"""Disk-card view helpers (S13/X5, 2026-11-02): display-only. A colour here always copies
a cut-off the engine scores (server/analytics/storage.py); no threshold of its own."""

from __future__ import annotations

from typing import Any, Optional

from server.scoring.score100 import band_for_risk_score

# S13: short names for the agent's ATA whitelist (client/collectors/smart.py; a test pins the
# key set). Vendor-specific ids keep a neutral name: makers disagree on what they count.
SMART_ATTR_RU = {
    "1": "частота ошибок чтения (raw — формат производителя)",
    "5": "переназначенные секторы",
    "9": "наработка (обычно часы)",
    "10": "повторы раскрутки",
    "175": "вендорский атрибут",
    "177": "вендорский атрибут",
    "181": "вендорский атрибут",
    "182": "вендорский атрибут",
    "184": "ошибки сквозного контроля данных",
    "187": "неисправимые ошибки (сообщённые)",
    "188": "таймауты команд",
    "194": "температура (обычно °C)",
    "196": "события переназначения",
    "197": "секторы в ожидании переназначения",
    "198": "неисправимые секторы",
    "199": "ошибки CRC интерфейса (кабель/разъём)",
    "231": "вендорский атрибут",
    "233": "вендорский атрибут",
    "235": "вендорский атрибут",
    "241": "вендорский (обычно объём записи)",
    "242": "вендорский (обычно объём чтения)",
}

# X5: Windows' counter left temperature/POH empty (RAID, some SATA) while the raw SMART value
# is there. These gate the DECODE, not risk: the agent masks 194 to 16 bits (plain degC in
# byte 0, byte 1 zero), so 5..90 also rejects 10xCelsius and packed layouts; attr 9 is hours
# on most models (fleet 2026-11: equals Windows' own POH on 259/259 SATA rows) but minutes on
# a few legacy ones -- hence the card's «по SMART» marker and neutral colour.
_SMART_PLAUSIBLE = {"194": (5.0, 90.0), "9": (1.0, 200_000.0)}


def smart_fallback(disk: Any, attr_id: str) -> Optional[float]:
    """Raw SMART attr 194 / 9 as degC / hours when plausible, else None (shown, never scored)."""
    try:
        value = float(disk["smart_attrs"][attr_id])
    except (KeyError, TypeError, ValueError):
        return None
    lo, hi = _SMART_PLAUSIBLE[attr_id]
    return value if lo <= value <= hi else None


# storage.py _score_disk Critical Warning hits -- label and points verbatim (a test pins both
# to the engine); the chip colour is the engine's own band of those points.
_NVME_CW_BITS = (
    (0b00001, "NVMe: резерв компенсации ниже порога", 70),
    (0b00010, "NVMe: критическая температура", 30),
    (0b00100, "NVMe: надёжность подсистемы деградировала", 70),
    (0b01000, "NVMe: диск в режиме только для чтения", 80),
    (0b10000, "NVMe: сбой резервного питания энергозависимой памяти", 40),
)
_BAND_CHIP = {"good": "good", "watch": "warn", "bad": "bad"}


def nvme_cw_flags(raw: Any) -> list[tuple[str, str]]:
    """(label, chip colour) per set Critical Warning bit, parsed int-only like the engine."""
    cw = int(raw) if isinstance(raw, (int, float)) else 0
    return [(lab, _BAND_CHIP[band_for_risk_score(p)]) for bit, lab, p in _NVME_CW_BITS if cw & bit]


def disk_spare_color(spare: Optional[float], threshold: Optional[float]) -> str:
    """storage.py: nvme_spare_pct below nvme_spare_threshold_pct is the 70-point R hit."""
    if spare is None or threshold is None:
        return "na"
    return "bad" if spare < threshold else "good"
