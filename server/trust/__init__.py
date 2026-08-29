"""Pure server-side telemetry-trust core (see telemetry-trust-contract.md)."""

from server.trust.domains import DOMAIN_SOURCES, DomainTrust, DomainTrustState, resolve_domain_trust
from server.trust.gate import compute_weight, derive_state
from server.trust.staleness import StaleUpdate, reevaluate_staleness, run_staleness_cycle
from server.trust.states import (
    GATE_PASS,
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
)
from server.trust.validators import MATERIAL_SOURCES, validate_source

__all__ = [
    "GATE_PASS",
    "CollectorStatus",
    "SemanticStatus",
    "SourceState",
    "SourceTrust",
    "derive_state",
    "compute_weight",
    "validate_source",
    "MATERIAL_SOURCES",
    "DOMAIN_SOURCES",
    "DomainTrust",
    "DomainTrustState",
    "resolve_domain_trust",
    "StaleUpdate",
    "reevaluate_staleness",
    "run_staleness_cycle",
]
