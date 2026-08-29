"""Unit tests for source_last_good and trust DB storage (Plan 3 DB layer).

These tests are pure SQLite; no network, no FastAPI. Each test gets a fresh
throwaway DB via the db_init fixture.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


# --------------------------------------------------------------------------- #
# source_last_good
# --------------------------------------------------------------------------- #
def test_last_good_round_trip(db_init):
    """set_last_good followed by get_last_good returns the original dict."""
    reading = {"temp_c": 45.0, "wear_pct": 12.3}
    db_init.set_last_good("dev-1", "disk", reading, "2026-01-01T00:00:00Z")

    result = db_init.get_last_good("dev-1", "disk")
    assert result == reading


def test_last_good_missing_returns_none(db_init):
    """get_last_good on an unknown (device, source) pair returns None."""
    assert db_init.get_last_good("ghost", "disk") is None


def test_last_good_upsert_overwrites(db_init):
    """A second set_last_good for the same (device, source) replaces the first."""
    db_init.set_last_good("dev-1", "cpu", {"load": 10.0}, "2026-01-01T00:00:00Z")
    db_init.set_last_good("dev-1", "cpu", {"load": 55.0}, "2026-01-02T00:00:00Z")

    result = db_init.get_last_good("dev-1", "cpu")
    assert result == {"load": 55.0}


def test_last_good_per_device_source_pair(db_init):
    """Two different sources on the same device are stored independently."""
    db_init.set_last_good("dev-1", "disk", {"wear_pct": 5.0}, "2026-01-01T00:00:00Z")
    db_init.set_last_good("dev-1", "network", {"loss_pct": 0.0}, "2026-01-01T00:00:00Z")

    disk = db_init.get_last_good("dev-1", "disk")
    network = db_init.get_last_good("dev-1", "network")

    assert disk == {"wear_pct": 5.0}
    assert network == {"loss_pct": 0.0}


def test_last_good_different_devices_no_collision(db_init):
    """The same source name on two devices stores separate readings."""
    db_init.set_last_good("dev-A", "cpu", {"load": 20.0}, "2026-01-01T00:00:00Z")
    db_init.set_last_good("dev-B", "cpu", {"load": 90.0}, "2026-01-01T00:00:00Z")

    assert db_init.get_last_good("dev-A", "cpu") == {"load": 20.0}
    assert db_init.get_last_good("dev-B", "cpu") == {"load": 90.0}


# --------------------------------------------------------------------------- #
# trust
# --------------------------------------------------------------------------- #
def test_trust_round_trip(db_init):
    """store_trust followed by get_trust returns the original result dict."""
    result = {
        "domains": {"disk": "ok", "cpu": "warn"},
        "lineage": ["validator_a", "validator_b"],
        "overall": 0.85,
    }
    db_init.store_trust("dev-1", "2026-01-01T00:00:00Z", result)

    stored = db_init.get_trust("dev-1")
    assert stored == result


def test_trust_missing_returns_none(db_init):
    """get_trust on an unknown device returns None."""
    assert db_init.get_trust("nobody") is None


def test_trust_upsert_overwrites(db_init):
    """A second store_trust for the same device replaces the previous result."""
    db_init.store_trust("dev-1", "2026-01-01T00:00:00Z", {"overall": 0.5})
    db_init.store_trust("dev-1", "2026-01-02T00:00:00Z", {"overall": 0.9})

    stored = db_init.get_trust("dev-1")
    assert stored == {"overall": 0.9}


def test_evaluate_trust_reads_last_good_once_per_source(client, monkeypatch) -> None:
    """o5-D11: `regressed` дёргал get_last_good отдельным соединением на КАЖДЫЙ
    источник, поверх такого же чтения в цикле выше — двойная работа на каждом
    конверте (самый горячий путь сервера)."""
    from server import db, pipeline

    from tests.conftest import envelope, healthy

    calls = {"n": 0}
    real = db.get_last_good

    def _counting(device_id, source):
        calls["n"] += 1
        return real(device_id, source)

    monkeypatch.setattr(db, "get_last_good", _counting)

    src_names = ["cpu", "memory", "uptime", "events", "certificates", "identity"]
    env = envelope("d11-dev", "heartbeat", healthy("heartbeat"))
    # Двойное чтение проявляется только у ОТКАЗАВШИХ источников: у здоровых
    # `and` в выражении `regressed` коротко замыкается до второго обращения.
    env["source_health"] = {n: {"status": "timeout", "fields": {}} for n in src_names}
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 200

    known = [n for n in src_names if n in pipeline._KNOWN_TRUST_SOURCES]
    assert known, "фикстура не попала ни в один известный источник"
    assert calls["n"] <= len(known), (
        f"get_last_good вызван {calls['n']} раз при {len(known)} источниках"
    )
