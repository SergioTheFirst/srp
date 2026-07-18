"""Agent self-update tests: pure functions + Updater with urllib/subprocess stubbed.

No real network or schtasks call happens here: ``urllib.request.urlopen`` and
``subprocess.run`` are monkeypatched in the ``client.updater`` namespace, the
same way ``test_transport.py`` stubs ``Transport._deliver`` to stay fast and
deterministic.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
import urllib.error
import zipfile

import pytest
from client import updater as updater_mod
from client.config import ClientConfig
from client.updater import Updater, compute_hmac, safe_extract, select_update

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cfg(tmp_path, **overrides) -> ClientConfig:
    base = {
        "server_url": "http://127.0.0.1:9",
        "device_id": "t-dev",
        "buffer_path": str(tmp_path / "buffer.jsonl"),
    }
    base.update(overrides)
    return ClientConfig(**base)


def _manifest(**overrides) -> dict:
    base = {"version": "0.2.0", "sha256": "a" * 64, "size": 1024}
    base.update(overrides)
    return base


def _good_members() -> dict:
    return {"setup.exe": b"setup-bytes", "payload/srp-agent.exe": b"agent-bytes"}


def _zip_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _manifest_url(cfg: ClientConfig) -> str:
    return cfg.server_url.rstrip("/") + "/api/v1/agent/update"


def _package_url(cfg: ClientConfig) -> str:
    return cfg.server_url.rstrip("/") + "/api/v1/agent/update/package"


def _urlopen_once(content: bytes):
    def _urlopen(req, timeout=None):
        return io.BytesIO(content)

    return _urlopen


def _urlopen_routed(routes: dict):
    """routes: URL -> bytes (200 body) | Exception instance (raised)."""

    def _urlopen(req, timeout=None):
        outcome = routes[req.full_url]
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome)

    return _urlopen


# --------------------------------------------------------------------------- #
# _parse_version
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.2.3", (1, 2, 3)),
        ("0.0.0", (0, 0, 0)),
        ("10.20.30", (10, 20, 30)),
        ("garbage", None),
        ("1.2", None),
        ("1.2.3.4", None),
        ("a.b.c", None),
        ("1.2.a", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_version(value, expected) -> None:
    assert updater_mod._parse_version(value) == expected


# --------------------------------------------------------------------------- #
# select_update
# --------------------------------------------------------------------------- #
def test_select_update_returns_manifest_when_newer() -> None:
    assert select_update(_manifest(), "0.1.0", has_token=False) == _manifest()


def test_select_update_none_when_equal() -> None:
    assert select_update(_manifest(version="0.1.0"), "0.1.0", has_token=False) is None


def test_select_update_none_when_older() -> None:
    assert select_update(_manifest(version="0.1.0"), "0.2.0", has_token=False) is None


def test_select_update_none_on_bad_version() -> None:
    assert select_update(_manifest(version="bad"), "0.1.0", has_token=False) is None


def test_select_update_none_on_bad_sha256() -> None:
    assert select_update(_manifest(sha256="not-hex-not-64-chars"), "0.1.0", has_token=False) is None


def test_select_update_none_on_oversized() -> None:
    huge = 200 * 1024 * 1024 + 1
    assert select_update(_manifest(size=huge), "0.1.0", has_token=False) is None


def test_select_update_none_on_zero_size() -> None:
    assert select_update(_manifest(size=0), "0.1.0", has_token=False) is None


def test_select_update_requires_hmac_with_token() -> None:
    # fail-closed: a stripping MITM must not silently downgrade us to sha256-only
    assert select_update(_manifest(), "0.1.0", has_token=True) is None


def test_select_update_accepts_hmac_with_token() -> None:
    m = _manifest(hmac="deadbeef")
    assert select_update(m, "0.1.0", has_token=True) == m


# --------------------------------------------------------------------------- #
# compute_hmac
# --------------------------------------------------------------------------- #
def test_compute_hmac_is_deterministic() -> None:
    a = compute_hmac("tok", "0.2.0", "a" * 64)
    b = compute_hmac("tok", "0.2.0", "a" * 64)
    assert a == b


def test_compute_hmac_differs_by_version() -> None:
    # Anti-downgrade material: signing the version means an old signed package
    # can't be replayed and relabeled as a different version.
    a = compute_hmac("tok", "0.2.0", "a" * 64)
    b = compute_hmac("tok", "0.3.0", "a" * 64)
    assert a != b


# --------------------------------------------------------------------------- #
# safe_extract
# --------------------------------------------------------------------------- #
def test_safe_extract_normal_zip(tmp_path) -> None:
    zpath = tmp_path / "pkg.zip"
    zpath.write_bytes(_zip_bytes(_good_members()))
    dest = tmp_path / "staging"
    safe_extract(zpath, dest)
    assert (dest / "setup.exe").read_bytes() == b"setup-bytes"
    assert (dest / "payload" / "srp-agent.exe").read_bytes() == b"agent-bytes"


@pytest.mark.parametrize(
    "bad_name",
    ["../evil.txt", "C:\\evil.txt", "/abs.txt", "payload\\..\\..\\x"],
)
def test_safe_extract_rejects_unsafe_names(tmp_path, bad_name) -> None:
    members = _good_members()
    members[bad_name] = b"evil"
    zpath = tmp_path / "pkg.zip"
    zpath.write_bytes(_zip_bytes(members))
    with pytest.raises(ValueError):
        safe_extract(zpath, tmp_path / "staging")


def test_safe_extract_requires_setup_exe(tmp_path) -> None:
    zpath = tmp_path / "pkg.zip"
    zpath.write_bytes(_zip_bytes({"payload/srp-agent.exe": b"agent-bytes"}))
    with pytest.raises(ValueError):
        safe_extract(zpath, tmp_path / "staging")


def test_safe_extract_wipes_stale_staging(tmp_path) -> None:
    dest = tmp_path / "staging"
    dest.mkdir()
    (dest / "old-garbage.txt").write_text("stale")
    zpath = tmp_path / "pkg.zip"
    zpath.write_bytes(_zip_bytes(_good_members()))
    safe_extract(zpath, dest)
    assert not (dest / "old-garbage.txt").exists()
    assert (dest / "setup.exe").exists()


# --------------------------------------------------------------------------- #
# Updater._download
# --------------------------------------------------------------------------- #
def test_download_success(tmp_path, monkeypatch) -> None:
    u = Updater(_cfg(tmp_path))
    content = b"zip-bytes-content"
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen_once(content))
    result = u._download("http://x/package", digest, len(content))
    assert result == u._update_dir / "pkg.zip"
    assert result.read_bytes() == content
    assert not (u._update_dir / "pkg.zip.part").exists()


def test_download_bad_sha_removes_part(tmp_path, monkeypatch) -> None:
    u = Updater(_cfg(tmp_path))
    content = b"zip-bytes-content"
    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen_once(content))
    result = u._download("http://x/package", "0" * 64, len(content))
    assert result is None
    assert not (u._update_dir / "pkg.zip.part").exists()
    assert not (u._update_dir / "pkg.zip").exists()


def test_download_oversized_content_aborts(tmp_path, monkeypatch) -> None:
    u = Updater(_cfg(tmp_path))
    content = b"x" * 100
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen_once(content))
    result = u._download("http://x/package", digest, 10)  # promised size is a lie
    assert result is None
    assert not (u._update_dir / "pkg.zip.part").exists()


# --------------------------------------------------------------------------- #
# Updater.check
# --------------------------------------------------------------------------- #
def test_check_update_channel_none_short_circuits(tmp_path, monkeypatch) -> None:
    u = Updater(_cfg(tmp_path, update_channel="none"))

    def _urlopen(req, timeout=None):
        raise AssertionError("must not be called when update_channel is none")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen)
    assert u.check("0.1.0") == (None, False)


def test_check_not_found_then_no_repeat_until_state_changes(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    u = Updater(cfg)
    err = urllib.error.HTTPError(_manifest_url(cfg), 404, "not found", None, None)
    monkeypatch.setattr(
        updater_mod.urllib.request, "urlopen", _urlopen_routed({_manifest_url(cfg): err})
    )

    payload, restart = u.check("0.1.0")
    assert payload is not None
    assert payload["state"] == "ok"
    assert restart is False

    payload2, restart2 = u.check("0.1.0")
    assert payload2 is None  # nothing changed -- don't spam
    assert restart2 is False


def test_check_network_error_is_silent(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    u = Updater(cfg)

    def _urlopen(req, timeout=None):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen)
    assert u.check("0.1.0") == (None, False)


def test_check_newer_available_dev_mode_does_not_download(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    u = Updater(cfg)
    manifest = json.dumps({"version": "9.9.9", "sha256": "a" * 64, "size": 10}).encode()

    def _urlopen(req, timeout=None):
        if req.full_url == _manifest_url(cfg):
            return io.BytesIO(manifest)
        raise AssertionError("package must not be fetched outside a frozen build")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen)
    # getattr(sys, "frozen", False) is False in the test process -- dev mode.
    payload, restart = u.check("0.1.0")
    assert payload is not None
    assert payload["state"] == "ok"
    assert payload["available_version"] == "9.9.9"
    assert restart is False


def test_check_frozen_no_token_never_applies(tmp_path, monkeypatch) -> None:
    """Security-review finding 1: without a shared token, sha256 alone is only
    integrity, not authenticity -- a LAN MITM could self-sign a malicious
    package. An agent with no ingest_token must never download/apply, even
    when frozen and a strictly-newer version is on offer."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path)  # no ingest_token
    u = Updater(cfg)
    manifest = json.dumps({"version": "9.9.9", "sha256": "a" * 64, "size": 10}).encode()

    def _urlopen(req, timeout=None):
        if req.full_url == _manifest_url(cfg):
            return io.BytesIO(manifest)
        raise AssertionError("package must not be fetched without an ingest_token")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(
        updater_mod.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(AssertionError("schtasks must not run")),
    )

    payload, restart = u.check("0.1.0")

    assert payload is not None
    assert payload["state"] == "ok"
    assert payload["available_version"] == "9.9.9"
    assert restart is False
    assert not u._state_path.exists()


def test_check_frozen_happy_path_applies_update(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok")
    u = Updater(cfg)
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    manifest = json.dumps(
        {
            "version": "9.9.9",
            "sha256": digest,
            "size": len(zip_bytes),
            "hmac": compute_hmac("tok", "9.9.9", digest),
        }
    ).encode()
    monkeypatch.setattr(
        updater_mod.urllib.request,
        "urlopen",
        _urlopen_routed({_manifest_url(cfg): manifest, _package_url(cfg): zip_bytes}),
    )
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(updater_mod.subprocess, "run", _fake_run)

    payload, restart = u.check("0.1.0")

    assert payload is not None
    assert payload["state"] == "updating"
    assert payload["available_version"] == "9.9.9"
    assert restart is True
    assert len(calls) == 2  # schtasks /create then /run
    assert (u._staging_dir / "setup.exe").exists()
    assert u._state_path.exists()
    assert json.loads(u._state_path.read_text())["target_version"] == "9.9.9"


def test_check_frozen_schtasks_create_failure_is_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok")
    u = Updater(cfg)
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    manifest = json.dumps(
        {
            "version": "9.9.9",
            "sha256": digest,
            "size": len(zip_bytes),
            "hmac": compute_hmac("tok", "9.9.9", digest),
        }
    ).encode()
    monkeypatch.setattr(
        updater_mod.urllib.request,
        "urlopen",
        _urlopen_routed({_manifest_url(cfg): manifest, _package_url(cfg): zip_bytes}),
    )
    monkeypatch.setattr(
        updater_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1)
    )

    payload, restart = u.check("0.1.0")

    assert payload is not None
    assert payload["state"] == "failed"
    assert payload["error"]
    assert restart is False


def test_check_attempts_exhausted_skips_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok")
    u = Updater(cfg)
    u._update_dir.mkdir(parents=True, exist_ok=True)
    u._state_path.write_text(
        json.dumps({"target_version": "9.9.9", "attempts": 3, "staged_at": time.time()})
    )
    manifest = json.dumps(
        {
            "version": "9.9.9",
            "sha256": "a" * 64,
            "size": 10,
            "hmac": compute_hmac("tok", "9.9.9", "a" * 64),
        }
    ).encode()

    def _urlopen(req, timeout=None):
        if req.full_url == _manifest_url(cfg):
            return io.BytesIO(manifest)
        raise AssertionError("package must not be downloaded once attempts are exhausted")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen)

    payload, restart = u.check("0.1.0")

    assert payload is not None
    assert payload["state"] == "failed"
    assert "9.9.9" in payload["error"]
    assert restart is False


# --------------------------------------------------------------------------- #
# Updater.reconcile_after_restart
# --------------------------------------------------------------------------- #
def test_reconcile_no_state_file_returns_none(tmp_path) -> None:
    u = Updater(_cfg(tmp_path))
    assert u.reconcile_after_restart("0.1.0") is None


def test_reconcile_target_reached_clears_state(tmp_path) -> None:
    u = Updater(_cfg(tmp_path))
    u._update_dir.mkdir(parents=True, exist_ok=True)
    u._state_path.write_text(
        json.dumps({"target_version": "0.2.0", "attempts": 1, "staged_at": time.time()})
    )
    (u._update_dir / "pkg.zip").write_bytes(b"leftover")

    result = u.reconcile_after_restart("0.2.0")

    assert result is not None
    assert result["state"] == "ok"
    assert not u._state_path.exists()
    assert not (u._update_dir / "pkg.zip").exists()


def test_reconcile_stale_stage_reports_failed_and_keeps_state(tmp_path) -> None:
    u = Updater(_cfg(tmp_path))
    u._update_dir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 1000  # older than the 900s stale threshold
    u._state_path.write_text(
        json.dumps({"target_version": "0.2.0", "attempts": 1, "staged_at": old})
    )

    result = u.reconcile_after_restart("0.1.0")

    assert result is not None
    assert result["state"] == "failed"
    assert u._state_path.exists()  # left in place -- the attempts ceiling keeps working


def test_reconcile_fresh_stage_returns_none(tmp_path) -> None:
    u = Updater(_cfg(tmp_path))
    u._update_dir.mkdir(parents=True, exist_ok=True)
    u._state_path.write_text(
        json.dumps({"target_version": "0.2.0", "attempts": 1, "staged_at": time.time()})
    )
    assert u.reconcile_after_restart("0.1.0") is None


# --------------------------------------------------------------------------- #
# P0-4 (stoperrors.md): update_hmac_secret must be a separate key from
# ingest_token -- ingest_token also rides a plaintext bearer header on every
# ordinary request, so reusing it as the update-signing key let a passive LAN
# eavesdropper forge a valid manifest.
# --------------------------------------------------------------------------- #
def test_signing_secret_prefers_update_hmac_secret(tmp_path) -> None:
    u = Updater(_cfg(tmp_path, ingest_token="tok", update_hmac_secret="secret2"))
    assert u._signing_secret() == "secret2"


def test_signing_secret_falls_back_to_ingest_token(tmp_path) -> None:
    u = Updater(_cfg(tmp_path, ingest_token="tok"))  # no update_hmac_secret
    assert u._signing_secret() == "tok"


def test_check_frozen_verifies_against_update_hmac_secret_not_ingest_token(
    tmp_path, monkeypatch
) -> None:
    """Compromising ingest_token alone must no longer be enough to forge a
    manifest once update_hmac_secret is configured."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok", update_hmac_secret="secret2")
    u = Updater(cfg)
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    # Signed with the OLD key (ingest_token) -- must be rejected now.
    manifest = json.dumps(
        {
            "version": "9.9.9",
            "sha256": digest,
            "size": len(zip_bytes),
            "hmac": compute_hmac("tok", "9.9.9", digest),
        }
    ).encode()
    monkeypatch.setattr(
        updater_mod.urllib.request,
        "urlopen",
        _urlopen_routed({_manifest_url(cfg): manifest, _package_url(cfg): zip_bytes}),
    )
    monkeypatch.setattr(
        updater_mod.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(AssertionError("schtasks must not run")),
    )

    payload, restart = u.check("0.1.0")

    assert restart is False
    assert payload is not None and payload["state"] == "failed"


def test_check_frozen_happy_path_with_update_hmac_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok", update_hmac_secret="secret2")
    u = Updater(cfg)
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    manifest = json.dumps(
        {
            "version": "9.9.9",
            "sha256": digest,
            "size": len(zip_bytes),
            "hmac": compute_hmac("secret2", "9.9.9", digest),
        }
    ).encode()
    monkeypatch.setattr(
        updater_mod.urllib.request,
        "urlopen",
        _urlopen_routed({_manifest_url(cfg): manifest, _package_url(cfg): zip_bytes}),
    )
    calls = []
    monkeypatch.setattr(
        updater_mod.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )

    payload, restart = u.check("0.1.0")

    assert payload is not None and payload["state"] == "updating"
    assert restart is True
    assert len(calls) == 2
