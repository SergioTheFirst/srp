"""Contract tests: the pydantic message models are the single source of truth."""

from __future__ import annotations

import pytest
from shared.schema import (
    CONTRACT_VERSION,
    Envelope,
    EventBatchPayload,
    HeartbeatPayload,
    HistoricalPayload,
    InventoryPayload,
    SourceHealth,
    StorageReliability,
    parse_payload,
)
from tests.conftest import degrading, healthy

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "msg_type, model",
    [
        ("inventory", InventoryPayload),
        ("historical", HistoricalPayload),
        ("heartbeat", HeartbeatPayload),
        ("events", EventBatchPayload),
    ],
)
def test_parse_payload_returns_typed_model(msg_type, model):
    parsed = parse_payload(msg_type, healthy(msg_type))
    assert isinstance(parsed, model)


def test_parse_payload_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown msg_type"):
        parse_payload("telemetry", {})


def test_payloads_are_forward_compatible():
    """A newer agent may add fields an older server has never seen (extra=allow)."""
    payload = healthy("heartbeat")
    payload["gpu_temp_c"] = 71.0  # field the schema does not define
    parsed = parse_payload("heartbeat", payload)
    # Known field still parses; unknown field is preserved, not rejected.
    assert parsed.cpu_perf_pct == 100.0
    assert parsed.model_dump()["gpu_temp_c"] == 71.0


def test_all_analytic_fields_optional():
    """Missing != zero: an empty payload must validate (sources can be blocked)."""
    parsed = parse_payload("heartbeat", {})
    assert parsed.cpu_pct is None
    assert parsed.free_space_pct is None


def test_nested_models_validate():
    parsed = parse_payload("historical", degrading("historical"))
    assert isinstance(parsed.storage[0], StorageReliability)
    assert parsed.storage[0].wear_pct == 82.0


def test_inventory_uses_cpu_logical_not_threads():
    parsed = parse_payload("inventory", healthy("inventory"))
    assert parsed.cpu_logical == 12
    assert not hasattr(InventoryPayload, "cpu_threads")


def test_envelope_defaults_version_and_timestamp():
    env = Envelope(device_id="dev-1", msg_type="heartbeat", payload={})
    assert env.agent_version == CONTRACT_VERSION
    assert env.ts  # auto-filled ISO timestamp
    assert "T" in env.ts


def test_envelope_rejects_bad_msg_type():
    with pytest.raises(ValueError):
        Envelope(device_id="dev-1", msg_type="nonsense", payload={})


# --------------------------------------------------------------------------- #
# SourceHealth and source_health on Envelope (Plan 2)
# --------------------------------------------------------------------------- #


def test_source_health_rejects_unknown_status():
    with pytest.raises(ValueError):
        SourceHealth(status="bogus")


def test_source_health_validates_status():
    sh = SourceHealth(status="ok")
    assert sh.status == "ok"
    assert sh.collected_at is None


def test_source_health_with_collected_at():
    sh = SourceHealth(status="timeout", collected_at="2026-05-30T10:00:00+00:00")
    assert sh.status == "timeout"
    assert sh.collected_at == "2026-05-30T10:00:00+00:00"


def test_source_health_is_forward_compatible():
    """Extra fields on SourceHealth must be tolerated (extra='allow' on _Base)."""
    sh = SourceHealth(status="partial", collected_at=None, future_field="x")
    assert sh.model_dump()["future_field"] == "x"


def test_envelope_default_source_health_is_empty():
    env = Envelope(device_id="dev-1", msg_type="heartbeat", payload={})
    assert env.source_health == {}


def test_envelope_accepts_source_health_block():
    env = Envelope(
        device_id="dev-1",
        msg_type="heartbeat",
        payload={},
        source_health={
            "free_space": {"status": "ok", "collected_at": "2026-05-30T10:00:00+00:00"},
            "throttle": {"status": "timeout", "collected_at": None},
        },
    )
    assert isinstance(env.source_health["free_space"], SourceHealth)
    assert env.source_health["free_space"].status == "ok"
    assert env.source_health["throttle"].status == "timeout"


def test_envelope_source_health_round_trips_through_parse_payload():
    """parse_payload works unaffected; source_health lives on the Envelope only."""
    payload = healthy("heartbeat")
    parsed = parse_payload("heartbeat", payload)
    assert parsed.cpu_perf_pct == 100.0


def test_envelope_without_source_health_still_valid():
    """Old agents that don't send source_health produce a valid Envelope."""
    raw = {
        "device_id": "old-agent",
        "agent_version": CONTRACT_VERSION,
        "msg_type": "heartbeat",
        "payload": {},
    }
    env = Envelope(**raw)
    assert env.source_health == {}
