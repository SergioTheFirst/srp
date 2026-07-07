"""Domain trust resolution (Tiered).

Each scoring domain declares required + optional sources. A required source that
fails the gate makes the whole domain UNKNOWN (never optimistic). An optional source
that fails is simply dropped; the domain stays TRUSTED on its required sources, at a
weight that reflects any degraded required source. A required NOT_APPLICABLE source
(e.g. battery on a desktop) makes the domain NOT_APPLICABLE -- not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from server.trust.states import SourceState, SourceTrust

DOMAIN_SOURCES: Dict[str, Dict[str, List[str]]] = {
    "storage": {"required": ["storage_reliability"], "optional": ["disk_latency", "smart"]},
    "battery": {"required": ["battery"], "optional": []},
    "disk_fill": {"required": ["free_space"], "optional": []},
    "os_stability": {"required": ["reliability"], "optional": []},
    "boot": {"required": ["boot_time"], "optional": []},
    "thermal": {"required": ["throttle"], "optional": []},
    # Phase 2: network feeds the network_risk axis -> it gates that axis only
    # (never the day-1 health axes; observability counts it as coverage).
    "network": {"required": ["network"], "optional": []},
}


class DomainTrustState(str, Enum):
    TRUSTED = "trusted"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class DomainTrust:
    domain: str
    state: DomainTrustState
    weight: float
    contributing: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    reason: str = ""


def resolve_domain_trust(domain: str, sources: Dict[str, SourceTrust]) -> DomainTrust:
    spec = DOMAIN_SOURCES.get(domain)
    if spec is None:
        return DomainTrust(domain, DomainTrustState.UNKNOWN, 0.0, reason="unknown domain")

    required_weights: List[float] = []
    contributing: List[str] = []
    dropped: List[str] = []

    for name in spec["required"]:
        st = sources.get(name)
        if st is None:
            return DomainTrust(
                domain,
                DomainTrustState.UNKNOWN,
                0.0,
                reason=f"required source {name} not reported",
            )
        if st.state is SourceState.NOT_APPLICABLE:
            return DomainTrust(
                domain,
                DomainTrustState.NOT_APPLICABLE,
                0.0,
                reason=f"{name} not applicable",
            )
        if not st.passes_gate:
            return DomainTrust(
                domain,
                DomainTrustState.UNKNOWN,
                0.0,
                reason=f"required source {name} is {st.state.value}",
            )
        required_weights.append(st.weight)
        contributing.append(name)

    for name in spec["optional"]:
        st = sources.get(name)
        if st is not None and st.passes_gate:
            contributing.append(name)
        elif st is not None:
            dropped.append(name)

    weight = min(required_weights) if required_weights else 0.0  # pragma: no cover
    return DomainTrust(domain, DomainTrustState.TRUSTED, weight, contributing, dropped)
