"""Неизменяемые модели телеметрии принтера.

Сентинелы Printer-MIB (-1 other, -2 unknown, -3 some-remaining) → None: под
неопределённостью отдаём UNKNOWN, а не выдуманное число. percent считается
только когда И уровень, И максимум известны и max > 0.
"""

from dataclasses import dataclass
from typing import Optional, Tuple


def _clean(raw: Optional[int]) -> Optional[int]:
    """Отрицательные сентинелы Printer-MIB / None → None (UNKNOWN)."""
    if raw is None or raw < 0:
        return None
    return raw


def _percent(level: Optional[int], maximum: Optional[int]) -> Optional[int]:
    if level is None or maximum is None or maximum <= 0:
        return None
    return max(0, min(100, round(level * 100 / maximum)))


@dataclass(frozen=True)
class Supply:
    """Расходник (тонер/чернила/барабан/контейнер отработки)."""

    name: str
    type: str
    class_: Optional[str]  # consumed (тонер/чернила) vs receptacle (отработка) vs None
    level: Optional[int]
    max: Optional[int]
    percent: Optional[int]
    unit: Optional[int]

    @classmethod
    def from_snmp(
        cls,
        *,
        name: str,
        type: str,
        level: Optional[int],
        max: Optional[int],
        unit: Optional[int] = None,
        class_: Optional[str] = None,
    ) -> "Supply":
        lvl = _clean(level)
        mx = _clean(max)
        return cls(
            name=name,
            type=type,
            class_=class_,
            level=lvl,
            max=mx,
            percent=_percent(lvl, mx),
            unit=unit,
        )


@dataclass(frozen=True)
class Tray:
    """Лоток подачи бумаги."""

    name: str
    media: Optional[str]
    level: Optional[int]
    max: Optional[int]
    status: Optional[int]

    @classmethod
    def from_snmp(
        cls,
        *,
        name: str,
        media: Optional[str],
        level: Optional[int],
        max: Optional[int],
        status: Optional[int] = None,
    ) -> "Tray":
        return cls(name=name, media=media, level=_clean(level), max=_clean(max), status=status)


@dataclass(frozen=True)
class PrinterError:
    """Активная ошибка/предупреждение принтера (из prtAlert / hrPrinterDetectedError)."""

    code: Optional[int]  # None, если prtAlertCode отсутствовал
    description: str


@dataclass(frozen=True)
class PrinterReading:
    """Снимок телеметрии одного принтера. Неизвестное поле → None / пустой кортеж."""

    ip: str
    hostname: Optional[str] = None
    mac: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    firmware: Optional[str] = None
    uptime: Optional[int] = None
    status: Optional[str] = None
    total_pages: Optional[int] = None
    color_pages: Optional[int] = None
    mono_pages: Optional[int] = None
    duplex_pages: Optional[int] = None
    supplies: Tuple[Supply, ...] = ()
    trays: Tuple[Tray, ...] = ()
    errors: Tuple[PrinterError, ...] = ()
    source_protocol: str = "snmp"


def _norm_serial(serial: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in serial.strip())
    return slug.strip("-")[:48]


def _norm_mac(mac: str) -> str:
    return "".join(c for c in mac if c.isalnum()).upper()[:32]


def printer_identity(*, serial: Optional[str], mac: Optional[str], ip: Optional[str]) -> str:
    """Стабильная идентичность принтера: серийник > MAC > IP.

    Совпадает с приоритетом дедупликации discovery. Серийник переживает смену IP
    (DHCP); MAC нормализуется (регистр/разделители), чтобы один сетевой адаптер
    всегда давал один id. Ничего не совпало → 'prn-unknown'.
    """
    if serial and serial.strip():
        norm = _norm_serial(serial)
        if norm:
            return "prn-sn-" + norm
    if mac and mac.strip():
        norm = _norm_mac(mac)
        if norm:
            return "prn-mac-" + norm
    if ip and ip.strip():
        return "prn-ip-" + ip.strip()
    return "prn-unknown"
