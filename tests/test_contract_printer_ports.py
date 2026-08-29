"""Contract: additive-optional printer-port discovery hints (printers phase 3).

``printer_ports`` rides in the existing ``HistoricalPayload`` (no new msg_type,
no CONTRACT_VERSION bump). An old agent that never sends the field stays valid
(absent -> default []). ``max_length`` is the real ingest boundary: one inflated
hint list is rejected at validation instead of stored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.schema import (
    CONTRACT_VERSION,
    PRINTER_PORTS_MAX,
    HistoricalPayload,
    PrinterPortHint,
)

_HINT = {"name": "HP LaserJet", "ip": "192.168.1.50"}


@pytest.mark.unit
def test_contract_version_not_bumped():
    # printer_ports is additive/optional -> the contract version must not change.
    assert CONTRACT_VERSION == "0.1.0"


@pytest.mark.unit
def test_old_agent_without_field_is_valid_and_defaults_empty():
    payload = HistoricalPayload(reliability_stability_index=9.0)
    assert payload.printer_ports == []


@pytest.mark.unit
def test_new_agent_hints_parse():
    payload = HistoricalPayload(printer_ports=[_HINT])
    assert len(payload.printer_ports) == 1
    assert payload.printer_ports[0].ip == "192.168.1.50"
    assert payload.printer_ports[0].name == "HP LaserJet"


@pytest.mark.unit
def test_hint_fields_optional():
    # A bare hint (no fields) is structurally valid; emptiness is handled upstream.
    hint = PrinterPortHint()
    assert hint.ip is None and hint.name is None


@pytest.mark.unit
def test_payload_at_cap_is_valid():
    payload = HistoricalPayload(printer_ports=[_HINT] * PRINTER_PORTS_MAX)
    assert len(payload.printer_ports) == PRINTER_PORTS_MAX


@pytest.mark.unit
def test_payload_over_cap_is_rejected():
    with pytest.raises(ValidationError):
        HistoricalPayload(printer_ports=[_HINT] * (PRINTER_PORTS_MAX + 1))


@pytest.mark.unit
def test_oversized_hint_strings_rejected():
    with pytest.raises(ValidationError):
        PrinterPortHint(ip="1" * 65)
    with pytest.raises(ValidationError):
        PrinterPortHint(name="x" * 257)
