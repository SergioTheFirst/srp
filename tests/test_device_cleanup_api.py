"""Device-ghost cleanup over the wire: delete + bulk-purge endpoints (2026-06-16).

Drives the real FastAPI app (TestClient + throwaway DB) to pin that an operator
can remove a ghost, that bulk-purge previews before deleting, and that a stray
GET can never wipe data. Old-but-silent rows are aged by back-dating last_seen
through the db module the running app already points at the temp DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from server.config import ServerConfig
from server.main import create_app
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _ids(client) -> set[str]:
    return {d["device_id"] for d in client.get("/api/v1/devices").json()}


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _token_client(tmp_path) -> TestClient:
    app = create_app(ServerConfig(db_path=str(tmp_path / "t.db"), ingest_token="secret"))
    return TestClient(app)


def test_delete_endpoint_removes_device(client):
    client.post("/api/v1/ingest", json=envelope("dghost", "heartbeat", healthy("heartbeat")))
    assert "dghost" in _ids(client)

    resp = client.post("/api/v1/devices/dghost/delete")

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True
    assert "dghost" not in _ids(client)


def test_delete_endpoint_unknown_is_404(client):
    assert client.post("/api/v1/devices/never-seen/delete").status_code == 404


def test_delete_requires_post_not_get(client):
    """A delete must never be reachable by GET (no prefetch/link-preview wipes)."""
    client.post("/api/v1/ingest", json=envelope("dG", "heartbeat", healthy("heartbeat")))
    assert client.get("/api/v1/devices/dG/delete").status_code in (404, 405)
    assert "dG" in _ids(client)  # the GET changed nothing


def test_purge_endpoint_preview_then_delete(client):
    from server import db

    db.upsert_device("dold", _iso_days_ago(40), "1.0.0", received_at=_iso_days_ago(40))
    db.upsert_device("dnew", db._now_iso(), "1.0.0")

    preview = client.post("/api/v1/devices/purge", json={"days": 30, "dry_run": True}).json()
    assert preview["device_ids"] == ["dold"]
    assert preview["deleted"] is False
    assert "dold" in _ids(client)  # preview deleted nothing

    result = client.post("/api/v1/devices/purge", json={"days": 30}).json()
    assert result["count"] == 1
    assert result["deleted"] is True
    assert "dold" not in _ids(client)
    assert "dnew" in _ids(client)  # the live one is untouched


def test_purge_rejects_zero_days(client):
    """days=0 would target everything -> rejected at the boundary (422)."""
    assert client.post("/api/v1/devices/purge", json={"days": 0}).status_code == 422


def test_connected_agent_shows_full_info_and_ghost_is_gone(client):
    """The operator's goal: a live agent shows up with its data; ghosts disappear."""
    from server import db

    db.upsert_device("ghost", _iso_days_ago(99), "1.0.0", received_at=_iso_days_ago(99))
    client.post("/api/v1/ingest", json=envelope("live", "inventory", healthy("inventory")))

    client.post("/api/v1/devices/purge", json={"days": 30})

    ids = _ids(client)
    assert "ghost" not in ids
    assert "live" in ids
    detail = client.get("/api/v1/devices/live")
    assert detail.status_code == 200
    assert detail.json()["device_id"] == "live"


def test_fleet_page_exposes_cleanup_controls(client):
    """The bulk-purge button (page) and per-row delete button (fragment) render."""
    client.post("/api/v1/ingest", json=envelope("dC", "inventory", healthy("inventory")))
    assert 'id="purgebtn"' in client.get("/").text
    assert "delbtn" in client.get("/fleet/fragment").text


# --------------------------------------------------------------------------- #
# P0-1: delete/purge must honour the same shared-token check as /ingest and
# /agent/update -- an unauthenticated LAN host must not be able to wipe the
# fleet. Empty ingest_token (test default via the `client` fixture) stays a
# no-op, matched by every test above this point.
# --------------------------------------------------------------------------- #
def test_delete_endpoint_rejects_without_or_wrong_token(tmp_path):
    with _token_client(tmp_path) as c:
        c.post(
            "/api/v1/ingest",
            json=envelope("dtok", "heartbeat", healthy("heartbeat")),
            headers={"X-SRP-Token": "secret"},
        )
        assert c.post("/api/v1/devices/dtok/delete").status_code == 401
        assert (
            c.post("/api/v1/devices/dtok/delete", headers={"X-SRP-Token": "wrong"}).status_code
            == 401
        )
        assert "dtok" in _ids(c)  # neither attempt actually deleted anything


def test_delete_endpoint_accepts_correct_token(tmp_path):
    with _token_client(tmp_path) as c:
        c.post(
            "/api/v1/ingest",
            json=envelope("dtok2", "heartbeat", healthy("heartbeat")),
            headers={"X-SRP-Token": "secret"},
        )
        resp = c.post("/api/v1/devices/dtok2/delete", headers={"X-SRP-Token": "secret"})
        assert resp.status_code == 200, resp.text
        assert "dtok2" not in _ids(c)


def test_purge_endpoint_rejects_without_or_wrong_token(tmp_path):
    with _token_client(tmp_path) as c:
        assert c.post("/api/v1/devices/purge", json={"days": 30}).status_code == 401
        assert (
            c.post(
                "/api/v1/devices/purge",
                json={"days": 30},
                headers={"X-SRP-Token": "wrong"},
            ).status_code
            == 401
        )


def test_purge_endpoint_accepts_correct_token(tmp_path):
    with _token_client(tmp_path) as c:
        resp = c.post("/api/v1/devices/purge", json={"days": 30}, headers={"X-SRP-Token": "secret"})
        assert resp.status_code == 200, resp.text


def test_delete_endpoint_is_rate_limited_after_a_burst(client):
    client.post("/api/v1/ingest", json=envelope("dR", "heartbeat", healthy("heartbeat")))
    statuses = {client.post("/api/v1/devices/dR/delete").status_code for _ in range(40)}
    assert 429 in statuses  # the flood is throttled, same as poll_discovery/poll_topology


def test_purge_endpoint_is_rate_limited_after_a_burst(client):
    statuses = {
        client.post("/api/v1/devices/purge", json={"days": 30, "dry_run": True}).status_code
        for _ in range(40)
    }
    assert 429 in statuses
