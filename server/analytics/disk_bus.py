"""Which disk rows may feed the verdict (0.3.4): a positively identified removable
disk is shown on the device card, never scored. Split out of storage.py (800-line
cap); every reader imports it from here."""

from __future__ import annotations

from typing import Any

# 0.3.4: hot-plug buses -- Get-PhysicalDisk BusType / Win32_DiskDrive InterfaceType, English
# names from Windows' own Storage.types.ps1xml (not localized). Only a POSITIVELY identified
# removable disk is left out; a missing/unknown bus counts as before (older agents).
# ponytail: SD/MMC stay counted on purpose -- built-in eMMC system disks enumerate there.
# Thunderbolt NVMe / eSATA enclosures report an internal bus and still count; add a
# per-row is_system/is_boot flag if either ever matters.
REMOVABLE_BUSES = frozenset({"USB", "1394"})


def is_removable_disk(disk: Any) -> bool:
    """A USB/FireWire disk: shown on the device card, never scored."""
    if not isinstance(disk, dict):
        return False
    bus = disk.get("bus_type") or disk.get("interface")
    return isinstance(bus, str) and bus.upper() in REMOVABLE_BUSES


def fixed_disks(disks: Any) -> list[dict[str, Any]]:
    """The disk rows that may feed the verdict: dict rows that are not removable."""
    return [d for d in disks or [] if isinstance(d, dict) and not is_removable_disk(d)]


# S2 (2026-11-02): MSFT_StorageReliabilityCounter sends an unpopulated Wear/Temperature as 0,
# not NULL -- fleet: wear 0 in 2513/2513 rows on every bus, temperature 0 on every RAID row.
# As SHOWN and TRENDED such a 0 is no reading; the engine keeps the raw row on purpose (its
# presence feeds H1/H5, its rules never fire on 0). A real NVMe wear rides
# nvme_percentage_used, and no running disk sits at 0 degC.
_UNPOPULATED_ZERO = ("wear_pct", "temperature_c")


def _is_zero(value: Any) -> bool:
    try:
        return float(value) == 0  # a numeric string too: the card coerces it with |float
    except (TypeError, ValueError):
        return False


def disk_reading(disk: dict[str, Any]) -> dict[str, Any]:
    """The disk row as shown and trended: the counter's unpopulated zeros read None."""
    return {**disk, **{k: None for k in _UNPOPULATED_ZERO if _is_zero(disk.get(k))}}
