"""W0.4 CONTRACT_VERSION discipline: forward/backward compat + version negotiation.

The contract is additive (optional fields + extra='allow'), so an older server
accepts a newer agent's envelope and vice-versa. CONTRACT_VERSION makes the
agent<->server compatibility decision explicit: same MAJOR = compatible. The
server never *rejects* telemetry on a version mismatch (UNKNOWN over false
confidence -> keep the data, flag the mismatch).
"""

from __future__ import annotations

import pytest
from shared.schema import CONTRACT_VERSION, is_contract_compatible, parse_version
from tests.conftest import envelope, healthy


# --------------------------------------------------------------------------- #
# Version parsing (unit)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [("0.1.0", (0, 1, 0)), ("1.2.3", (1, 2, 3)), ("10.0.0", (10, 0, 0))],
)
def test_parse_version_valid(raw, expected):
    assert parse_version(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "x", "1", "1.2", "1.2.x", None])
def test_parse_version_invalid_returns_none(raw):
    assert parse_version(raw) is None


# --------------------------------------------------------------------------- #
# Compatibility decision (unit)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_same_major_is_compatible():
    server_major = parse_version(CONTRACT_VERSION)[0]
    assert is_contract_compatible(f"{server_major}.99.5") is True


@pytest.mark.unit
def test_different_major_is_incompatible():
    server_major = parse_version(CONTRACT_VERSION)[0]
    assert is_contract_compatible(f"{server_major + 1}.0.0") is False


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "garbage", None, "9"])
def test_unparseable_version_is_incompatible(bad):
    # UNKNOWN over false confidence: an unreadable version is flagged, not trusted.
    assert is_contract_compatible(bad) is False


# --------------------------------------------------------------------------- #
# Forward/backward compat + negotiation across the HTTP ingest boundary
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_ingest_accepts_newer_agent_envelope(client):
    """A future agent: unknown top-level envelope field + higher version, accepted."""
    env = envelope("fwd", "heartbeat", healthy("heartbeat"))
    env["agent_version"] = "99.0.0"
    env["future_envelope_field"] = {"anything": 1}
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 200, r.text


@pytest.mark.integration
def test_ingest_accepts_older_minimal_envelope(client):
    """An older agent omits newer optional fields (no source_health/site_code)."""
    raw = {
        "device_id": "old",
        "agent_version": "0.0.1",
        "msg_type": "heartbeat",
        "payload": {"free_space_pct": 50.0},
    }
    r = client.post("/api/v1/ingest", json=raw)
    assert r.status_code == 200, r.text


@pytest.mark.integration
def test_ingest_response_reports_version_negotiation(client):
    """Server tells the agent its contract version + whether they're compatible."""
    env = envelope("nego", "heartbeat", healthy("heartbeat"))
    env["agent_version"] = CONTRACT_VERSION
    r = client.post("/api/v1/ingest", json=env).json()
    assert r["server_contract_version"] == CONTRACT_VERSION
    assert r["contract_compatible"] is True


@pytest.mark.integration
def test_ingest_flags_incompatible_major_but_still_stores(client):
    """A wrong-major agent is flagged incompatible, yet telemetry is NOT dropped."""
    bad_major = parse_version(CONTRACT_VERSION)[0] + 1
    env = envelope("badver", "heartbeat", healthy("heartbeat"))
    env["agent_version"] = f"{bad_major}.0.0"
    r = client.post("/api/v1/ingest", json=env).json()
    assert r["contract_compatible"] is False
    assert any(d["device_id"] == "badver" for d in client.get("/api/v1/devices").json())
