"""W0.4 CONTRACT_VERSION discipline: forward/backward compat + version negotiation.

The contract is additive (optional fields + extra='allow'), so an older server
accepts a newer agent's envelope and vice-versa as long as the MAJOR matches.
CONTRACT_VERSION makes the agent<->server compatibility decision explicit: same
MAJOR = compatible, stored and scored normally, mismatched MINOR/PATCH included
(UNKNOWN over false confidence -> keep the data). A different or unparseable
MAJOR is a real contract break: since D2 (owner decision) the /ingest endpoint
rejects it with 406 *before* any processing -- nothing is stored, the agent
gets a machine-readable stop signal instead of silently leaking telemetry the
server can no longer interpret.
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
    """A future same-MAJOR agent: unknown field + higher minor/patch, accepted."""
    server_major = parse_version(CONTRACT_VERSION)[0]
    env = envelope("fwd", "heartbeat", healthy("heartbeat"))
    env["agent_version"] = f"{server_major}.99.0"
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
def test_ingest_rejects_incompatible_major_with_406(client):
    """D2: a wrong-major agent is stopped at the boundary -- 406, nothing stored."""
    from server import ingest_guards

    bad_major = parse_version(CONTRACT_VERSION)[0] + 1
    env = envelope("badver", "heartbeat", healthy("heartbeat"))
    env["agent_version"] = f"{bad_major}.0.0"
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 406, r.text
    assert not any(d["device_id"] == "badver" for d in client.get("/api/v1/devices").json())
    assert ingest_guards.REJECT_COUNTS["incompatible"] == 1


@pytest.mark.integration
def test_ingest_rejects_unparseable_agent_version_with_406(client):
    """Garbage agent_version can't be checked for MAJOR compat -> UNKNOWN, 406."""
    env = envelope("junkver", "heartbeat", healthy("heartbeat"))
    env["agent_version"] = "not-a-version"
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 406, r.text
    assert not any(d["device_id"] == "junkver" for d in client.get("/api/v1/devices").json())
