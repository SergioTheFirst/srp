"""Gate derivation + weight. state = authoritative gate, weight = modulation only.

Precedence (first match wins): NOT_APPLICABLE -> SUSPECT (semantics beats collector)
-> UNAVAILABLE -> STALE -> DEGRADED -> OK. weight is computed ONLY for gate-pass
states; a gate-failed source gets 0.0 and can never be reanimated by weight.
"""

from __future__ import annotations

from typing import Optional

from server.trust.states import CollectorStatus, SemanticStatus, SourceState

_COLLECTOR_FAIL = frozenset(
    {
        CollectorStatus.EMPTY,
        CollectorStatus.TIMEOUT,
        CollectorStatus.BLOCKED,
        CollectorStatus.ABSENT,
    }
)
_SEMANTIC_SUSPECT = frozenset(
    {
        SemanticStatus.IMPLAUSIBLE,
        SemanticStatus.INCONSISTENT,
        SemanticStatus.FROZEN,
        SemanticStatus.KNOWN_BAD,
    }
)


def derive_state(
    collector_status: CollectorStatus,
    semantic_status: SemanticStatus,
    age_sec: Optional[float],
    stale_after_sec: Optional[float],
    applicable: bool = True,
) -> SourceState:
    if not applicable:
        return SourceState.NOT_APPLICABLE
    if semantic_status in _SEMANTIC_SUSPECT:
        return SourceState.SUSPECT
    if collector_status in _COLLECTOR_FAIL:
        return SourceState.UNAVAILABLE
    if stale_after_sec is not None and age_sec is not None and age_sec > stale_after_sec:
        return SourceState.STALE
    if collector_status == CollectorStatus.PARTIAL:
        return SourceState.DEGRADED
    return SourceState.OK


_DEGRADED_WEIGHT = 0.5  # single attenuation band; no continuous calculus (scope ceiling)


def compute_weight(state: SourceState) -> float:
    if state == SourceState.OK:
        return 1.0
    if state == SourceState.DEGRADED:
        return _DEGRADED_WEIGHT
    return 0.0  # gate-fail: weight is irrelevant and never reanimates
