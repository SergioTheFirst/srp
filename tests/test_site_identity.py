"""Tests for W1.1 site/org identity: site_code and site_name grouping.

TDD order: RED (these tests are written first, before implementation).

Scenarios covered:
1. Envelope accepts site_code/site_name; both default None.
2. Inventory envelope with site sets device-level site columns.
3. Heartbeat WITHOUT site does NOT wipe a previously-set site (COALESCE).
4. Heartbeat WITH site stores it on a freshly-created device.
5. Fleet page renders and shows the site value in the Объект column.
"""

from __future__ import annotations

import pytest
from shared.schema import Envelope
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration

SITE_DEVICE = "test-site-001"


# --------------------------------------------------------------------------- #
# 1. Schema: Envelope gains site_code / site_name, both optional and None by default
# --------------------------------------------------------------------------- #


def test_envelope_site_fields_default_none():
    env = Envelope(device_id="dev-1", msg_type="heartbeat", payload={})
    assert env.site_code is None
    assert env.site_name is None


def test_envelope_accepts_site_code_and_name():
    env = Envelope(
        device_id="dev-1",
        msg_type="inventory",
        payload={},
        site_code="555",
        site_name="ACME Corp",
    )
    assert env.site_code == "555"
    assert env.site_name == "ACME Corp"


def test_envelope_site_fields_forward_compatible():
    """Old agents that omit site_code/site_name still produce a valid Envelope."""
    raw = {
        "device_id": "old-agent",
        "agent_version": "0.1.0",
        "msg_type": "heartbeat",
        "payload": {},
    }
    env = Envelope(**raw)
    assert env.site_code is None
    assert env.site_name is None


# --------------------------------------------------------------------------- #
# 2. Ingest: inventory with site_code/site_name stores them on the device
# --------------------------------------------------------------------------- #


def test_inventory_with_site_stores_site(client):
    """POSTing an inventory envelope with site_code/site_name persists them."""
    env = {
        **envelope(SITE_DEVICE, "inventory", healthy("inventory")),
        "site_code": "555",
        "site_name": "ACME",
    }
    resp = client.post("/api/v1/ingest", json=env)
    assert resp.status_code == 200, resp.text

    dev = client.get(f"/api/v1/devices/{SITE_DEVICE}").json()
    assert dev["site_code"] == "555"
    assert dev["site_name"] == "ACME"


# --------------------------------------------------------------------------- #
# 3. COALESCE: heartbeat WITHOUT site must NOT wipe a previously-set site
# --------------------------------------------------------------------------- #


def test_heartbeat_without_site_does_not_wipe_existing_site(client):
    """COALESCE semantics: a message that omits site fields must preserve the
    site that was set by a prior inventory envelope."""
    # First: inventory with site
    inv_env = {
        **envelope(SITE_DEVICE, "inventory", healthy("inventory")),
        "site_code": "555",
        "site_name": "ACME",
    }
    client.post("/api/v1/ingest", json=inv_env)

    # Second: heartbeat with NO site fields
    hb_env = envelope(SITE_DEVICE, "heartbeat", healthy("heartbeat"))
    # Explicitly ensure site fields are absent from the dict (not just None)
    assert "site_code" not in hb_env
    assert "site_name" not in hb_env
    client.post("/api/v1/ingest", json=hb_env)

    dev = client.get(f"/api/v1/devices/{SITE_DEVICE}").json()
    assert dev["site_code"] == "555", "site_code was wiped by a site-less heartbeat"
    assert dev["site_name"] == "ACME", "site_name was wiped by a site-less heartbeat"


def test_heartbeat_with_explicit_none_site_does_not_wipe_existing_site(client):
    """A message that explicitly sends site_code=None must also preserve the
    existing site (None triggers COALESCE to keep the stored value)."""
    inv_env = {
        **envelope(SITE_DEVICE, "inventory", healthy("inventory")),
        "site_code": "555",
        "site_name": "ACME",
    }
    client.post("/api/v1/ingest", json=inv_env)

    hb_env = {
        **envelope(SITE_DEVICE, "heartbeat", healthy("heartbeat")),
        "site_code": None,
        "site_name": None,
    }
    client.post("/api/v1/ingest", json=hb_env)

    dev = client.get(f"/api/v1/devices/{SITE_DEVICE}").json()
    assert dev["site_code"] == "555"
    assert dev["site_name"] == "ACME"


# --------------------------------------------------------------------------- #
# 4. Heartbeat-first: device created by a heartbeat carrying site stores it
# --------------------------------------------------------------------------- #


def test_heartbeat_with_site_stores_site_on_new_device(client):
    """A device first seen via a heartbeat that carries site info must store it."""
    hb_env = {
        **envelope("hb-site-device", "heartbeat", healthy("heartbeat")),
        "site_code": "25",
        "site_name": "Beta Org",
    }
    resp = client.post("/api/v1/ingest", json=hb_env)
    assert resp.status_code == 200, resp.text

    dev = client.get("/api/v1/devices/hb-site-device").json()
    assert dev["site_code"] == "25"
    assert dev["site_name"] == "Beta Org"


# --------------------------------------------------------------------------- #
# 5. Fleet page renders and shows the Объект column with the site value
# --------------------------------------------------------------------------- #


def test_fleet_page_shows_site_column(client):
    """The fleet dashboard must render an Объект column with the device's site."""
    env = {
        **envelope(SITE_DEVICE, "inventory", healthy("inventory")),
        "site_code": "555",
        "site_name": "ACME",
    }
    client.post("/api/v1/ingest", json=env)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Объект" in resp.text
    assert "ACME" in resp.text


def test_fleet_page_shows_site_code_when_no_site_name(client):
    """When site_name is absent, the dashboard falls back to site_code."""
    env = {
        **envelope("code-only-device", "inventory", healthy("inventory")),
        "site_code": "777",
    }
    client.post("/api/v1/ingest", json=env)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "777" in resp.text


def test_fleet_page_shows_dash_when_no_site(client):
    """Devices without any site info must show '—' in the Объект column."""
    env = envelope("no-site-device", "inventory", healthy("inventory"))
    client.post("/api/v1/ingest", json=env)

    resp = client.get("/")
    assert resp.status_code == 200
    # The em-dash placeholder must appear
    assert "—" in resp.text


# --------------------------------------------------------------------------- #
# 6. get_devices includes site fields
# --------------------------------------------------------------------------- #


def test_get_devices_includes_site_fields(client):
    """The /api/v1/devices list must include site_code and site_name per device."""
    env = {
        **envelope(SITE_DEVICE, "inventory", healthy("inventory")),
        "site_code": "555",
        "site_name": "ACME",
    }
    client.post("/api/v1/ingest", json=env)

    devices = client.get("/api/v1/devices").json()
    device = next(d for d in devices if d["device_id"] == SITE_DEVICE)
    assert device["site_code"] == "555"
    assert device["site_name"] == "ACME"
