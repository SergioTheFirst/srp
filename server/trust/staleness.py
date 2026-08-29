"""P2-2: periodic re-evaluation of per-source trust staleness.

``derive_state`` (server/trust/gate.py) has always had a STALE branch, but its
only caller (``pipeline.evaluate_trust``) only ever runs reactively, on ingest
of a NEW envelope -- so a source that silently stops reporting forever keeps
its last-known trust state frozen indefinitely. This module adds the missing
periodic half: re-evaluate every device_source_trust row's age against a
configured threshold, independent of ingest.

Design: docs/superpowers/specs/2026-07-22-trust-source-staleness-reeval-design.md

Split for testability (mirrors server/netdisco/reconcile.py::run_topology_cycle):
``reevaluate_staleness`` is a pure function (no I/O, no clock reads beyond the
*now* argument) that decides WHAT changed; ``run_staleness_cycle`` is a thin,
dependency-injectable orchestrator that reads, calls the pure function, writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from server import db
from server.trust.domains import DOMAIN_SOURCES
from server.trust.gate import compute_weight, derive_state
from server.trust.states import CollectorStatus, SemanticStatus

# Only sources that actually gate a domain (server.trust.domains.DOMAIN_SOURCES)
# have a natural reporting cadence to go stale against. Event-driven sources
# (print_jobs, events, identity, certificates) are excluded -- a device that
# simply had nothing to report would otherwise false-flag STALE for "nothing
# happened", not "gone silent" (design D1).
_DOMAIN_GATING_SOURCES = frozenset(
    src for spec in DOMAIN_SOURCES.values() for src in (*spec["required"], *spec["optional"])
)


@dataclass(frozen=True)
class StaleUpdate:
    """One device_source_trust row that must change state on this pass."""

    device_id: str
    source: str
    state: str
    weight: float
    reason: str
    # The evidence_seen_at this update was computed from -- carried through as an
    # optimistic-concurrency guard for the write (db.apply_source_staleness): if a
    # real ingest has since moved the clock, the stale write is safely dropped.
    evidence_seen_at: Optional[str]


def _age_sec(evidence_seen_at: Optional[str], now: datetime) -> Optional[float]:
    """Seconds between *evidence_seen_at* (server clock, ISO) and *now*.

    None on missing/unparseable input -- fail-closed: a row with no usable
    evidence timestamp is never aged, never flagged (nothing to age against).
    """
    if not evidence_seen_at:
        return None
    try:
        dt = datetime.fromisoformat(str(evidence_seen_at).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds()


def reevaluate_staleness(
    rows: list[dict[str, Any]],
    now: datetime,
    stale_after_sec: float,
) -> list[StaleUpdate]:
    """Pure: which device_source_trust rows should transition on this pass.

    Age is computed from ``evidence_seen_at`` (server clock at real ingest),
    never from the client-controlled ``ts`` (W0.2). A row whose stored state is
    already SUSPECT or UNAVAILABLE is naturally left alone: ``derive_state``'s
    own precedence ladder returns that same verdict again regardless of age, so
    no update is emitted -- this job only ever degrades OK/DEGRADED -> STALE,
    never revives a source (design D7). NOT_APPLICABLE is out of scope here, not
    an equally-safe case: it is gated by ``derive_state``'s separate
    ``applicable`` bool, which this job never sets to False and which is not
    reconstructable from a stored row -- but no production source currently
    produces a NOT_APPLICABLE row (battery, its only trigger, was removed), so
    this is dead code paths not colliding, not a live gap.
    """
    updates: list[StaleUpdate] = []
    for row in rows:
        if row.get("source") not in _DOMAIN_GATING_SOURCES:
            continue
        evidence_seen_at = row.get("evidence_seen_at")
        age_sec = _age_sec(evidence_seen_at, now)
        if age_sec is None:
            continue
        try:
            collector_status = CollectorStatus(row["collector_status"])
            semantic_status = SemanticStatus(row["semantic_status"])
        except ValueError:
            continue  # malformed row (forced-majeure) -- skip rather than guess
        new_state = derive_state(collector_status, semantic_status, age_sec, stale_after_sec)
        if new_state.value == row.get("state"):
            continue  # no change -- avoid write churn
        age_h = age_sec / 3600
        thr_h = stale_after_sec / 3600
        updates.append(
            StaleUpdate(
                device_id=row["device_id"],
                source=row["source"],
                state=new_state.value,
                weight=compute_weight(new_state),
                reason=f"источник молчит {age_h:.0f} ч (порог {thr_h:.0f} ч)",
                evidence_seen_at=evidence_seen_at,
            )
        )
    return updates


def run_staleness_cycle(
    stale_after_sec: float,
    *,
    get_rows: Callable[[], list[dict[str, Any]]] = db.get_source_trust_rows,
    write: Callable[[list[StaleUpdate]], int] = db.apply_source_staleness,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Read every device_source_trust row, re-evaluate, persist only the changed
    ones. All dependencies injectable (mirrors run_topology_cycle).

    *stale_after_sec* is floored (design D5): an operator misconfiguring 0 (or
    negative) must not flag every domain source in the fleet STALE on the very
    next cycle.
    """
    moment = now or datetime.now(timezone.utc)
    floor_sec = max(60.0, stale_after_sec)
    rows = get_rows()
    updates = reevaluate_staleness(rows, moment, floor_sec)
    applied = write(updates) if updates else 0
    return {"checked": len(rows), "updated": applied}
