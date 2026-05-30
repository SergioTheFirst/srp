"""Acceptance scenarios from telemetry-trust-contract.md sec.14, exercised through
the pure trust core (derive_state -> compute_weight -> resolve_domain_trust)."""

from __future__ import annotations

import pytest
from server.trust import (
    CollectorStatus,
    DomainTrustState,
    SemanticStatus,
    SourceState,
    SourceTrust,
    compute_weight,
    derive_state,
    resolve_domain_trust,
    validate_source,
)

pytestmark = pytest.mark.unit


def _trust(source, collector, semantic, age=0.0, stale_after=300.0, applicable=True):
    state = derive_state(collector, semantic, age, stale_after, applicable)
    return SourceTrust(source, state, compute_weight(state), collector, semantic)


def test_disk_unavailable_yields_unknown_not_healthy():
    # Required SMART source blocked -> storage domain MUST be UNKNOWN, never healthy.
    sources = {
        "storage_reliability": _trust(
            "storage_reliability", CollectorStatus.BLOCKED, SemanticStatus.UNCHECKED
        )
    }
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.UNKNOWN


def test_thermal_fake_constant_is_suspect_and_zero_weight():
    semantic, _ = validate_source("throttle", {"value": 27.0}, last={"value": 27.0})
    t = _trust("throttle", CollectorStatus.OK, semantic)
    assert t.state is SourceState.SUSPECT
    assert t.weight == 0.0
    assert resolve_domain_trust("thermal", {"throttle": t}).state is DomainTrustState.UNKNOWN


def test_desktop_battery_is_not_applicable_no_noise():
    t = _trust("battery", CollectorStatus.ABSENT, SemanticStatus.UNCHECKED, applicable=False)
    d = resolve_domain_trust("battery", {"battery": t})
    assert d.state is DomainTrustState.NOT_APPLICABLE


def test_weight_cannot_reanimate_a_suspect_source():
    t = _trust("storage_reliability", CollectorStatus.OK, SemanticStatus.IMPLAUSIBLE)
    assert t.state is SourceState.SUSPECT
    assert t.weight == 0.0


def test_immaterial_signal_never_drives_suspect():
    semantic, _ = validate_source("cpu_pct", {"value": 9999.0}, last=None)
    assert semantic is SemanticStatus.UNCHECKED
