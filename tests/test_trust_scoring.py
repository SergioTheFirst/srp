"""Integration: bayesian risk gated by per-domain trust (Plan 3c).

Posts envelopes carrying `source_health`, then asserts that the scores the server
stores reflect trust: a class whose mapped domain is untrusted is tagged UNKNOWN,
an untrusted identity flags the device, and old agents (no source_health) get
ungated scores unchanged.
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


def _classes(dev: dict) -> dict:
    return {c["name"]: c for c in dev["scores"]["risk"]["classes"]}


_HIST_OK = {
    "storage_reliability": _sh("ok"),
    "reliability": _sh("ok"),
    "boot_time": _sh("ok"),
}


def test_storage_class_unknown_when_domain_untrusted(client):
    sh = dict(_HIST_OK, storage_reliability=_sh("blocked"))
    client.post("/api/v1/ingest", json=_env("dev-st", "historical", healthy("historical"), sh))
    dev = client.get("/api/v1/devices/dev-st").json()
    assert _classes(dev)["storage"]["trust"] == "unknown"
    assert dev["scores"]["risk"]["domains"]["storage"]["state"] == "unknown"
    # P0-5 (stoperrors.md): a gate-failed domain must not leak a confident
    # number over the wire -- "trust":"unknown" used to sit right next to a
    # real probability/level in this exact response.
    assert _classes(dev)["storage"]["probability"] is None
    assert _classes(dev)["storage"]["level"] == "unknown"


def test_storage_class_trusted_when_source_ok(client):
    client.post(
        "/api/v1/ingest", json=_env("dev-ok", "historical", healthy("historical"), _HIST_OK)
    )
    dev = client.get("/api/v1/devices/dev-ok").json()
    assert _classes(dev)["storage"]["trust"] == "trusted"
    assert _classes(dev)["storage"]["probability"] is not None


def test_memory_class_is_ungated(client):
    client.post(
        "/api/v1/ingest", json=_env("dev-mem", "historical", healthy("historical"), _HIST_OK)
    )
    dev = client.get("/api/v1/devices/dev-mem").json()
    assert _classes(dev)["memory"]["trust"] is None


def test_device_untrusted_when_identity_fails(client):
    client.post(
        "/api/v1/ingest",
        json=_env("dev-unt", "inventory", healthy("inventory"), {"identity": _sh("blocked")}),
    )
    dev = client.get("/api/v1/devices/dev-unt").json()
    assert dev["scores"]["risk"]["device_trust"] == "untrusted"
    # contract §7: untrusted identity -> day-1 scores withheld (None), not shown
    assert dev["scores"]["performance"] is None
    assert dev["scores"]["risk_exposure"] is None
    # P0-5: identity-untrusted is a superset gate -- every mapped bayesian
    # class withholds too, regardless of its own domain's individual state.
    classes = _classes(dev)
    # memory has no trust domain of its own (test_memory_class_is_ungated) --
    # device_untrusted is the one thing that must still withhold it, since it
    # is the identity-level superset gate, not a domain lookup.
    for name in ("storage", "power_thermal", "stability", "memory"):
        assert classes[name]["probability"] is None, name
        assert classes[name]["level"] == "unknown", name


def test_no_source_health_low_confidence_score100(client):
    """Backward compat (W0.5): an old agent (no source_health) keeps its ungated
    legacy numbers, but its Score100 is flagged low-confidence -- never silently
    healthy."""
    env = {
        "device_id": "dev-old",
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": healthy("historical"),
    }
    client.post("/api/v1/ingest", json=env)
    dev = client.get("/api/v1/devices/dev-old").json()
    sc = dev["scores"]
    # legacy risk gating untouched (no per-domain trust to apply)
    assert "device_trust" not in sc["risk"]
    assert "trust" not in _classes(dev)["storage"]
    # but the Score100 envelope is present and low/unknown confidence with a reason
    rel = sc["risk"]["score100"]["reliability"]
    assert rel["confidence"] in ("low", "unknown")
    assert "source_health отсутствует" in rel["missing_evidence"]
    # legacy numeric still present for the dashboard
    assert sc["reliability"] is not None


# --------------------------------------------------------------------------- #
# KodSR H2: gate-failed domain must not leak its raw contribution into a
# day-1 axis's numeric value -- only disk_fill/storage are OPTIONAL for
# risk_exposure (server/scoring/score100.py ~399), so a failed gate there
# only used to lower confidence while the polluted number rode along.
# --------------------------------------------------------------------------- #
def test_disk_fill_gate_failure_blanks_risk_exposure_leak(client):
    hb_bad = dict(healthy("heartbeat"), free_space_pct=-5.0)
    sh = {"free_space": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-df-neg", "heartbeat", hb_bad, sh))
    dev = client.get("/api/v1/devices/dev-df-neg").json()
    assert dev["scores"]["risk"]["domains"]["disk_fill"]["state"] == "unknown"
    # A clean heartbeat (no free-space penalty at all) scores risk_exposure 0.0;
    # the gate-failed reading must match that, not leak its own +30.
    assert dev["scores"]["risk_exposure"] == 0.0


def test_disk_fill_gate_failure_out_of_range_high_blanks_too(client):
    """free_space_pct=150 is implausible too -- must not be read as 'disk free'."""
    hb_bad = dict(healthy("heartbeat"), free_space_pct=150.0)
    sh = {"free_space": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-df-hi", "heartbeat", hb_bad, sh))
    dev = client.get("/api/v1/devices/dev-df-hi").json()
    assert dev["scores"]["risk"]["domains"]["disk_fill"]["state"] == "unknown"
    assert dev["scores"]["risk_exposure"] == 0.0


# --------------------------------------------------------------------------- #
# KodSR H3: "ok" collector status + an empty extracted reading at a domain-
# gating source must not read as TRUSTED -- the collector said "fine" but
# handed back nothing to actually validate.
# --------------------------------------------------------------------------- #
def test_empty_reading_at_gating_source_is_not_trusted(client):
    hist = dict(healthy("historical"), storage=[])
    sh = dict(_HIST_OK, storage_reliability=_sh("ok"))
    client.post("/api/v1/ingest", json=_env("dev-empty-st", "historical", hist, sh))
    dev = client.get("/api/v1/devices/dev-empty-st").json()
    assert dev["scores"]["risk"]["domains"]["storage"]["state"] == "unknown"


def test_empty_certificates_with_ok_status_stays_ok_regression(client):
    """certificates is event-driven -- legitimately empty must NOT be reinterpreted
    as EMPTY/gate-fail (only domain-gating sources get that treatment)."""
    from server import db

    sh = dict(_HIST_OK, certificates=_sh("ok"))
    client.post(
        "/api/v1/ingest", json=_env("dev-certs-empty", "historical", healthy("historical"), sh)
    )
    trust = db.get_trust("dev-certs-empty")
    assert trust["sources"]["certificates"]["state"] == "ok"


def test_disk_latency_stays_ok_with_its_normal_empty_reading(client):
    """Security review HIGH-2: disk_latency is a domain-gating source but NOT
    material (its _extract_reading always returns {} -- pipeline.py has no
    case for it) -- an 'ok' collector report there must not be coerced to
    EMPTY just because its reading is always empty by design."""
    from server import db

    sh = dict(_HIST_OK, disk_latency=_sh("ok"))
    client.post("/api/v1/ingest", json=_env("dev-dl-ok", "historical", healthy("historical"), sh))
    trust = db.get_trust("dev-dl-ok")
    assert trust["sources"]["disk_latency"]["state"] == "ok"


def test_free_space_none_with_ok_status_is_not_trusted_and_freezes_evidence(client):
    """Security review MEDIUM-3: free_space's _extract_reading wraps a missing
    value as {"value": None} -- truthy, so a plain `not reading` coercion
    check never fires, and validate_scalar_range(None) returns PLAUSIBLE
    ("absence is a collector concern, not semantic") -- an 'ok' collector with
    nothing real to report must still gate-fail disk_fill and must never
    advance evidence_seen_at."""
    from server import db

    hb_ok = dict(healthy("heartbeat"), free_space_pct=61.0)
    sh = {"free_space": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("dev-fs-none", "heartbeat", hb_ok, sh))
    with db._connect() as conn:
        before = conn.execute(
            "SELECT evidence_seen_at FROM device_source_trust WHERE device_id=? AND source=?",
            ("dev-fs-none", "free_space"),
        ).fetchone()["evidence_seen_at"]

    hb_none = dict(healthy("heartbeat"), free_space_pct=None)
    client.post("/api/v1/ingest", json=_env("dev-fs-none", "heartbeat", hb_none, sh))
    dev = client.get("/api/v1/devices/dev-fs-none").json()
    assert dev["scores"]["risk"]["domains"]["disk_fill"]["state"] == "unknown"

    with db._connect() as conn:
        after = conn.execute(
            "SELECT evidence_seen_at FROM device_source_trust WHERE device_id=? AND source=?",
            ("dev-fs-none", "free_space"),
        ).fetchone()["evidence_seen_at"]
    assert after == before
