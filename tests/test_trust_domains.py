from __future__ import annotations

import pytest
from server.trust.domains import DomainTrustState, resolve_domain_trust
from server.trust.states import CollectorStatus, SemanticStatus, SourceState, SourceTrust

pytestmark = pytest.mark.unit


def _src(name, state, weight):
    return SourceTrust(name, state, weight, CollectorStatus.OK, SemanticStatus.PLAUSIBLE)


def test_required_source_unavailable_makes_domain_unknown():
    sources = {"storage_reliability": _src("storage_reliability", SourceState.UNAVAILABLE, 0.0)}
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.UNKNOWN
    assert d.weight == 0.0


def test_required_source_missing_makes_domain_unknown():
    d = resolve_domain_trust("storage", {})  # nothing reported at all
    assert d.state is DomainTrustState.UNKNOWN


def test_battery_not_applicable_propagates():
    sources = {"battery": _src("battery", SourceState.NOT_APPLICABLE, 0.0)}
    d = resolve_domain_trust("battery", sources)
    assert d.state is DomainTrustState.NOT_APPLICABLE


def test_optional_source_failure_keeps_domain_trusted_but_drops_it():
    sources = {
        "storage_reliability": _src("storage_reliability", SourceState.OK, 1.0),
        "disk_latency": _src("disk_latency", SourceState.SUSPECT, 0.0),
    }
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.TRUSTED
    assert "disk_latency" in d.dropped
    assert d.contributing == ["storage_reliability"]


def test_degraded_required_lowers_domain_weight():
    sources = {"storage_reliability": _src("storage_reliability", SourceState.DEGRADED, 0.5)}
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.TRUSTED
    assert d.weight == 0.5
