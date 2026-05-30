from __future__ import annotations

import pytest
from server.trust.states import (
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
)

pytestmark = pytest.mark.unit


def test_gate_pass_only_for_ok_and_degraded():
    ok = SourceTrust(
        "storage_reliability", SourceState.OK, 1.0, CollectorStatus.OK, SemanticStatus.PLAUSIBLE
    )
    degraded = SourceTrust(
        "free_space", SourceState.DEGRADED, 0.5, CollectorStatus.PARTIAL, SemanticStatus.PLAUSIBLE
    )
    suspect = SourceTrust(
        "throttle", SourceState.SUSPECT, 0.0, CollectorStatus.OK, SemanticStatus.FROZEN
    )
    assert ok.passes_gate is True
    assert degraded.passes_gate is True
    assert suspect.passes_gate is False


def test_source_trust_is_immutable():
    import dataclasses

    t = SourceTrust("battery", SourceState.OK, 1.0, CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.weight = 0.9  # frozen dataclass -> FrozenInstanceError
