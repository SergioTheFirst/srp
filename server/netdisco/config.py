"""Server-side netdisco configuration (mirrors PrinterConfig).

Every value has a safe default and discovery is OFF until ``enabled`` is an
explicit ``True`` -- the same secure-default stance as printer polling and the
ingest token. Intervals are clamped to a floor so no config can make the loop
hammer the network or the server. Active scanning / SNMP credentials arrive in
later phases (P5+); this phase only drives the no-probe inventory refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

_MIN_INTERVAL_SEC = 60  # never refresh faster than this, whatever the config says
_DEFAULT_INVENTORY_INTERVAL_SEC = 900
_DEFAULT_JITTER_SEC = 30


@dataclass(frozen=True)
class NetdiscoConfig:
    enabled: bool = False  # OFF until explicit True (secure default)
    inventory_interval_sec: int = _DEFAULT_INVENTORY_INTERVAL_SEC
    jitter_sec: int = _DEFAULT_JITTER_SEC  # de-phase the loop (anti-thundering-herd)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_netdisco_config(data: Optional[Mapping[str, Any]]) -> NetdiscoConfig:
    """Build a NetdiscoConfig from a raw mapping, clamping unsafe input."""
    d = data or {}
    interval = max(
        _MIN_INTERVAL_SEC,
        _as_int(d.get("inventory_interval_sec"), _DEFAULT_INVENTORY_INTERVAL_SEC),
    )
    jitter = max(0, _as_int(d.get("jitter_sec"), _DEFAULT_JITTER_SEC))
    return NetdiscoConfig(
        enabled=d.get("enabled") is True,
        inventory_interval_sec=interval,
        jitter_sec=jitter,
    )
