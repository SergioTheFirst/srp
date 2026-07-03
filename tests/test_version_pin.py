"""Пин версии сборки + юнит-тесты packaging/make_update_package.py.

VERSION (корень) и client.transport.AGENT_VERSION -- единый источник версии
для сборки (packaging/make_update_package.py читает VERSION) и конверта
(AGENT_VERSION идёт в каждое сообщение агента). Расползание номеров означает,
что сервер соберёт манифест под одной версией, а агенты будут слать другую --
и парк перестанет видеть собственное обновление.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def _load_make_update_package() -> ModuleType:
    """Load packaging/make_update_package.py by file path, not by import.

    A plain ``import packaging.make_update_package`` would resolve to the
    real PyPI ``packaging`` distribution (a transitive build dependency of
    PyInstaller/pip, installed in this venv) instead of the repo's
    ``packaging/`` directory, since that distribution is a regular package
    (has ``__init__.py``) and wins name resolution over our plain directory.
    """
    path = _ROOT / "packaging" / "make_update_package.py"
    spec = importlib.util.spec_from_file_location("srp_make_update_package", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_update_package = _load_make_update_package()


def _write(path: Path, content: bytes = b"stub") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _make_share(tmp_path: Path) -> Path:
    """A tmp dist/share/ layout mirroring build.bat's real output."""
    share = tmp_path / "share"
    _write(share / "setup.exe", b"fake-installer-bytes")
    _write(share / "VERSION", b"1.2.3\r\n")
    _write(share / "config.template.json", b'{"org_code": ""}')  # must NOT ship
    _write(share / "deploy-all.bat", b"@echo off\r\nrem secrets baked in here\r\n")  # must NOT ship
    _write(share / "payload" / "srp-agent.exe", b"fake-agent-binary")
    _write(share / "payload" / "x.dll", b"fake-dll-bytes")
    return share


# --------------------------------------------------------------------------- #
# Pin: VERSION == AGENT_VERSION
# --------------------------------------------------------------------------- #
def test_version_file_matches_agent_version() -> None:
    from client.transport import AGENT_VERSION

    version_text = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert version_text == AGENT_VERSION


# --------------------------------------------------------------------------- #
# build_package: happy path
# --------------------------------------------------------------------------- #
def test_build_package_zips_expected_members_and_writes_manifest(tmp_path: Path) -> None:
    share = _make_share(tmp_path)
    out = tmp_path / "updates"

    manifest = make_update_package.build_package(share, out, "1.2.3")

    zip_path = out / "srp-agent-update-1.2.3.zip"
    assert manifest["version"] == "1.2.3"
    assert manifest["file"] == "srp-agent-update-1.2.3.zip"
    assert manifest["size"] == zip_path.stat().st_size

    expected_sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert manifest["sha256"] == expected_sha256

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    # Exact set -- proves both the required members AND the absence of
    # config.template.json / deploy-all.bat in one assertion.
    assert names == {"setup.exe", "VERSION", "payload/srp-agent.exe", "payload/x.dll"}

    manifest_on_disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_on_disk == manifest


# --------------------------------------------------------------------------- #
# build_package: validation
# --------------------------------------------------------------------------- #
def test_build_package_missing_setup_exe_raises(tmp_path: Path) -> None:
    share = tmp_path / "share"
    _write(share / "payload" / "srp-agent.exe")  # setup.exe deliberately absent

    with pytest.raises(ValueError):
        make_update_package.build_package(share, tmp_path / "out", "1.0.0")


def test_build_package_rejects_malformed_version(tmp_path: Path) -> None:
    share = _make_share(tmp_path)

    with pytest.raises(ValueError):
        make_update_package.build_package(share, tmp_path / "out", "abc")
