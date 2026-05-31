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
    assert "UNKNOWN" in r.text
    assert "/fleet/fragment" in r.text  # JS polls the fragment endpoint
    assert "обновлено" in r.text  # live indicator
    assert "Объект:" in r.text  # site grouping header


def test_fleet_fragment_is_a_partial(client):
    client.post("/api/v1/ingest", json=_env("d2", "inventory", healthy("inventory")))
    r = client.get("/fleet/fragment")
    assert r.status_code == 200
    assert "kpis" in r.text
    assert "/device/d2" in r.text  # the device row link
    assert "<html" not in r.text.lower()  # partial, not the full page


def test_ack_button_reflects_acknowledgement(client):
    client.post("/api/v1/ingest", json=_env("d3", "inventory", healthy("inventory")))
    assert "квит." in client.get("/fleet/fragment").text  # un-acked label
    client.post("/api/v1/devices/d3/ack", json={"note": "investigating"})
    assert "acked" in client.get("/fleet/fragment").text  # acked button class
