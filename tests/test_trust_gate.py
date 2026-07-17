from __future__ import annotations

import dataclasses

import pytest
from server.trust.gate import compute_weight, derive_state
from server.trust.states import (
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
)

pytestmark = pytest.mark.unit


def _state(collector, semantic, age=0.0, stale_after=300.0, applicable=True):
    return derive_state(collector, semantic, age, stale_after, applicable)


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
    t = SourceTrust("throttle", SourceState.OK, 1.0, CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.weight = 0.9  # frozen dataclass -> FrozenInstanceError


def test_not_applicable_wins_over_everything():
    # A sensor genuinely absent from this hardware: not a degradation, the
    # capability simply does not exist on this machine.
    s = _state(CollectorStatus.ABSENT, SemanticStatus.UNCHECKED, applicable=False)
    assert s is SourceState.NOT_APPLICABLE


def test_suspect_beats_collector_ok():
    # A fresh, complete, but lying source is more dangerous than an absent one.
    s = _state(CollectorStatus.OK, SemanticStatus.FROZEN)
    assert s is SourceState.SUSPECT


def test_collector_failure_is_unavailable():
    s = _state(CollectorStatus.BLOCKED, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.UNAVAILABLE


def test_old_sample_is_stale():
    s = _state(CollectorStatus.OK, SemanticStatus.PLAUSIBLE, age=9000.0, stale_after=300.0)
    assert s is SourceState.STALE


def test_partial_payload_is_degraded():
    s = _state(CollectorStatus.PARTIAL, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.DEGRADED


def test_clean_source_is_ok():
    s = _state(CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.OK


def test_weight_full_for_ok():
    assert compute_weight(SourceState.OK) == 1.0


def test_weight_attenuated_for_degraded():
    assert compute_weight(SourceState.DEGRADED) == 0.5


@pytest.mark.parametrize(
    "state",
    [SourceState.STALE, SourceState.UNAVAILABLE, SourceState.SUSPECT, SourceState.NOT_APPLICABLE],
)
def test_weight_zero_for_gate_fail(state):
    # Hard rule: weight never reanimates a gate-failed source.
    assert compute_weight(state) == 0.0
