"""Tests for W1.2 certificate-expiry monitoring.

TDD order: schema → collector → days_until → dashboard integration.

Scenarios:
1. CertInfo validates correctly; HistoricalPayload round-trips with certificates.
2. collect_historical() populates certificates when the cert script succeeds.
3. collect_historical() handles a blocked cert script gracefully (empty list, blocked status).
4. days_until: future date → positive days; past date → negative; None → None.
5. Dashboard: POST historical with certificates; GET /device shows subjects and expiry chips.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from client.collectors.historical import collect_historical
from client.collectors.ps import PsResult
from client.collectors.sources import CERTIFICATES
from server.web.dashboard import days_until
from shared.schema import CertInfo, HistoricalPayload
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration

CERT_DEVICE = "test-cert-001"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FUTURE_10 = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
_FUTURE_400 = (datetime.now(timezone.utc) + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
_PAST_5 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

_SAMPLE_CERT = {
    "subject": "CN=test.example.com",
    "issuer": "CN=Test CA",
    "thumbprint": "AABBCCDD",
    "not_after": _FUTURE_10,
    "not_before": "2025-01-01T00:00:00+00:00",
}

_MAIN_PS_DATA = {
    "reliability_stability_index": 9.0,
    "kernel_power_41_30d": 0,
    "dirty_shutdowns_30d": 0,
    "bugchecks_30d": 0,
    "app_crashes_30d": 0,
    "whea_errors_30d": 0,
    "avg_boot_ms": 20000,
    "storage": [],
    "battery": {"present": False},
    "observation_days": 30,
}

# ---------------------------------------------------------------------------
# 1. Schema tests
# ---------------------------------------------------------------------------


def test_cert_info_validates():
    c = CertInfo(
        subject="CN=foo",
        issuer="CN=bar",
        thumbprint="DEADBEEF",
        not_after="2027-01-01T00:00:00+00:00",
        not_before="2025-01-01T00:00:00+00:00",
    )
    assert c.subject == "CN=foo"
    assert c.thumbprint == "DEADBEEF"


def test_cert_info_all_optional():
    c = CertInfo()
    assert c.subject is None
    assert c.not_after is None


def test_historical_payload_certificates_roundtrip():
    payload = HistoricalPayload(
        certificates=[
            {
                "subject": "CN=test.example.com",
                "issuer": "CN=Test CA",
                "thumbprint": "AABBCCDD",
                "not_after": "2027-06-01T00:00:00+00:00",
                "not_before": "2025-06-01T00:00:00+00:00",
            }
        ]
    )
    assert len(payload.certificates) == 1
    cert = payload.certificates[0]
    assert isinstance(cert, CertInfo)
    assert cert.subject == "CN=test.example.com"
    assert cert.thumbprint == "AABBCCDD"


def test_historical_payload_certificates_default_empty():
    payload = HistoricalPayload()
    assert payload.certificates == []


# ---------------------------------------------------------------------------
# 2. Collector: cert script succeeds
# ---------------------------------------------------------------------------


def test_collect_historical_populates_certificates():
    cert_ps_data = {"certificates": [_SAMPLE_CERT]}

    def side_effect(script, timeout=30):
        # First call is the main _SCRIPT, second is _CERT_SCRIPT.
        if not hasattr(side_effect, "_calls"):
            side_effect._calls = 0
        side_effect._calls += 1
        if side_effect._calls == 1:
            return PsResult("ok", dict(_MAIN_PS_DATA))
        return PsResult("ok", cert_ps_data)

    with patch("client.collectors.historical.run_ps", side_effect=side_effect):
        result = collect_historical()

    assert result.payload is not None
    certs = result.payload["certificates"]
    assert len(certs) == 1
    assert certs[0]["subject"] == "CN=test.example.com"
    assert certs[0]["thumbprint"] == "AABBCCDD"
    assert result.source_health[CERTIFICATES]["status"] == "ok"


def test_collect_historical_empty_cert_store():
    """An empty certificate list is valid; source status should be 'empty'."""
    cert_ps_data = {"certificates": []}

    call_count = 0

    def side_effect(script, timeout=30):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return PsResult("ok", dict(_MAIN_PS_DATA))
        return PsResult("ok", cert_ps_data)

    with patch("client.collectors.historical.run_ps", side_effect=side_effect):
        result = collect_historical()

    assert result.payload["certificates"] == []
    assert result.source_health[CERTIFICATES]["status"] == "empty"


# ---------------------------------------------------------------------------
# 3. Collector: cert script blocked
# ---------------------------------------------------------------------------


def test_collect_historical_cert_blocked():
    call_count = 0

    def side_effect(script, timeout=30):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return PsResult("ok", dict(_MAIN_PS_DATA))
        return PsResult("blocked")

    with patch("client.collectors.historical.run_ps", side_effect=side_effect):
        result = collect_historical()

    assert result.payload["certificates"] == []
    assert result.source_health[CERTIFICATES]["status"] == "blocked"


def test_collect_historical_main_script_failure_marks_certs_failed():
    """If the main script fails, all owned sources including CERTIFICATES are marked failed."""
    with patch("client.collectors.historical.run_ps", return_value=PsResult("timeout")):
        result = collect_historical()

    assert result.payload is None
    assert result.source_health[CERTIFICATES]["status"] == "timeout"


# ---------------------------------------------------------------------------
# 4. days_until
# ---------------------------------------------------------------------------


def test_days_until_future():
    future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    result = days_until(future)
    assert result is not None
    assert 9 <= result <= 11  # allow ±1 for timing


def test_days_until_past():
    past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    result = days_until(past)
    assert result is not None
    assert result < 0


def test_days_until_none():
    assert days_until(None) is None


def test_days_until_empty_string():
    assert days_until("") is None


def test_days_until_invalid():
    assert days_until("not-a-date") is None


def test_days_until_z_suffix():
    """ISO strings with trailing Z must be handled (like PowerShell emits)."""
    future = (datetime.now(timezone.utc) + timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = days_until(future)
    assert result is not None
    assert 14 <= result <= 16


# ---------------------------------------------------------------------------
# 5. Dashboard integration
# ---------------------------------------------------------------------------


def _cert_historical_payload() -> dict:
    base = healthy("historical")
    base["certificates"] = [
        {
            "subject": "CN=soon.example.com",
            "issuer": "CN=Test CA",
            "thumbprint": "AABB1111",
            "not_after": _FUTURE_10,
            "not_before": "2025-01-01T00:00:00+00:00",
        },
        {
            "subject": "CN=later.example.com",
            "issuer": "CN=Other CA",
            "thumbprint": "CCDD2222",
            "not_after": _FUTURE_400,
            "not_before": "2025-01-01T00:00:00+00:00",
        },
    ]
    return base


def test_device_page_shows_cert_subjects(client):
    client.post(
        "/api/v1/ingest",
        json=envelope(CERT_DEVICE, "historical", _cert_historical_payload()),
    )
    resp = client.get(f"/device/{CERT_DEVICE}")
    assert resp.status_code == 200
    assert "soon.example.com" in resp.text
    assert "later.example.com" in resp.text


def test_device_page_shows_expiry_chips(client):
    client.post(
        "/api/v1/ingest",
        json=envelope(CERT_DEVICE, "historical", _cert_historical_payload()),
    )
    resp = client.get(f"/device/{CERT_DEVICE}")
    assert resp.status_code == 200
    # 10-day cert → < 17d threshold → cert-alert-inline (yellow highlight)
    assert "cert-alert-inline" in resp.text
    # long-lived cert (400 days) should render a good chip
    assert "chip good" in resp.text


def test_device_page_cert_section_heading(client):
    client.post(
        "/api/v1/ingest",
        json=envelope(CERT_DEVICE, "historical", _cert_historical_payload()),
    )
    resp = client.get(f"/device/{CERT_DEVICE}")
    assert resp.status_code == 200
    assert "Сертификаты" in resp.text


def test_device_page_no_certs_shows_placeholder(client):
    """When no certificates are present the placeholder message must appear."""
    base = healthy("historical")
    base["certificates"] = []
    client.post(
        "/api/v1/ingest",
        json=envelope(CERT_DEVICE, "historical", base),
    )
    resp = client.get(f"/device/{CERT_DEVICE}")
    assert resp.status_code == 200
    assert "Действующих личных сертификатов нет" in resp.text
