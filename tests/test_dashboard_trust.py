"""Dashboard surfacing of telemetry trust (Plan 3d).

Renders the device page through TestClient and checks the trust is visible: an
untrusted domain shows UNKNOWN (not a false-confidence %), an untrusted identity
shows a banner, and a coverage section lists the domains.
"""

from __future__ import annotations

import pytest

from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-05-30T00:00:00+00:00"}


def _env(device_id: str, msg_type: str, payload: dict, source_health: dict) -> dict:
    return {
        "device_id": device_id,
        "agent_version": "0.1.0",
        "msg_type": msg_type,
        "payload": payload,
        "source_health": source_health,
    }


def test_device_page_marks_unknown_domain(client):
    sh = {
        "storage_reliability": _sh("blocked"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("dx", "historical", healthy("historical"), sh))
    resp = client.get("/device/dx")
    assert resp.status_code == 200
    assert "UNKNOWN" in resp.text
    assert "Покрытие источников" in resp.text


def test_device_page_shows_untrusted_banner(client):
    client.post(
        "/api/v1/ingest",
        json=_env("du", "inventory", healthy("inventory"), {"identity": _sh("blocked")}),
    )
    resp = client.get("/device/du")
    assert resp.status_code == 200
    assert "Идентичность недостоверна" in resp.text


def test_fleet_page_still_renders(client):
    sh = {
        "storage_reliability": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("df", "historical", healthy("historical"), sh))
    assert client.get("/").status_code == 200


def test_device_page_neutral_name_has_same_suffix_as_fleet(client):
    """Task 4b: безымянный ПК показывает один и тот же текст на /fleet и
    /device/{id} -- device_id-суффикс различает несколько «Без названия» в
    обоих местах одинаково (сейчас на карточке он голый, без суффикса)."""
    from server import db

    device_id = "a1b2c3d4e5"
    ts = "2026-05-30T00:00:00+00:00"
    risk = {"day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []}}
    db.touch_device(device_id, ts, "0.1.0")
    db.store_scores(device_id, ts, {"risk": risk})

    expected = f"Без названия ({device_id[-6:]})"
    assert expected in db.get_devices()[0]["display_name"]

    resp = client.get(f"/device/{device_id}")
    assert resp.status_code == 200
    assert expected in resp.text
