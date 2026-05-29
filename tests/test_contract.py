"""Contract tests: the pydantic message models are the single source of truth."""

from __future__ import annotations

import pytest
from shared.schema import (
    CONTRACT_VERSION,
    BatteryInfo,
    Envelope,
    EventBatchPayload,
    HeartbeatPayload,
    HistoricalPayload,
    InventoryPayload,
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
    assert isinstance(parsed.battery, BatteryInfo)
    assert parsed.battery.present is True
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
