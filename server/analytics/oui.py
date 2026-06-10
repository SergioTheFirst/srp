"""OUI -> vendor seed lookup for the network map (Phase 2, D3).

A deliberately tiny, high-confidence seed (virtualisation + Raspberry Pi) in the
spirit of _KNOWN_BAD_FIRMWARE: a hook, not a platform. The real fleet list is
curated out-of-band; an unknown OUI honestly returns None -- UNKNOWN over an
invented vendor name. Keys are the first three MAC octets, normalised "AA-BB-CC".
"""

from __future__ import annotations

import re
from typing import Optional

_NON_HEX = re.compile(r"[^0-9A-F]")

_VENDOR_SEED: dict[str, str] = {
    "00-50-56": "VMware",
    "00-0C-29": "VMware",
    "00-05-69": "VMware",
    "00-15-5D": "Microsoft Hyper-V",
    "08-00-27": "VirtualBox",
    "52-54-00": "QEMU/KVM",
    "B8-27-EB": "Raspberry Pi",
    "DC-A6-32": "Raspberry Pi",
    "E4-5F-01": "Raspberry Pi",
}


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """Uppercase dash-separated AA-BB-CC-DD-EE-FF, or None when not a MAC."""
    if not mac:
        return None
    digits = _NON_HEX.sub("", mac.upper())
    if len(digits) != 12:
        return None
    return "-".join(digits[i : i + 2] for i in range(0, 12, 2))


def vendor_for_mac(mac: Optional[str]) -> Optional[str]:
    norm = normalize_mac(mac)
    if norm is None:
        return None
    return _VENDOR_SEED.get(norm[:8])
