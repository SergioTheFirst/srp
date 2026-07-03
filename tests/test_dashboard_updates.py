"""T5: dashboard surfacing of the agent auto-update pipeline.

Covers the server/db.py::get_devices() point-fix (agent_version was missing from
the SELECT, so the fleet-wide 'outdated' badge never had real per-device version
data to compare) plus the new server/web/dashboard.py plumbing: available-version
lookup from the staged server/updates package, and the three templates
(_fleet_body, device, deploy) rendering the update_state/available-version chips.
"""

from __future__ import annotations

import hashlib
import json
import types
import zipfile
from pathlib import Path
from typing import Iterator, Optional

import pytest
from fastapi.testclient import TestClient
from server import db, updates
from server.config import ServerConfig
from server.main import create_app
from server.web import dashboard
from tests.conftest import envelope

pytestmark = pytest.mark.integration

_ZIP_NAME = "srp-agent-update-0.3.0.zip"
_VERSION = "0.3.0"
_OUTDATED_CHIP = '<span class="badge warn">устар.</span>'


def _make_zip(path: Path, content: bytes = b"payload") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("setup.exe", content)
        zf.writestr("payload/srp-agent.exe", b"binary")


def _stage_package(updates_dir: Path) -> None:
    """Write a valid manifest.json + zip describing a v0.3.0 package."""
    updates_dir.mkdir(parents=True, exist_ok=True)
    zip_path = updates_dir / _ZIP_NAME
    _make_zip(zip_path)
    manifest = {
        "version": _VERSION,
        "file": _ZIP_NAME,
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        "size": zip_path.stat().st_size,
    }
    (updates_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_updates_cache() -> Iterator[None]:
    updates.reset_cache()
    yield
    updates.reset_cache()


@pytest.fixture
def app_with_updates(tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient whose server/updates has a valid staged v0.3.0 package."""
    updates_dir = tmp_path / "updates"
    _stage_package(updates_dir)
    app = create_app(
        ServerConfig(
            db_path=str(tmp_path / "t.db"),
            updates_dir=str(updates_dir),
            org_directory_path=str(tmp_path / "org_directory.json"),
        )
    )
    with TestClient(app) as c:
        yield c


def _fake_request(updates_dir: Optional[str]) -> types.SimpleNamespace:
    """Duck-typed stand-in for FastAPI's Request -- _fleet_available_version only
    reads request.app.state.updates_dir, so a full Request/TestClient is overkill."""
    state = types.SimpleNamespace(updates_dir=updates_dir)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


# --------------------------------------------------------------------------- #
# server/db.py: get_devices() now carries agent_version (pre-existing bug fix)
# --------------------------------------------------------------------------- #
def test_get_devices_returns_agent_version(client: TestClient) -> None:
    db.upsert_device("dv1", "2026-07-03T00:00:00+00:00", "0.1.5", hostname="PC-1")
    row = next(r for r in db.get_devices() if r["device_id"] == "dv1")
    assert row["agent_version"] == "0.1.5"


# --------------------------------------------------------------------------- #
# _fleet_available_version
# --------------------------------------------------------------------------- #
def test_fleet_available_version_reads_staged_manifest(tmp_path: Path) -> None:
    updates_dir = tmp_path / "updates"
    _stage_package(updates_dir)
    version = dashboard._fleet_available_version(_fake_request(str(updates_dir)))
    assert version == _VERSION


def test_fleet_available_version_none_without_manifest(tmp_path: Path) -> None:
    updates_dir = tmp_path / "updates"
    updates_dir.mkdir()  # no manifest.json staged
    assert dashboard._fleet_available_version(_fake_request(str(updates_dir))) is None


def test_fleet_available_version_none_when_updates_dir_unset() -> None:
    assert dashboard._fleet_available_version(_fake_request(None)) is None


# --------------------------------------------------------------------------- #
# Fleet + fragment: outdated chip driven by the staged package, not fleet-max
# --------------------------------------------------------------------------- #
def test_fleet_page_flags_outdated_against_staged_package(app_with_updates: TestClient) -> None:
    db.upsert_device("old1", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="OLD-PC")
    assert _OUTDATED_CHIP in app_with_updates.get("/").text


def test_fleet_fragment_flags_outdated_against_staged_package(
    app_with_updates: TestClient,
) -> None:
    db.upsert_device("old2", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="OLD-PC-2")
    assert _OUTDATED_CHIP in app_with_updates.get("/fleet/fragment").text


def test_fleet_fragment_not_outdated_when_matching_staged_version(
    app_with_updates: TestClient,
) -> None:
    """A device already on the staged version must NOT get the 'устар.' chip --
    regression guard for the available-vs-fleet-max comparison direction."""
    db.upsert_device("cur1", "2026-07-03T00:00:00+00:00", _VERSION, hostname="CUR-PC")
    assert _OUTDATED_CHIP not in app_with_updates.get("/fleet/fragment").text


def test_fleet_fragment_shows_update_failed_chip(app_with_updates: TestClient) -> None:
    env = envelope("fail1", "update_status", {"state": "failed", "error": "сеть недоступна"})
    app_with_updates.post("/api/v1/ingest", json=env)
    body = app_with_updates.get("/fleet/fragment").text
    assert "ошибка обновл." in body
    assert "сеть недоступна" in body


# --------------------------------------------------------------------------- #
# Device page: update badges
# --------------------------------------------------------------------------- #
def test_device_page_shows_updating_badge(app_with_updates: TestClient) -> None:
    env = envelope("upd1", "update_status", {"state": "updating"})
    app_with_updates.post("/api/v1/ingest", json=env)
    assert "обновляется" in app_with_updates.get("/device/upd1").text


def test_device_page_shows_update_available_badge(app_with_updates: TestClient) -> None:
    db.upsert_device("upd2", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="UPD-PC-2")
    assert "обновление доступно" in app_with_updates.get("/device/upd2").text


def test_device_page_shows_update_failed_badge_with_error(app_with_updates: TestClient) -> None:
    env = envelope("upd3", "update_status", {"state": "failed", "error": "нет связи с сервером"})
    env["agent_version"] = _VERSION  # matches staged package -> isolates the failed chip
    app_with_updates.post("/api/v1/ingest", json=env)
    body = app_with_updates.get("/device/upd3").text
    assert "ошибка обновления" in body
    assert "нет связи с сервером" in body
    assert "обновление доступно" not in body


def test_device_page_shows_update_checked_and_version_changed_timestamps(
    app_with_updates: TestClient,
) -> None:
    env = envelope("upd4", "update_status", {"state": "ok", "checked_at": "2026-07-03T00:00:00Z"})
    app_with_updates.post("/api/v1/ingest", json=env)
    body = app_with_updates.get("/device/upd4").text
    assert "проверка обновления" in body
    assert "последнее обновление версии" in body  # version_changed_at set on first sighting


# --------------------------------------------------------------------------- #
# /deploy: package info block
# --------------------------------------------------------------------------- #
def test_deploy_page_without_package_says_not_staged(client: TestClient) -> None:
    assert "не выложен" in client.get("/deploy").text


def test_deploy_page_with_staged_package_shows_version_and_file(
    app_with_updates: TestClient,
) -> None:
    body = app_with_updates.get("/deploy").text
    assert _VERSION in body
    assert _ZIP_NAME in body
