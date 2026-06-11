"""Logical telemetry source names + per-source health helpers (Plan 2).

Source names MUST match ``server/trust/domains.py`` ``DOMAIN_SOURCES`` so the
server can key collector-status to the right trust domain. Pure stdlib.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

# Logical source names (keys in Envelope.source_health), aligned with
# server/trust/domains.py DOMAIN_SOURCES.
STORAGE_RELIABILITY = "storage_reliability"
DISK_LATENCY = "disk_latency"
BATTERY = "battery"
FREE_SPACE = "free_space"
RELIABILITY = "reliability"
BOOT_TIME = "boot_time"
THROTTLE = "throttle"
IDENTITY = "identity"
EVENTS = "events"
CERTIFICATES = "certificates"
PRINT_JOBS = "print_jobs"
NETWORK = "network"  # Phase 2: trust domain gating the network_risk axis


class CollectorResult(NamedTuple):
    """What a collector returns: a payload (None on failure) + per-source health."""

    payload: Optional[dict[str, Any]]
    source_health: dict[str, dict[str, Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def health(status: str) -> dict[str, Any]:
    """One source_health entry: a collector status stamped with the collection time."""
    return {"status": status, "collected_at": _now_iso()}


def field_status(present: bool, complete: bool = True) -> str:
    """Map field presence to a collector status: ok / partial / empty."""
    if not present:
        return "empty"
    return "ok" if complete else "partial"


def failed(sources: list[str], status: str) -> dict[str, dict[str, Any]]:
    """Mark every owned source with the same failure status (run_ps returned no data)."""
    return {s: health(status) for s in sources}
