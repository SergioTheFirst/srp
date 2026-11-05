"""Agent self-update tests: pure functions + Updater with urllib/subprocess stubbed.

No real network or schtasks call happens here: ``urllib.request.urlopen`` and
``subprocess.run`` are monkeypatched in the ``client.updater`` namespace, the
same way ``test_transport.py`` stubs ``Transport._deliver`` to stay fast and
deterministic.
"""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import subprocess
import sys
import time
import urllib.error
import zipfile
from typing import Any

import pytest
from client import ed25519
from client import updater as updater_mod
from client.config import ClientConfig
from client.updater import Updater, safe_extract, select_update, signed_message

pytestmark = pytest.mark.unit

# Тестовый ключ сборки: агент «собран» с его открытой половиной (фикстура ниже).
_SEED = bytes(range(32))
_OTHER_SEED = bytes(range(1, 33))  # чужой ключ / второй доверенный (спасатель)
_PUB = ed25519.public_key(_SEED).hex()
_OTHER_PUB = ed25519.public_key(_OTHER_SEED).hex()


@pytest.fixture(autouse=True)
def _embedded_key(monkeypatch) -> None:
    monkeypatch.setattr(updater_mod, "TRUSTED_KEYS", (_PUB,))


@pytest.fixture(autouse=True)
def _exe_in_tmp(tmp_path, monkeypatch) -> None:
    """Frozen-агент держит папку update рядом со своим exe: в тестах exe -- в tmp_path,
    там же, где буфер из _cfg, иначе frozen-тесты писали бы в папку python.exe."""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "srp-agent.exe"))


def test_frozen_update_dir_stays_next_to_the_exe_whatever_buffer_path_says(
    tmp_path, monkeypatch
) -> None:
    """Ревью 0.3.3: buffer_path из config.json (подложенный или кривой) уводил staging --
    откуда SYSTEM запускает setup.exe -- в папку, куда пишет обычный пользователь."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    exe_dir = tmp_path / "SRP"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "executable", str(exe_dir / "srp-agent.exe"))
    elsewhere = tmp_path / "Users" / "Public" / "buffer.jsonl"

    u = Updater(_cfg(tmp_path, buffer_path=str(elsewhere)))

    assert u._staging_dir == exe_dir / "update" / "staging"
    assert u._state_path == exe_dir / "update" / "state.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cfg(tmp_path, **overrides) -> ClientConfig:
    base: dict[str, Any] = {
        "server_url": "http://127.0.0.1:9",
        "device_id": "t-dev",
        "buffer_path": str(tmp_path / "buffer.jsonl"),
    }
    base.update(overrides)
    return ClientConfig(**base)


def _manifest(**overrides) -> dict:
    base = {
        "version": "0.2.0",
        "sha256": "a" * 64,
        "size": 1024,
        "signature": "b" * 128,
        "public_key": _PUB,
    }
    base.update(overrides)
    return base


def _signed(version: str, sha256: str, size: int, seed: bytes = _SEED) -> dict:
    """Манифест так, как его подписывает srp_packaging.update_signing на сборке."""
    signature = ed25519.sign(seed, signed_message(version, sha256))
    return {
        "version": version,
        "sha256": sha256,
        "size": size,
        "signature": signature.hex(),
        "public_key": ed25519.public_key(seed).hex(),
    }


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
        ("١.٢.٣", None),  # int() принял бы арабские цифры; в подписанном сообщении -- только ASCII
    ],
)
def test_parse_version(value, expected) -> None:
    assert updater_mod._parse_version(value) == expected


# --------------------------------------------------------------------------- #
# select_update
# --------------------------------------------------------------------------- #
def test_select_update_returns_manifest_when_newer() -> None:
    assert select_update(_manifest(), "0.1.0") == _manifest()


def test_select_update_none_when_equal() -> None:
    assert select_update(_manifest(version="0.1.0"), "0.1.0") is None


def test_select_update_none_when_older() -> None:
    assert select_update(_manifest(version="0.1.0"), "0.2.0") is None


def test_select_update_none_on_bad_version() -> None:
    assert select_update(_manifest(version="bad"), "0.1.0") is None


@pytest.mark.parametrize(
    "sha256", ["not-hex-not-64-chars", "A" * 64, " " + "a" * 63, "0x" + "a" * 62]
)
def test_select_update_none_on_bad_sha256(sha256) -> None:
    # int(x, 16) принимал «0x», пробелы и «_» -- проверка теперь по набору символов.
    assert select_update(_manifest(sha256=sha256), "0.1.0") is None


def test_select_update_none_on_oversized() -> None:
    huge = 200 * 1024 * 1024 + 1
    assert select_update(_manifest(size=huge), "0.1.0") is None


def test_select_update_none_on_zero_size() -> None:
    assert select_update(_manifest(size=0), "0.1.0") is None


def test_select_update_requires_a_signature() -> None:
    # fail-closed: MITM, срезавший подпись, не превращает проверку в «только sha256»
    unsigned = _manifest()
    del unsigned["signature"]
    assert select_update(unsigned, "0.1.0") is None


@pytest.mark.parametrize(
    "signature",
    ["B" * 128, "b" * 127, "b" * 130, "z" * 128, "é" * 128, "0x" + "b" * 126, 7, None],
)
def test_select_update_rejects_a_malformed_signature(signature) -> None:
    assert select_update(_manifest(signature=signature), "0.1.0") is None


# --------------------------------------------------------------------------- #
# signed_message -- контракт между сборкой и агентами РАЗНЫХ версий
# --------------------------------------------------------------------------- #
def test_signed_message_is_the_exact_build_contract() -> None:
    # Агент 0.3.3 проверяет подписи, которые поставят будущие сборки: смена формата
    # = весь парк 0.3.3 отвергает следующие пакеты.
    assert signed_message("0.3.3", "a" * 64) == b"srp-agent-update:0.3.3:" + b"a" * 64


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


def test_safe_extract_rejects_oversized_uncompressed_total(tmp_path, monkeypatch) -> None:
    """P1-8: _MAX_PACKAGE_SIZE only bounds the downloaded (compressed) size --
    a small, highly-compressible archive could otherwise unpack into far more
    data than the disk can hold. safe_extract must reject an oversized total
    declared file_size from ZipInfo metadata alone, BEFORE ever calling
    extractall or touching the destination directory -- no real gigabytes of
    data are decompressed to prove this."""
    zpath = tmp_path / "pkg.zip"
    zpath.write_bytes(_zip_bytes(_good_members()))
    dest = tmp_path / "staging"

    class _FakeInfo:
        def __init__(self, filename: str, file_size: int) -> None:
            self.filename = filename
            self.file_size = file_size

    huge = updater_mod._MAX_UNCOMPRESSED_SIZE + 1
    fake_infos = [_FakeInfo("setup.exe", huge), _FakeInfo("payload/srp-agent.exe", 1)]
    monkeypatch.setattr(zipfile.ZipFile, "infolist", lambda self: fake_infos)
    monkeypatch.setattr(
        zipfile.ZipFile,
        "extractall",
        lambda self, *a, **kw: (_ for _ in ()).throw(
            AssertionError("must not extract before the uncompressed-size cap check")
        ),
    )

    with pytest.raises(ValueError, match="объём"):
        safe_extract(zpath, dest)

    assert not dest.exists()  # nothing was even created, let alone written to


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


def test_check_offline_mode_short_circuits(tmp_path, monkeypatch) -> None:
    u = Updater(_cfg(tmp_path, offline_mode=True))

    def _urlopen(req, timeout=None):
        raise AssertionError("must not be called when offline_mode is true")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen)
    assert u.check("0.1.0") == (None, False)


def test_check_offline_mode_does_not_raise_on_empty_server_url(tmp_path) -> None:
    # P1-1: the documented offline setup (server_url="", offline_mode=True) used
    # to raise ValueError building the manifest request (urllib.request.Request()
    # on a schemeless relative URL) -- outside _fetch_manifest()'s own try/except,
    # same shape as the transport.py bug this ticket started with. Real Updater,
    # no urlopen mocking -- this must not raise.
    u = Updater(_cfg(tmp_path, server_url="", offline_mode=True))
    assert u.check("0.1.0") == (None, False)


def test_check_not_found_then_no_repeat_until_state_changes(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    u = Updater(cfg)
    err = urllib.error.HTTPError(
        _manifest_url(cfg), 404, "not found", email.message.Message(), None
    )
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
    manifest = json.dumps(_manifest(version="9.9.9", size=10)).encode()

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


def test_check_frozen_without_token_now_applies(tmp_path, monkeypatch) -> None:
    """0.3.3: подлинность -- подпись сборки, а не ingest_token. Агент без токена
    (раньше «не качать вовсе») ставит пакет, подпись которого сходится."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path)  # no ingest_token
    zip_bytes = _zip_bytes(_good_members())
    manifest = _signed("9.9.9", hashlib.sha256(zip_bytes).hexdigest(), len(zip_bytes))
    routes = {_manifest_url(cfg): json.dumps(manifest).encode(), _package_url(cfg): zip_bytes}
    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen_routed(routes))
    monkeypatch.setattr(
        updater_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    )

    payload, restart = Updater(cfg).check("0.1.0")

    assert restart is True
    assert payload is not None and payload["state"] == "updating"


def test_check_frozen_happy_path_applies_update(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok")
    u = Updater(cfg)
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    manifest = json.dumps(_signed("9.9.9", digest, len(zip_bytes))).encode()
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
    manifest = json.dumps(_signed("9.9.9", digest, len(zip_bytes))).encode()
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
    manifest = json.dumps(_signed("9.9.9", "a" * 64, 10)).encode()

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


def test_check_bad_signature_does_not_burn_attempt(tmp_path, monkeypatch) -> None:
    """P1-7: a pure signature failure (forged signature, no real package ever
    fetched) must not count against _MAX_ATTEMPTS. Otherwise a MITM able to
    tamper with the manifest could fail the check 3 times running and
    permanently block a legitimate version until an operator manually clears
    update/state.json."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok")
    u = Updater(cfg)
    forged = _signed("9.9.9", "a" * 64, 10, seed=_OTHER_SEED)
    forged["public_key"] = _PUB  # подделка выдаёт себя за наш ключ
    monkeypatch.setattr(
        updater_mod.urllib.request,
        "urlopen",
        _urlopen_routed({_manifest_url(cfg): json.dumps(forged).encode()}),
    )
    monkeypatch.setattr(
        updater_mod.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(
            AssertionError("must not download/schtasks on a signature failure")
        ),
    )

    for _ in range(updater_mod._MAX_ATTEMPTS):
        payload, restart = u.check("0.1.0")
        assert restart is False
        assert payload is not None and payload["state"] == "failed"
        assert payload["error"] == "подпись пакета не сошлась"

    assert u._attempts_for("9.9.9") == 0  # pure signature failures never burned an attempt
    assert not u._state_path.exists()  # no state was ever written, not just a zeroed counter

    # A genuine manifest for the same version must still be applicable afterwards --
    # the version was never permanently blocked by the earlier forged manifests.
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    good_manifest = json.dumps(_signed("9.9.9", digest, len(zip_bytes))).encode()
    monkeypatch.setattr(
        updater_mod.urllib.request,
        "urlopen",
        _urlopen_routed({_manifest_url(cfg): good_manifest, _package_url(cfg): zip_bytes}),
    )
    monkeypatch.setattr(
        updater_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    )

    payload, restart = u.check("0.1.0")

    assert restart is True
    assert payload is not None and payload["state"] == "updating"


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
# 0.3.3: подпись сборки Ed25519; 0.3.7: ключи проверки вшиты в агента списком из git
# (client/update_trust.py: основной + спасатель), годится подпись любым из них;
# любой провал проверки -- ДО state.json (P1-7: попытка не сгорает).
# --------------------------------------------------------------------------- #
def _frozen_updater(tmp_path, monkeypatch, manifest: dict, zip_bytes: bytes = b""):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path, ingest_token="tok")
    routes = {_manifest_url(cfg): json.dumps(manifest).encode()}
    if zip_bytes:
        routes[_package_url(cfg)] = zip_bytes
    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen_routed(routes))
    return Updater(cfg)


def _no_schtasks(monkeypatch) -> None:
    monkeypatch.setattr(
        updater_mod.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(AssertionError("schtasks must not run")),
    )


def _no_download(monkeypatch, u: Updater) -> None:
    def _forbidden(*_a, **_k):
        raise AssertionError("a package that failed the signature check must not be fetched")

    monkeypatch.setattr(u, "_download", _forbidden)


def _assert_refused(u: Updater, error: str) -> None:
    payload, restart = u.check("0.1.0")
    assert restart is False
    assert payload is not None and payload["state"] == "failed"
    assert payload["error"] == error
    assert payload["available_version"] == "9.9.9"
    assert not u._state_path.exists()  # P1-7: a signature failure burns no attempt


@pytest.mark.parametrize("embedded", [(), ("zz",), ("A" * 64,), ("",)])
def test_check_refuses_when_the_agent_was_built_without_a_key(
    tmp_path, monkeypatch, embedded
) -> None:
    # публичное дерево, free-сборка: client/update_trust.py нет -> TRUSTED_KEYS = ()
    monkeypatch.setattr(updater_mod, "TRUSTED_KEYS", embedded)
    u = _frozen_updater(tmp_path, monkeypatch, _signed("9.9.9", "a" * 64, 10))
    _no_download(monkeypatch, u)
    _no_schtasks(monkeypatch)

    _assert_refused(u, "агент собран без ключа проверки подписи")


def test_check_refuses_a_package_signed_with_another_key(tmp_path, monkeypatch) -> None:
    # Пакет, собранный на другой машине (свой .signing): подпись верна, но не нашим ключом.
    manifest = _signed("9.9.9", "a" * 64, 10, seed=_OTHER_SEED)
    u = _frozen_updater(tmp_path, monkeypatch, manifest)
    _no_download(monkeypatch, u)
    _no_schtasks(monkeypatch)

    other = manifest["public_key"]
    _assert_refused(u, f"пакет подписан другим ключом (агент {_PUB[:8]}, пакет {other[:8]})")


def test_refusal_names_every_key_the_agent_trusts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(updater_mod, "TRUSTED_KEYS", (_PUB, _OTHER_PUB))
    manifest = _signed("9.9.9", "a" * 64, 10, seed=bytes(range(2, 34)))
    u = _frozen_updater(tmp_path, monkeypatch, manifest)
    _no_download(monkeypatch, u)
    _no_schtasks(monkeypatch)

    mine = f"{_PUB[:8]},{_OTHER_PUB[:8]}"
    theirs = manifest["public_key"][:8]
    _assert_refused(u, f"пакет подписан другим ключом (агент {mine}, пакет {theirs})")


def test_check_applies_a_package_signed_by_any_trusted_key(tmp_path, monkeypatch) -> None:
    # Релиз, подписанный вторым ключом списка (спасателем), ставится так же, как основным.
    monkeypatch.setattr(updater_mod, "TRUSTED_KEYS", (_PUB, _OTHER_PUB))
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    manifest = _signed("9.9.9", digest, len(zip_bytes), seed=_OTHER_SEED)
    u = _frozen_updater(tmp_path, monkeypatch, manifest, zip_bytes)
    monkeypatch.setattr(
        updater_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    )

    payload, restart = u.check("0.1.0")

    assert restart is True and payload is not None and payload["state"] == "updating"


@pytest.mark.parametrize("public_key", [None, "", "C" * 64, "c" * 63, 7])
def test_check_newer_manifest_without_a_valid_public_key_is_refused(
    tmp_path, monkeypatch, public_key
) -> None:
    """Ключ пакета обязателен: по нему агент выбирает, каким из своих ключей проверять."""
    manifest = _signed("9.9.9", "a" * 64, 10)
    if public_key is None:
        del manifest["public_key"]
    else:
        manifest["public_key"] = public_key
    u = _frozen_updater(tmp_path, monkeypatch, manifest)
    _no_download(monkeypatch, u)
    _no_schtasks(monkeypatch)

    _assert_refused(u, "манифест обновления отклонён: нет подписи или неверный формат")


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "9.9.8"),  # подпись старого релиза, перевешенная на новую версию
        ("sha256", "c" * 64),  # подпись настоящего релиза на чужом архиве
    ],
)
def test_check_signature_binds_version_and_sha256(tmp_path, monkeypatch, field, value) -> None:
    manifest = _signed("9.9.9", "a" * 64, 10)
    signed_for = dict(manifest, **{field: value})
    resigned = _signed(signed_for["version"], signed_for["sha256"], 10)
    manifest["signature"] = resigned["signature"]
    u = _frozen_updater(tmp_path, monkeypatch, manifest)
    _no_download(monkeypatch, u)
    _no_schtasks(monkeypatch)

    _assert_refused(u, "подпись пакета не сошлась")


@pytest.mark.parametrize("signature", ["B" * 128, "b" * 64, "é" * 128, None])
def test_check_malformed_signature_is_a_refused_newer_not_ok(
    tmp_path, monkeypatch, signature
) -> None:
    """AUTH-5/VR-8: новая версия с подписью не того вида -- НЕ «всё ок»
    (UNKNOWN over false confidence) и не падение; попытка не сгорает."""
    manifest = dict(_signed("9.9.9", "a" * 64, 10), signature=signature)
    u = _frozen_updater(tmp_path, monkeypatch, manifest)
    _no_download(monkeypatch, u)
    _no_schtasks(monkeypatch)

    _assert_refused(u, "манифест обновления отклонён: нет подписи или неверный формат")


def test_check_newer_unsigned_manifest_is_failed_not_ok(tmp_path, monkeypatch) -> None:
    """AUTH-5/VR-8: агент отверг новую версию без подписи -- это НЕ «всё ок»
    (UNKNOWN over false confidence); попытка не сгорает."""
    manifest = {"version": "9.9.9", "sha256": "a" * 64, "size": 10}
    u = _frozen_updater(tmp_path, monkeypatch, manifest)
    _no_schtasks(monkeypatch)

    _assert_refused(u, "манифест обновления отклонён: нет подписи или неверный формат")


def test_check_unsigned_manifest_for_current_version_is_ok(tmp_path, monkeypatch) -> None:
    manifest = {"version": "0.1.0", "sha256": "a" * 64, "size": 10}
    u = _frozen_updater(tmp_path, monkeypatch, manifest)

    payload, _ = u.check("0.1.0")

    assert payload is not None and payload["state"] == "ok"


def test_update_task_run_ignores_battery_constraints() -> None:
    """Задача без XML получает DisallowStartIfOnBatteries=true: на батарее /run
    молча не стартует setup.exe. /I -- «игнорировать любые ограничения»."""
    assert "/I" in updater_mod.update_task_run_cmd()


def test_attempts_cap_allows_one_retry_per_day(tmp_path, monkeypatch) -> None:
    """Лимит 3 попыток больше не вечен: через сутки после последней -- ещё одна."""
    zip_bytes = _zip_bytes(_good_members())
    digest = hashlib.sha256(zip_bytes).hexdigest()
    manifest = _signed("9.9.9", digest, len(zip_bytes))
    u = _frozen_updater(tmp_path, monkeypatch, manifest, zip_bytes)
    u._update_dir.mkdir(parents=True, exist_ok=True)
    day_ago = time.time() - updater_mod._ATTEMPT_RETRY_SEC - 60
    u._state_path.write_text(
        json.dumps({"target_version": "9.9.9", "attempts": 3, "staged_at": day_ago})
    )
    monkeypatch.setattr(
        updater_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    )

    payload, restart = u.check("0.1.0")

    assert restart is True and payload["state"] == "updating"
    state = json.loads(u._state_path.read_text())
    assert state["attempts"] == 3  # still capped -- the next retry is a day away again
    assert state["staged_at"] > day_ago

    payload, restart = u.check("0.1.0")
    assert restart is False and payload["state"] == "failed"
    assert "лимит" in payload["error"]


def test_apply_exception_is_reported_as_failed(tmp_path, monkeypatch) -> None:
    """AGT-APPLY-8: исключение мимо узких except в _apply уходило в лог агента без
    статуса -- дашборд показывал прежнее «ok»/«обновляется»."""
    u = _frozen_updater(tmp_path, monkeypatch, _signed("9.9.9", "a" * 64, 10))

    def _boom(*_a, **_k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(u, "_download", _boom)

    payload, restart = u.check("0.1.0")

    assert restart is False
    assert payload is not None and payload["state"] == "failed"
    assert "RuntimeError" in payload["error"]


def test_gate_resends_ok_once_a_day(tmp_path, monkeypatch) -> None:
    """AGT-STATUS-7: подавление повторного «ok» застывало update_checked_at на
    времени старта агента -- «проверка обновления: 3 нед. назад» у живого ПК."""
    u = Updater(_cfg(tmp_path))
    now = [1_000_000.0]
    monkeypatch.setattr(updater_mod.time, "time", lambda: now[0])
    ok = {"checked_at": "x", "state": "ok"}

    assert u._gate(dict(ok)) is not None
    assert u._gate(dict(ok)) is None
    now[0] += 86400
    assert u._gate(dict(ok)) is not None


def test_handoff_failed_names_the_staged_target(tmp_path) -> None:
    u = Updater(_cfg(tmp_path))
    u._update_dir.mkdir(parents=True, exist_ok=True)
    u._state_path.write_text(json.dumps({"target_version": "9.9.9", "attempts": 1}))

    payload = u.handoff_failed()

    assert payload["state"] == "failed"
    assert payload["available_version"] == "9.9.9"
    assert "задача обновления" in payload["error"]


def test_reconcile_newer_than_target_clears_state(tmp_path) -> None:
    """VR-10: ПК, вручную переустановленный на версию НОВЕЕ застрявшей цели, больше
    не шлёт ложный failed после каждого старта."""
    u = Updater(_cfg(tmp_path))
    u._update_dir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 5000
    u._state_path.write_text(
        json.dumps({"target_version": "0.2.0", "attempts": 3, "staged_at": old})
    )

    result = u.reconcile_after_restart("0.3.0")

    assert result is not None and result["state"] == "ok"
    assert not u._state_path.exists()


def test_reconcile_reports_setup_rollback_reason_at_once(tmp_path) -> None:
    """setup.exe пишет причину отката в state.json -- агент сообщает её сразу, без
    15-минутной паузы (раньше причина жила только в install.log на ПК)."""
    u = Updater(_cfg(tmp_path))
    u._update_dir.mkdir(parents=True, exist_ok=True)
    u._state_path.write_text(
        json.dumps(
            {
                "target_version": "0.2.0",
                "attempts": 1,
                "staged_at": time.time(),
                "setup_error": "не удалось скопировать файлы (robocopy)",
            }
        )
    )

    result = u.reconcile_after_restart("0.1.0")

    assert result is not None and result["state"] == "failed"
    assert "не удалось скопировать файлы" in result["error"]
    assert u._state_path.exists()  # attempts ceiling keeps working


def test_run_schtasks_is_bounded_and_never_raises(monkeypatch) -> None:
    """o5-C7: зависший schtasks (заклинивший Task Scheduler) подвешивал агентский
    цикл навсегда; ненулевой код -> вызывающий отчитается о неудаче, цикл живёт."""
    import subprocess

    from client import updater as up

    seen: dict = {}

    def _hang(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(up.subprocess, "run", _hang)
    assert up._run_schtasks(["schtasks", "/query"]) == 1
    assert "timeout" in seen


def test_network_helpers_survive_a_non_http_reply(tmp_path, monkeypatch) -> None:
    """Ревью 2026-10-15: http.client.HTTPException (BadStatusLine, IncompleteRead) --
    не OSError; транспорт уже ловил, а updater -- нет (трейсбек каждый час, .part
    оставался на диске)."""
    import http.client

    u = Updater(_cfg(tmp_path, ingest_token="tok"))

    def _garbage(req, timeout=None):
        raise http.client.BadStatusLine("x")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _garbage)
    assert u._fetch_manifest() == ("error", None)

    class _CutBody(io.BytesIO):
        def read(self, *_a):
            raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", lambda req, timeout=None: _CutBody())
    assert u._download(_package_url(u._cfg), "a" * 64, 10) is None
    assert not (u._update_dir / "pkg.zip.part").exists()


# --------------------------------------------------------------------------- #
# 0.3.6: x86 build asks for ?arch=x86 and refuses a package of the other bitness
# --------------------------------------------------------------------------- #
def _frozen_x86_routes(cfg: ClientConfig, monkeypatch, manifest_arch: Any) -> dict:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater_mod, "AGENT_ARCH", "x86")
    zip_bytes = _zip_bytes(_good_members())
    manifest = _signed("9.9.9", hashlib.sha256(zip_bytes).hexdigest(), len(zip_bytes))
    if manifest_arch is not None:
        manifest["arch"] = manifest_arch
    return {
        _manifest_url(cfg) + "?arch=x86": json.dumps(manifest).encode(),
        _package_url(cfg) + "?arch=x86": zip_bytes,
    }


def test_x86_agent_fetches_and_applies_the_x86_package(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    routes = _frozen_x86_routes(cfg, monkeypatch, "x86")
    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen_routed(routes))
    monkeypatch.setattr(
        updater_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0)
    )

    payload, restart = Updater(cfg).check("0.1.0")

    assert restart is True
    assert payload is not None and payload["state"] == "updating"


@pytest.mark.parametrize("manifest_arch", ["x64", None, "arm64"])
def test_x86_agent_refuses_a_package_of_another_bitness(
    tmp_path, monkeypatch, manifest_arch
) -> None:
    cfg = _cfg(tmp_path)
    routes = _frozen_x86_routes(cfg, monkeypatch, manifest_arch)
    del routes[_package_url(cfg) + "?arch=x86"]  # KeyError if it ever downloads
    monkeypatch.setattr(updater_mod.urllib.request, "urlopen", _urlopen_routed(routes))

    payload, restart = Updater(cfg).check("0.1.0")

    assert restart is False
    assert payload is not None and payload["state"] == "failed"
    assert "разрядност" in payload["error"]
    assert payload["available_version"] == "9.9.9"


def test_x64_agent_refuses_an_x86_package_and_sends_no_arch_param(monkeypatch) -> None:
    monkeypatch.setattr(updater_mod, "AGENT_ARCH", "x64")
    assert select_update(_manifest(version="9.9.9", arch="x86"), "0.1.0") is None
    assert select_update(_manifest(version="9.9.9", arch="x64"), "0.1.0") is not None
    assert select_update(_manifest(version="9.9.9"), "0.1.0") is not None  # до 0.3.6 = x64
    assert updater_mod._arch_query() == ""  # старый сервер и x64-агент -- как до 0.3.6
