"""Trust state model: the authoritative gate + modulation weight per source.

A source is classified by two orthogonal inputs -- collector_status (did we get
the data?) and semantic_status (is the data believable?). From them we derive a
SourceState (the gate) and a weight (modulation, meaningful only when the gate
passes). The weight can never reanimate a gate-failed source.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CollectorStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    ABSENT = "absent"


class SemanticStatus(str, Enum):
    PLAUSIBLE = "plausible"
    IMPLAUSIBLE = "implausible"
    INCONSISTENT = "inconsistent"
    FROZEN = "frozen"
    KNOWN_BAD = "known_bad"
    UNCHECKED = "unchecked"


class SourceState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    SUSPECT = "suspect"
    NOT_APPLICABLE = "not_applicable"


GATE_PASS = frozenset({SourceState.OK, SourceState.DEGRADED})


@dataclass(frozen=True)
class SourceTrust:
    source: str
    state: SourceState
    weight: float  # [0..1]; meaningful only when passes_gate
    collector_status: CollectorStatus
    semantic_status: SemanticStatus
    age_sec: Optional[float] = None
    reason: Optional[str] = None

    @property
    def passes_gate(self) -> bool:
        return self.state in GATE_PASS
