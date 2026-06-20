"""W1.3b — live dashboard shell, /fleet/fragment partial, and ack-from-list flow."""

from __future__ import annotations

import pytest
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _env(device_id: str, msg_type: str, payload: dict) -> dict:
    return dict(envelope(device_id, msg_type, payload))


def test_fleet_shell_has_kpis_live_and_poll(client):
    client.post("/api/v1/ingest", json=_env("d1", "inventory", healthy("inventory")))
    r = client.get("/")
    assert r.status_code == 200
    assert "в зоне риска" in r.text  # KPI card
    assert "/fleet/fragment" in r.text  # JS polls the fragment endpoint
    assert "обновлено" in r.text  # live indicator
    assert "Объект:" in r.text  # site grouping header


def test_fleet_fragment_is_a_partial(client):
    client.post("/api/v1/ingest", json=_env("d2", "inventory", healthy("inventory")))
    r = client.get("/fleet/fragment")
    assert r.status_code == 200
    assert "kpi-strip" in r.text
    assert "/device/d2" in r.text  # the device row link
    assert "<html" not in r.text.lower()  # partial, not the full page


def test_ack_button_reflects_acknowledgement(client):
    client.post("/api/v1/ingest", json=_env("d3", "inventory", healthy("inventory")))
    assert "квитировать" in client.get("/fleet/fragment").text  # un-acked title attr
    client.post("/api/v1/devices/d3/ack", json={"note": "investigating"})
    assert "acked" in client.get("/fleet/fragment").text  # acked button class


def _historical_with_ip(ip: str) -> dict:
    p = healthy("historical")
    p["network_adapters"] = [
        {
            "name": "Ethernet",
            "kind": "ethernet",
            "mac": "AA-BB-CC-00-00-01",
            "up": True,
            "ipv4": [ip],
            "gateway": "192.168.1.1",
        }
    ]
    return p


def test_fmt_age_zero_reads_as_seconds_not_words():
    from server.web.dashboard import fmt_age

    assert fmt_age(0) == "0с"
    assert fmt_age(-5) == "0с"
    assert fmt_age(None) == "—"
    assert fmt_age(45) == "45с"


def test_primary_ip_extracts_first_adapter_ipv4():
    import json

    from server.db import _primary_ip

    payload = json.dumps({"network_adapters": [{"ipv4": ["192.168.1.7"]}]})
    assert _primary_ip(payload) == "192.168.1.7"
    assert _primary_ip(None) is None
    assert _primary_ip("not json") is None
    assert _primary_ip(json.dumps({"network_adapters": []})) is None


def test_fleet_has_sortable_device_header(client):
    client.post("/api/v1/ingest", json=_env("d4", "inventory", healthy("inventory")))
    body = client.get("/fleet/fragment").text
    assert 'data-sort="text"' in body  # device-name column is sortable by name


def test_ip_column_shows_local_ip_with_copy_affordance(client):
    client.post(
        "/api/v1/ingest", json=_env("d5", "historical", _historical_with_ip("192.168.1.42"))
    )
    body = client.get("/fleet/fragment").text
    assert "192.168.1.42" in body
    assert 'class="ip-copy"' in body
    assert 'data-ip="192.168.1.42"' in body


def test_ip_column_falls_back_when_no_network_data(client):
    client.post("/api/v1/ingest", json=_env("d6", "inventory", healthy("inventory")))
    body = client.get("/fleet/fragment").text
    assert '<th title="локальный IP — клик копирует">IP</th>' in body
