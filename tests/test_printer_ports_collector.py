"""Agent printer-port collector (printers phase 3).

The agent reads its own spooler config (Get-Printer / Get-PrinterPort) to learn
which network printers it prints to. Privacy contract: ONLY RFC1918 printer host
addresses leave the agent -- hostnames and public IPs are dropped. The hint is a
discovery seed (an IP to poll later), never trust/scoring telemetry, so the
collector emits NO source_health entry.
"""

from __future__ import annotations

import pytest
from client.collectors import printer_ports
from client.collectors.ps import PsResult
from shared.schema import PRINTER_PORTS_MAX, HistoricalPayload


@pytest.mark.unit
def test_rfc1918_host_becomes_hint():
    hints = printer_ports._hints_from({"ports": [{"name": "HP LJ", "host": "192.168.1.50"}]})
    assert hints == [{"name": "HP LJ", "ip": "192.168.1.50"}]


@pytest.mark.unit
def test_public_ip_host_dropped():
    hints = printer_ports._hints_from({"ports": [{"name": "x", "host": "8.8.8.8"}]})
    assert hints == []


@pytest.mark.unit
def test_hostname_host_dropped():
    # A non-literal host (DNS name) is not an RFC1918 address -> not emitted.
    hints = printer_ports._hints_from({"ports": [{"name": "x", "host": "PRN-FLOOR2"}]})
    assert hints == []


@pytest.mark.unit
def test_non_dict_and_empty_host_skipped():
    hints = printer_ports._hints_from(
        {"ports": ["nope", {"name": "y"}, {"host": ""}, {"host": "   "}]}
    )
    assert hints == []


@pytest.mark.unit
def test_duplicate_ip_deduped_keeps_first_name():
    hints = printer_ports._hints_from(
        {
            "ports": [
                {"name": "first", "host": "10.0.0.5"},
                {"name": "second", "host": "10.0.0.5"},
            ]
        }
    )
    assert hints == [{"name": "first", "ip": "10.0.0.5"}]


@pytest.mark.unit
def test_hints_capped():
    many = {
        "ports": [
            {"name": f"p{i}", "host": f"10.1.{i // 256}.{i % 256}"}
            for i in range(PRINTER_PORTS_MAX + 50)
        ]
    }
    hints = printer_ports._hints_from(many)
    assert len(hints) == PRINTER_PORTS_MAX


@pytest.mark.unit
def test_agent_cap_within_contract_cap():
    assert printer_ports._MAX_HINTS <= PRINTER_PORTS_MAX


@pytest.mark.unit
def test_long_name_clipped_to_contract_so_payload_is_not_422():
    # A pathological printer name must not 422 the WHOLE historical envelope:
    # the agent clips it to the contract length, mirroring the schema cap.
    hints = printer_ports._hints_from({"ports": [{"name": "P" * 500, "host": "10.0.0.7"}]})
    assert len(hints[0]["name"]) == 256
    HistoricalPayload(printer_ports=hints)  # passes the contract, no rejection


@pytest.mark.unit
def test_collect_success_emits_no_source_health(monkeypatch):
    data = {"ports": [{"name": "HP", "host": "192.168.0.20"}]}
    monkeypatch.setattr(printer_ports, "run_ps", lambda *a, **k: PsResult("ok", data))
    payload, health = printer_ports.collect_printer_ports()
    assert payload is not None
    assert payload["printer_ports"] == [{"name": "HP", "ip": "192.168.0.20"}]
    # Informational discovery: never a trust domain -> no source_health.
    assert health == {}
    HistoricalPayload(**payload)  # the hint payload passes the contract


@pytest.mark.unit
def test_collect_failure_returns_none_and_no_health(monkeypatch):
    monkeypatch.setattr(printer_ports, "run_ps", lambda *a, **k: PsResult("timeout"))
    payload, health = printer_ports.collect_printer_ports()
    assert payload is None
    assert health == {}
