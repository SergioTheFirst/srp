"""Журнал исходов и переходов вердикта (спека 2026-10-30, H18).

Пинует: переход пишется только при смене уровня h0-h4; потолки строк на устройство;
исход оператора под admin-токеном и лимитом частоты, без исхода ack работает
по-старому; удаление и пурж устройства сохраняют машинные поля, но обнуляют заметку
(152-ФЗ) и пишут отметку decommissioned; журналы вне _DEVICE_TABLES.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from server import health_journal, ingest_guards

from tests.conftest import envelope, healthy

pytestmark = pytest.mark.unit

_TS_OLD = "2020-01-01T00:00:00+00:00"
_TS_NEW = datetime.now(timezone.utc).isoformat()  # relative to now: no date bomb in prune


@pytest.fixture
def jdb(tmp_path):
    from server import db

    db.init_db(tmp_path / "journal.db")
    return db


def _ingest(client, did: str, msg_type: str, payload: dict) -> None:
    resp = client.post("/api/v1/ingest", json=envelope(did, msg_type, payload))
    assert resp.status_code == 200, resp.text


def _seed(client, did: str = "dJ") -> None:
    for msg_type in ("inventory", "historical", "heartbeat"):
        _ingest(client, did, msg_type, healthy(msg_type))


def _latest_health(did: str) -> dict:
    from server import db

    return db.get_score_series(did, limit=1)[0]["risk"]["health"]


# --------------------------------------------------------------------------- #
# DB layer
# --------------------------------------------------------------------------- #
def test_transition_appended_and_read_back(jdb):
    jdb.append_health_transition(
        "d1", _TS_NEW, "h0", "h3", raw_state="h3", dominant="storage", detail={"via_unknown": False}
    )
    rows = jdb.get_health_transitions("d1")
    assert len(rows) == 1
    row = rows[0]
    assert (row["from_state"], row["to_state"], row["raw_state"], row["dominant"]) == (
        "h0",
        "h3",
        "h3",
        "storage",
    )
    assert row["detail"] == {"via_unknown": False}


def test_transitions_are_capped_per_device_keeping_newest(jdb, monkeypatch):
    monkeypatch.setattr(jdb, "_JOURNAL_TRANSITIONS_MAX", 3)
    for i in range(5):
        jdb.append_health_transition("d1", _TS_NEW, f"h{i % 5}", f"h{(i + 1) % 5}")
    jdb.append_health_transition("d2", _TS_NEW, None, "h0")  # another device untouched
    rows = jdb.get_health_transitions("d1", limit=10)
    assert len(rows) == 3
    assert rows[0]["to_state"] == "h0"  # newest first: i=4 -> h0
    assert len(jdb.get_health_transitions("d2")) == 1


def test_outcomes_are_capped_per_device_keeping_newest(jdb, monkeypatch):
    monkeypatch.setattr(jdb, "_JOURNAL_OUTCOMES_MAX", 2)
    for outcome in ("false_alarm", "unverified", "confirmed_failure"):
        jdb.add_health_outcome("d1", _TS_NEW, "operator", outcome)
    rows = jdb.get_health_outcomes("d1")
    assert [r["outcome"] for r in rows] == ["confirmed_failure", "unverified"]


def test_prune_journal_drops_rows_older_than_retention(jdb):
    jdb.append_health_transition("d1", _TS_OLD, None, "h0")
    jdb.append_health_transition("d1", _TS_NEW, "h0", "h1")
    jdb.add_health_outcome("d1", _TS_OLD, "operator", "false_alarm")
    jdb.add_health_outcome("d1", _TS_NEW, "operator", "unverified")
    removed = jdb.prune_health_journal(730)
    assert removed == 2
    assert [r["to_state"] for r in jdb.get_health_transitions("d1")] == ["h1"]
    assert [r["outcome"] for r in jdb.get_health_outcomes("d1")] == ["unverified"]


def test_journal_tables_are_pseudonymous_not_device_tables(jdb):
    assert set(jdb._PSEUDONYMOUS_TABLES) == {"health_transitions", "health_outcomes"}
    assert not set(jdb._PSEUDONYMOUS_TABLES) & set(jdb._DEVICE_TABLES)


def test_unknown_cycle_is_not_a_level_change(jdb):
    """h2 -> unknown -> h2 writes nothing; a level reached through unknown is marked."""
    h2 = {"state": "h2", "last_known_state": "h2"}
    blind = {"state": "unknown", "last_known_state": "h2"}
    assert health_journal.record_transition("d1", h2, blind, _TS_NEW) is False
    assert health_journal.record_transition("d1", blind, h2, _TS_NEW) is False
    h3 = {"state": "h3", "last_known_state": "h3", "raw_state": "h3"}
    assert health_journal.record_transition("d1", blind, h3, _TS_NEW) is True
    (row,) = jdb.get_health_transitions("d1")
    assert (row["from_state"], row["to_state"]) == ("h2", "h3")
    assert row["detail"] == {"state": "h3", "via_unknown": True}


def test_delete_survives_a_corrupt_risk_row(client):
    """A non-dict risk JSON in scores must not fail the delete (code review)."""
    from server import db

    _seed(client)
    with db._lock, db._connect() as conn:
        conn.execute("UPDATE scores SET risk='[1]' WHERE device_id='dJ'")
    assert db.delete_device("dJ") is True
    outcomes = db.get_health_outcomes("dJ")
    assert [(r["outcome"], r["state_at"]) for r in outcomes] == [("decommissioned", None)]


def test_os_reinstall_mark_is_written_once(jdb):
    assert health_journal.mark_os_reinstalls({"old": "new"}, _TS_NEW) == 1
    assert health_journal.mark_os_reinstalls({"old": "new"}, _TS_NEW) == 0
    rows = jdb.get_health_outcomes("old")
    assert [(r["source"], r["outcome"]) for r in rows] == [("auto", "os_reinstalled")]


# --------------------------------------------------------------------------- #
# Pipeline: transitions and raw_state
# --------------------------------------------------------------------------- #
def test_first_recompute_writes_initial_transition(client):
    from server import db

    _seed(client)
    rows = db.get_health_transitions("dJ")
    assert len(rows) == 1
    assert rows[0]["from_state"] is None
    assert rows[0]["to_state"] == _latest_health("dJ")["last_known_state"]


def test_equal_level_writes_nothing(client):
    from server import db

    _seed(client)
    before = len(db.get_health_transitions("dJ"))
    _ingest(client, "dJ", "heartbeat", healthy("heartbeat"))  # recompute, same level
    assert len(db.get_health_transitions("dJ")) == before


def test_level_change_writes_one_transition(client):
    from server import db

    _seed(client)
    first = db.get_health_transitions("dJ")[0]["to_state"]
    sick = healthy("historical")
    sick["storage"] = [{**sick["storage"][0], "smart_attrs": {"5": 0, "197": 12, "198": 0}}]
    _ingest(client, "dJ", "historical", sick)
    rows = db.get_health_transitions("dJ")
    latest = _latest_health("dJ")
    assert latest["last_known_state"] != first
    assert len(rows) == 2
    assert (rows[0]["from_state"], rows[0]["to_state"]) == (first, latest["last_known_state"])
    assert rows[0]["raw_state"] == latest["raw_state"]


def test_verdict_carries_raw_state(client):
    _seed(client)
    assert _latest_health("dJ")["raw_state"] in {"h0", "h1", "h2", "h3", "h4", "unknown"}


# --------------------------------------------------------------------------- #
# API: ack with an outcome
# --------------------------------------------------------------------------- #
def test_ack_note_only_still_works_without_token(client):
    from server import db

    _seed(client)
    client.app.state.admin_token = "t0k"  # a token is configured, the legacy body still passes
    resp = client.post("/api/v1/devices/dJ/ack", json={"note": "смотрим"})
    assert resp.status_code == 200
    assert db.get_health_outcomes("dJ") == []


def test_ack_outcome_writes_a_verdict_snapshot(client):
    from server import db

    _seed(client)
    resp = client.post(
        "/api/v1/devices/dJ/ack", json={"note": "проверили", "outcome": "false_alarm"}
    )
    assert resp.status_code == 200
    health = _latest_health("dJ")
    rows = db.get_health_outcomes("dJ")
    assert len(rows) == 1
    row = rows[0]
    assert (row["source"], row["outcome"], row["note"]) == ("operator", "false_alarm", "проверили")
    assert (row["state_at"], row["dominant_at"], row["action"]) == (
        health["state"],
        health["dominant"],
        health["action"],
    )
    assert db.get_ack("dJ")["note"] == "проверили"  # the legacy ack is kept in step


def test_ack_outcome_rejects_unknown_value(client):
    _seed(client)
    resp = client.post("/api/v1/devices/dJ/ack", json={"outcome": "drive_exploded"})
    assert resp.status_code == 422


def test_ack_note_over_cap_is_rejected(client):
    _seed(client)
    resp = client.post("/api/v1/devices/dJ/ack", json={"note": "x" * 1001, "outcome": "unverified"})
    assert resp.status_code == 422


def test_ack_outcome_needs_the_admin_token_when_configured(client):
    _seed(client)
    client.app.state.admin_token = "t0k"
    body = {"outcome": "unverified"}
    assert client.post("/api/v1/devices/dJ/ack", json=body).status_code == 401
    resp = client.post("/api/v1/devices/dJ/ack", json=body, headers={"x-srp-token": "t0k"})
    assert resp.status_code == 200


def test_ack_outcome_is_rate_limited(client, monkeypatch):
    _seed(client)
    monkeypatch.setattr(ingest_guards, "_device_windows", {})
    monkeypatch.setattr(ingest_guards, "_RATE_MAX_PER_WINDOW", 2)
    body = {"outcome": "unverified"}
    codes = [client.post("/api/v1/devices/dJ/ack", json=body).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# --------------------------------------------------------------------------- #
# Delete / purge: machine fields survive, the note does not (152-ФЗ)
# --------------------------------------------------------------------------- #
def test_delete_device_keeps_machine_fields_and_clears_note(client):
    from server import db

    _seed(client)
    client.post("/api/v1/devices/dJ/ack", json={"note": "ФИО, телефон", "outcome": "false_alarm"})
    assert db.delete_device("dJ") is True
    outcomes = db.get_health_outcomes("dJ")
    assert {r["outcome"] for r in outcomes} == {"false_alarm", "decommissioned"}
    assert all(r["note"] is None for r in outcomes)
    assert db.get_health_transitions("dJ")  # the transitions survive as well


def test_operator_outcome_for_a_deleted_device_is_not_written(client):
    """The delete committed between the 404 check and the write (security review):
    the existence check runs under the same lock, so no orphan note survives."""
    from server import db

    _seed(client)
    assert db.delete_device("dJ") is True
    assert health_journal.record_operator_outcome("dJ", "false_alarm", "ФИО", _TS_NEW) is False
    assert all(r["note"] is None for r in db.get_health_outcomes("dJ"))


def test_purge_marks_the_device_decommissioned(client):
    from server import db

    _seed(client)
    state = _latest_health("dJ")["state"]
    assert db.purge_devices_silent_for(0)["count"] == 1
    rows = db.get_health_outcomes("dJ")
    assert [(r["source"], r["outcome"], r["state_at"]) for r in rows] == [
        ("auto", "decommissioned", state)
    ]


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def test_fleet_page_offers_the_outcome_dialog(client):
    page = client.get("/").text
    assert 'id="ackdlg"' in page
    for value in health_journal.OPERATOR_OUTCOMES:
        assert f'value="{value}"' in page
