"""Generic netdisco driver: no vendor overlay until OIDs are hardware-verified.

Mirrors the printer ``standard`` driver. Vendor-specific OID maps stay EMPTY here
until verified on real hardware -- honesty over invented OIDs. The probe already
gathers everything standard (sysDescr/sysObjectID/serial/interfaces); a vendor
driver only adds model/serial overrides where the standard MIB is unhelpful.
"""

from __future__ import annotations

from typing import Dict, Optional

NAME = "standard"

# Empty until a real device is on the bench. Vendor drivers fill their own map.
VENDOR_OIDS: Dict[str, str] = {}


def read(session: object, *, sys_object_id: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Generic driver adds nothing beyond the standard probe -> empty extras."""
    return {}
