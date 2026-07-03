"""T4: agent auto-update endpoints -- manifest/package serving, fail-closed
manifest validation, shared-token auth (same pattern as /ingest), rate limit,
sha256-recompute cache invalidation on zip change."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import zipfile
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import updates
from server.config import ServerConfig
from server.main import create_app

pytestmark = pytest.mark.integration

_ZIP_NAME = "srp-agent-update-0.2.0.zip"


def _make_zip(path: Path, content: bytes = b"payload-v1") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("setup.exe", content)
        zf.writestr("payload/srp-agent.exe", b"binary")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(dir_: Path, zip_name: str = _ZIP_NAME, **overrides: object) -> None:
    """Write a manifest.json in *dir_* correctly describing dir_/zip_name, unless
    a field is explicitly overridden (used to inject a single bad field)."""
    zip_path = dir_ / zip_name
    manifest: dict[str, object] = {
        "version": "0.2.0",
        "file": zip_name,
        "sha256": _sha256(zip_path),
        "size": zip_path.stat().st_size,
    }
    manifest.update(overrides)
    (dir_ / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_updates_cache() -> Iterator[None]:
    updates.reset_cache()
    yield
    updates.reset_cache()


@pytest.fixture
def updates_dir(tmp_path: Path) -> Path:
    d = tmp_path / "updates"
    d.mkdir()
    return d


def _app(updates_dir: Path, token: str = "") -> FastAPI:
    return create_app(
        ServerConfig(
            db_path=str(updates_dir.parent / "t.db"),
            updates_dir=str(updates_dir),
            ingest_token=token,
        )
    )


# --------------------------------------------------------------------------- #
# No manifest staged
# --------------------------------------------------------------------------- #
def test_no_manifest_returns_404_for_both_endpoints(updates_dir: Path) -> None:
    with TestClient(_app(updates_dir)) as c:
        assert c.get("/api/v1/agent/update").status_code == 404
        assert c.get("/api/v1/agent/update/package").status_code == 404


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_valid_package_returns_200_with_no_hmac_when_token_empty(updates_dir: Path) -> None:
    zip_path = updates_dir / _ZIP_NAME
    _make_zip(zip_path)
    _write_manifest(updates_dir)
    with TestClient(_app(updates_dir)) as c:
        r = c.get("/api/v1/agent/update")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["version"] == "0.2.0"
        assert body["size"] == zip_path.stat().st_size
        assert body["sha256"] == _sha256(zip_path)
        assert "hmac" not in body


def test_package_endpoint_returns_zip_bytes_with_zip_content_type(updates_dir: Path) -> None:
    zip_path = updates_dir / _ZIP_NAME
    _make_zip(zip_path, content=b"actual-agent-payload")
    _write_manifest(updates_dir)
    with TestClient(_app(updates_dir)) as c:
        r = c.get("/api/v1/agent/update/package")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert r.content == zip_path.read_bytes()


# --------------------------------------------------------------------------- #
# Token auth (same pattern as /ingest)
# --------------------------------------------------------------------------- #
def test_token_required_when_configured(updates_dir: Path) -> None:
    _make_zip(updates_dir / _ZIP_NAME)
    _write_manifest(updates_dir)
    with TestClient(_app(updates_dir, token="s3cret")) as c:
        assert c.get("/api/v1/agent/update").status_code == 401
        wrong = c.get("/api/v1/agent/update", headers={"X-SRP-Token": "wrong"})
        assert wrong.status_code == 401

        r = c.get("/api/v1/agent/update", headers={"X-SRP-Token": "s3cret"})
        assert r.status_code == 200, r.text
        body = r.json()
        material = f"{body['version']}|{body['sha256']}".encode()
        expected = hmac.new(b"s3cret", material, hashlib.sha256).hexdigest()
        assert body["hmac"] == expected


def test_package_endpoint_requires_token_too(updates_dir: Path) -> None:
    _make_zip(updates_dir / _ZIP_NAME)
    _write_manifest(updates_dir)
    with TestClient(_app(updates_dir, token="s3cret")) as c:
        assert c.get("/api/v1/agent/update/package").status_code == 401
        ok = c.get("/api/v1/agent/update/package", headers={"X-SRP-Token": "s3cret"})
        assert ok.status_code == 200


# --------------------------------------------------------------------------- #
# Fail-closed manifest validation
# --------------------------------------------------------------------------- #
def test_wrong_sha256_in_manifest_rejected(updates_dir: Path) -> None:
    _make_zip(updates_dir / _ZIP_NAME)
    _write_manifest(updates_dir, sha256="0" * 64)
    with TestClient(_app(updates_dir)) as c:
        assert c.get("/api/v1/agent/update").status_code == 404


def test_wrong_size_in_manifest_rejected(updates_dir: Path) -> None:
    _make_zip(updates_dir / _ZIP_NAME)
    _write_manifest(updates_dir, size=1)
    with TestClient(_app(updates_dir)) as c:
        assert c.get("/api/v1/agent/update").status_code == 404


def test_traversal_file_name_rejected(updates_dir: Path) -> None:
    """manifest["file"] must be a flat name -- a ``../`` escape is rejected before
    the (real, on-disk) file it points at is ever touched."""
    _make_zip(updates_dir / _ZIP_NAME)  # legit zip present but irrelevant here
    evil = updates_dir.parent / "evil.zip"
    evil.write_bytes(b"evil-payload")  # what an unguarded join would have served
    _write_manifest(updates_dir, file="../evil.zip")
    with TestClient(_app(updates_dir)) as c:
        assert c.get("/api/v1/agent/update").status_code == 404


def test_malformed_version_rejected(updates_dir: Path) -> None:
    _make_zip(updates_dir / _ZIP_NAME)
    _write_manifest(updates_dir, version="абв")
    with TestClient(_app(updates_dir)) as c:
        assert c.get("/api/v1/agent/update").status_code == 404


# --------------------------------------------------------------------------- #
# Cache: recompute must catch a zip swapped in after a successful validation
# --------------------------------------------------------------------------- #
def test_cache_invalidated_when_zip_content_changes(updates_dir: Path) -> None:
    zip_path = updates_dir / _ZIP_NAME
    _make_zip(zip_path, content=b"v1-content")
    _write_manifest(updates_dir)
    with TestClient(_app(updates_dir)) as c:
        first = c.get("/api/v1/agent/update")
        assert first.status_code == 200, first.text

        # Overwrite with different bytes; manifest keeps describing the OLD file.
        _make_zip(zip_path, content=b"tampered-content-of-a-different-length!!")
        # Force a strictly later mtime so the cache key changes regardless of the
        # filesystem's timestamp resolution -- the point under test is the
        # recompute-on-change path, not timestamp granularity.
        bumped = zip_path.stat().st_mtime + 5
        os.utime(zip_path, (bumped, bumped))

        second = c.get("/api/v1/agent/update")
        assert second.status_code == 404


# --------------------------------------------------------------------------- #
# Rate limit (shared "endpoint:agent_update" bucket, 30/window per ingest_guards)
# --------------------------------------------------------------------------- #
def test_rate_limit_returns_429_after_max_per_window(updates_dir: Path) -> None:
    _make_zip(updates_dir / _ZIP_NAME)
    _write_manifest(updates_dir)
    with TestClient(_app(updates_dir)) as c:
        statuses = [c.get("/api/v1/agent/update").status_code for _ in range(31)]
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429
