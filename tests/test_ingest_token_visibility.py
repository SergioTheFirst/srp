"""P0-2 (stoperrors.md): an empty ``ingest_token`` (shipped default) must be LOUD,
not silent -- startup log WARNING + dashboard banner, until an operator sets one.
Behaviour (auth stays off when empty) is unchanged; only visibility is added."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server.config import ServerConfig
from server.main import create_app

pytestmark = pytest.mark.integration


def test_startup_warns_when_ingest_token_empty(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="srp.auth"):
        create_app(ServerConfig(db_path=str(tmp_path / "t.db"), ingest_token=""))
    assert any("БЕЗ аутентификации" in r.message for r in caplog.records)


def test_startup_silent_when_ingest_token_set(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="srp.auth"):
        create_app(ServerConfig(db_path=str(tmp_path / "t.db"), ingest_token="secret"))
    assert not any("БЕЗ аутентификации" in r.message for r in caplog.records)


def test_dashboard_shows_banner_when_ingest_token_empty(tmp_path):
    app = create_app(
        ServerConfig(
            db_path=str(tmp_path / "t.db"),
            ingest_token="",
            org_directory_path=str(tmp_path / "org_directory.json"),
        )
    )
    with TestClient(app) as c:
        assert "БЕЗ аутентификации" in c.get("/").text


def test_dashboard_hides_banner_when_ingest_token_set(tmp_path):
    app = create_app(
        ServerConfig(
            db_path=str(tmp_path / "t.db"),
            ingest_token="secret",
            org_directory_path=str(tmp_path / "org_directory.json"),
        )
    )
    with TestClient(app) as c:
        assert "БЕЗ аутентификации" not in c.get("/").text
