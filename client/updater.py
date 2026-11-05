"""Agent self-update: check the server's manifest, verify, stage, hand off.

Pure stdlib (urllib + zipfile + client.ed25519) -- like the rest of client/,
this module carries zero third-party dependencies. Mechanism: fetch a small JSON
manifest from the server -> compare its version against ours -> verify the
build's Ed25519 signature -> download the zip -> verify size + sha256 -> unpack
into ``update/staging`` -> register and fire a ONE-SHOT SYSTEM scheduled task
("SRP Agent Update") that runs ``staging\\setup.exe --update``. setup.exe stops
this process itself (schtasks /end + taskkill); the agent does NOT exit on its
own -- if it is still alive minutes later, the task never started (AV, policy)
and the agent reports that instead of sitting dead until the next real boot.

A dedicated scheduled task is used instead of spawning setup.exe as a child
process: Task Scheduler kills every process in a task's job object when the
task's own process exits, so a child updater would die together with the agent
it is trying to replace. A second, independent task has its own job and
survives this process exiting.

Authenticity (0.3.3): the build machine -- the server, via update.bat -- signs
``srp-agent-update:<version>:<sha256>`` (:func:`signed_message`) with an Ed25519
key that never leaves it (``.signing``); the agent verifies the signature with a
public key baked into its own binary. 0.3.7: the agent trusts the list committed in
``client/update_trust.py`` (main key + an offline recovery key) instead of whatever
key the build machine happened to hold -- a share built on one machine and a package
signed on another no longer strand the fleet. Nothing an agent or a
LAN observer holds can forge a package -- unlike the 0.3.0-0.3.2 HMAC keyed by
ingest_token, which rides plain http in every request and sits in a config every
user of every PC can read. The token therefore no longer gates updates (it still
authenticates the HTTP calls when configured). An agent built without the key
(dev run, public tree, free edition) refuses every package. The sha256 inside the
signed message binds the signature to the exact zip; only a strictly newer
version is ever applied, which closes off downgrade/replay of an old, once-valid
manifest. Every signature failure is decided BEFORE state.json is written, so a
MITM tampering with the (unauthenticated) manifest cannot burn the attempts.

Residual risk (documented): whoever controls the build machine controls the
fleet; a MITM can still withhold updates (freeze), never substitute one.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import os
import shutil
import subprocess  # nosec B404 -- see _run_schtasks: static argv, shell=False
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from client import ed25519
from client.config import ClientConfig
from client.winflags import AGENT_ARCH, NO_WINDOW

try:
    # Список в git (0.3.7). Публичное дерево и free-сборка модуля не несут -> ключей нет ->
    # агент отказывается от любого пакета («агент собран без ключа проверки подписи»).
    from client.update_trust import TRUSTED_KEYS
except ImportError:
    TRUSTED_KEYS = ()

log = logging.getLogger(__name__)

TASK_NAME = "SRP Agent Update"
_MAX_PACKAGE_SIZE = 200 * 1024 * 1024  # 200 MiB cap, mirrors the server-side manifest guard
# zip-bomb guard (P1-8): _MAX_PACKAGE_SIZE only bounds the DOWNLOADED (compressed)
# size -- a small, highly-compressible archive could otherwise unpack into far more
# data than the disk can hold. A real agent package (two executables) is nowhere
# near this; 10x the compressed cap is generous headroom while still bounding the DoS.
_MAX_UNCOMPRESSED_SIZE = _MAX_PACKAGE_SIZE * 10
_MAX_ATTEMPTS = 3  # per target version -- stop re-downloading a package that never applies
# ponytail: 3 quick tries, then 1/day -- a broken package can't loop, a transient
# failure (AV lock, cut download, battery) can't pin the PC to the old version.
_ATTEMPT_RETRY_SEC = 24 * 3600
_STALE_STAGE_SEC = 900  # 15 min grace period for the update task to finish the restart
# schtasks обычно отвечает за миллисекунды; заклинивший Task Scheduler не должен
# держать агентский цикл дольше этого.
_SCHTASKS_TIMEOUT_SEC = 30
_CHUNK_SIZE = 64 * 1024
_REQUIRED_MEMBERS = ("setup.exe", "payload/srp-agent.exe")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_version(value: Optional[str]) -> Optional[tuple[int, int, int]]:
    """Parse a strict MAJOR.MINOR.PATCH string into a tuple; None if malformed.

    Duplicated from shared.schema.parse_version, not imported -- client stays
    pure stdlib (same policy as transport.AGENT_VERSION vs CONTRACT_VERSION).
    """
    if not value or not isinstance(value, str) or not value.isascii():
        return None  # int() принял бы и арабские цифры; подписанное сообщение -- ASCII
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    return (nums[0], nums[1], nums[2])


_HEX_DIGITS = frozenset("0123456789abcdef")


def signed_message(version: str, sha256_hex: str) -> bytes:
    """What the build signs and the agent verifies -- a contract between binaries of
    DIFFERENT versions (agent 0.3.3 checks packages signed by future builds).
    Domain-separated; both fields are strictly validated before use (no ":")."""
    return f"srp-agent-update:{version}:{sha256_hex}".encode("ascii")


def _is_hex(value: Any, length: int) -> bool:
    """Exactly *length* lowercase hex digits -- the only spelling the build writes.
    A character check, not int(x, 16): that accepts "0x", "_", signs and spaces."""
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX_DIGITS


def _arch_query() -> str:
    """``?arch=x86`` for the 32-bit build only: no parameter = x64, as before 0.3.6."""
    return "?arch=x86" if AGENT_ARCH == "x86" else ""


def _arch_matches(manifest: dict) -> bool:
    """The package was built for THIS agent's bitness (no ``arch`` = x64, pre-0.3.6).

    ponytail: ``arch`` is NOT signed on purpose -- the signed message format
    (:func:`signed_message`) is a contract with already-deployed agents. A swapped
    arch never runs foreign code (the signature still binds the exact zip) and
    replaces nothing: an x86 setup.exe refuses in ``_dispatch`` before
    ``run_update`` on a 64-bit OS, an x64 one never starts on 32-bit Windows --
    either way ``handoff_failed``. Sign it once the contract has a version gate."""
    return manifest.get("arch", "x64") == AGENT_ARCH


def select_update(manifest: dict, current_version: str) -> Optional[dict]:
    """Validate *manifest* and return it iff it names a strictly newer version.

    Fail-closed: any malformed field -- including a missing or mangled
    ``signature`` (a stripping MITM must not downgrade us to sha256-only) -- is
    treated as "nothing to do" rather than guessed at; check() reports a newer
    version refused this way as failed. An equal or older remote version is
    also None -- guards against downgrade/replay of an old, once-valid manifest.
    """
    if not isinstance(manifest, dict):
        return None
    remote = _parse_version(manifest.get("version"))
    current = _parse_version(current_version)
    if remote is None or current is None:
        return None
    if not _is_hex(manifest.get("sha256"), 64):
        return None
    size = manifest.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not (0 < size <= _MAX_PACKAGE_SIZE):
        return None
    if not _is_hex(manifest.get("signature"), 128):
        return None
    if not _is_hex(manifest.get("public_key"), 64):  # 0.3.7: по нему выбирается ключ проверки
        return None
    if not _arch_matches(manifest):
        return None
    if remote <= current:
        return None
    return manifest


def _signature_refusal(manifest: dict, version: str) -> str:
    """Why *manifest* (already through select_update) fails the build-time Ed25519
    check; "" when it verifies under one of the keys baked into this agent."""
    trusted = [key for key in TRUSTED_KEYS if _is_hex(key, 64)]
    if not trusted:
        return "агент собран без ключа проверки подписи"
    package_key = manifest["public_key"]
    if package_key not in trusted:
        mine = ",".join(key[:8] for key in trusted)
        return f"пакет подписан другим ключом (агент {mine}, пакет {package_key[:8]})"
    message = signed_message(version, manifest["sha256"])
    signature = bytes.fromhex(manifest["signature"])
    if not ed25519.verify(bytes.fromhex(package_key), message, signature):
        return "подпись пакета не сошлась"
    return ""


def _refused_newer(manifest: Optional[dict], current_version: str) -> Optional[str]:
    """The manifest's version string iff it is newer than ours -- for reporting a
    manifest select_update refused (unsigned/malformed) as failed, not "ok"."""
    raw = manifest.get("version") if isinstance(manifest, dict) else None
    remote, current = _parse_version(raw), _parse_version(current_version)
    if remote is None or current is None or remote <= current:
        return None
    return str(raw)


def _check_member_name(name: str) -> None:
    """Zip-slip guard for one archive member name."""
    if not name or name.startswith("/") or name.startswith("\\") or ":" in name:
        raise ValueError(f"unsafe path in update package: {name!r}")
    if any(segment == ".." for segment in name.replace("\\", "/").split("/")):
        raise ValueError(f"unsafe path in update package: {name!r}")


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract *zip_path* into *dest*, which is wiped first.

    Every member name is validated, and the total UNCOMPRESSED size capped,
    BEFORE any file touches disk: absolute paths, drive letters, leading
    slashes/backslashes and ".." segments are all rejected (split on both "/"
    and "\\" -- the zip spec stores forward slashes, but a hostile archive can
    still smuggle a backslash for Windows); total size is summed from ZipInfo
    metadata alone, never by actually decompressing anything.
    """
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for info in infos:
            _check_member_name(info.filename)
        total_size = sum(info.file_size for info in infos)
        if total_size > _MAX_UNCOMPRESSED_SIZE:
            raise ValueError("пакет обновления распаковывается в слишком большой объём данных")
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        zf.extractall(dest)
    for rel in _REQUIRED_MEMBERS:
        if not (dest / rel).exists():
            raise ValueError("пакет без обязательных файлов")


def update_task_create_cmd(setup_exe: str) -> list[str]:
    return [
        "schtasks",
        "/create",
        "/tn",
        TASK_NAME,
        "/tr",
        f'"{setup_exe}" --update',
        "/sc",
        "once",
        "/st",
        "00:00",
        "/ru",
        "SYSTEM",
        "/f",
    ]


def update_task_run_cmd() -> list[str]:
    # /I: the /sc once task gets Task Scheduler's defaults (DisallowStartIfOnBatteries)
    # -- on battery a plain /run "succeeds" yet never starts setup.exe.
    # ponytail: StopIfGoingOnBatteries stays (unplug mid-swap); register from XML if it bites.
    return ["schtasks", "/run", "/I", "/tn", TASK_NAME]


def _run_schtasks(argv: list[str]) -> int:
    # Static argv (schtasks + fixed literals / a validated staging path), shell=False.
    try:
        proc = subprocess.run(  # nosec B603
            argv, capture_output=True, timeout=_SCHTASKS_TIMEOUT_SEC, creationflags=NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        return 1  # ненулевой код -> вызывающий отчитается о неудачном обновлении, цикл живёт
    return proc.returncode


class Updater:
    """Per-agent update checker/applier bound to one :class:`ClientConfig`."""

    def __init__(self, cfg: ClientConfig) -> None:
        self._cfg = cfg
        base = cfg.resolved_buffer_path().parent
        if getattr(sys, "frozen", False):
            # SYSTEM запускает setup.exe из staging: папка обновления -- только рядом с exe
            # (там же её держит setup.exe), какой бы buffer_path ни стоял в config.json.
            base = Path(sys.executable).parent
        self._update_dir = base / "update"
        self._staging_dir = self._update_dir / "staging"
        self._state_path = self._update_dir / "state.json"
        # Last status sent this run -- lets check() skip repeating unchanged
        # good news every cycle; a "failed" always gets through (see _gate).
        self._last_status: Optional[tuple[str, Optional[str], int]] = None

    # -- public API ---------------------------------------------------------- #
    def reconcile_after_restart(self, current_version: str) -> Optional[dict]:
        """Called once at agent startup to close the loop on a staged update.

        No state.json -> nothing was ever staged, nothing to report. Target
        reached (or passed, e.g. a manual reinstall of a newer build) ->
        success, clean up. setup.exe recorded a rollback reason -> report it
        at once. Target still not reached after a generous grace period ->
        the update task likely never ran; report it but LEAVE state.json in
        place so the attempts ceiling keeps working. Staged too recently ->
        the task may still be running -- say nothing yet.
        """
        state = self._read_state()
        if state is None:
            return None
        target = state.get("target_version")
        cur, tgt = _parse_version(current_version), _parse_version(target)
        if target == current_version or (cur is not None and tgt is not None and cur >= tgt):
            self._clear_state()
            return {"checked_at": _now_iso(), "state": "ok"}
        setup_error = state.get("setup_error")
        if isinstance(setup_error, str) and setup_error:
            return self._failed(target, f"откат обновления: {setup_error}")
        staged_at = state.get("staged_at")
        if not isinstance(staged_at, (int, float)) or (time.time() - staged_at) <= _STALE_STAGE_SEC:
            return None
        return self._failed(target, "обновление не применилось после перезапуска")

    def handoff_failed(self) -> dict:
        """The agent outlived the handoff grace: setup.exe never stopped it, so
        the one-shot update task did not start (AV/EDR, policy). The attempt is
        already counted; the agent keeps working instead of staying dead."""
        target = (self._read_state() or {}).get("target_version")
        return self._failed(target, "задача обновления не запустилась (setup.exe не стартовал)")

    def check(self, current_version: str) -> tuple[Optional[dict], bool]:
        """Fetch the manifest, decide, and (in a frozen build) apply it.

        Returns (payload-to-send-or-None, restart_pending).
        """
        # P1-1: offline_mode must never attempt a server call -- server_url may be
        # empty, and urllib.request.Request() raises ValueError building a request
        # for a schemeless relative URL, outside _fetch_manifest()'s own try/except
        # (that block only wraps urlopen(), not the Request() call a line above it).
        if self._cfg.offline_mode or self._cfg.update_channel == "none":
            return (None, False)

        outcome, manifest = self._fetch_manifest()
        if outcome == "error":
            return (None, False)  # server unreachable -- stay silent, retry next cycle
        if outcome == "not_found":
            return (self._gate({"checked_at": _now_iso(), "state": "ok"}), False)

        update = select_update(manifest or {}, current_version)
        if update is None:
            refused = _refused_newer(manifest, current_version)
            if refused:  # a newer version we would not take is NOT "up to date"
                error = "манифест обновления отклонён: нет подписи или неверный формат"
                if isinstance(manifest, dict) and not _arch_matches(manifest):
                    error = f"пакет обновления другой разрядности (агент {AGENT_ARCH})"
                return (self._gate(self._failed(refused, error)), False)
            return (self._gate({"checked_at": _now_iso(), "state": "ok"}), False)

        remote = str(update["version"])
        if not getattr(sys, "frozen", False):
            log.info("update %s available but not applying -- not a frozen build", remote)
            payload = {"checked_at": _now_iso(), "state": "ok", "available_version": remote}
            return (self._gate(payload), False)

        attempts = self._attempts_for(remote)
        if attempts >= _MAX_ATTEMPTS:
            error = f"превышен лимит попыток обновления до {remote} (повтор через сутки)"
            return (self._gate(self._failed(remote, error)), False)

        try:
            return self._apply(update, remote, attempts)
        except Exception as exc:  # noqa: BLE001 -- report, don't vanish into the agent log
            log.exception("update apply failed")
            return (
                self._gate(self._failed(remote, f"сбой обновления: {type(exc).__name__}")),
                False,
            )

    # -- apply ----------------------------------------------------------------#
    def _apply(self, manifest: dict, remote: str, attempts: int) -> tuple[Optional[dict], bool]:
        """Download, verify, stage and hand off *manifest* (known to be newer).

        A pure signature failure does NOT burn an attempt (P1-7): the manifest
        fetch itself is unauthenticated, so a MITM able to tamper with it could
        otherwise fail the check 3 times running -- no download ever attempted --
        and permanently block this legitimate version until an operator clears
        update/state.json by hand. That path can't be looped to bypass this: a
        check only runs once per _UPDATE_CHECK_MIN_INTERVAL_SEC (agent.py), a
        hardcoded floor the caller cannot configure below.

        state.json is written for this attempt right after the signature check
        passes, BEFORE anything else, so a failure at any step FROM HERE ON --
        including a network failure during download -- still counts against
        the 3-attempt ceiling; otherwise a permanently broken package would be
        re-downloaded forever, every check cycle.
        """
        refusal = _signature_refusal(manifest, remote)
        if refusal:
            return (self._gate(self._failed(remote, refusal)), False)

        self._write_state(
            {"target_version": remote, "attempts": attempts + 1, "staged_at": time.time()}
        )
        pkg_url = self._cfg.server_url.rstrip("/") + "/api/v1/agent/update/package" + _arch_query()
        pkg_path = self._download(pkg_url, manifest["sha256"], int(manifest["size"]))
        if pkg_path is None:
            return (self._gate(self._failed(remote, "не удалось скачать пакет обновления")), False)

        try:
            safe_extract(pkg_path, self._staging_dir)
        except (ValueError, OSError) as exc:
            log.warning("update package extract failed: %s", exc)
            return (self._gate(self._failed(remote, "пакет обновления повреждён")), False)

        setup_exe = str(self._staging_dir / "setup.exe")
        if _run_schtasks(update_task_create_cmd(setup_exe)) != 0:
            return (self._gate(self._failed(remote, "не удалось создать задачу обновления")), False)
        if _run_schtasks(update_task_run_cmd()) != 0:
            return (
                self._gate(self._failed(remote, "не удалось запустить задачу обновления")),
                False,
            )

        payload = {"checked_at": _now_iso(), "state": "updating", "available_version": remote}
        return (self._gate(payload), True)

    # -- network --------------------------------------------------------------#
    def _fetch_manifest(self) -> tuple[str, Optional[dict]]:
        """GET the manifest. Returns ('ok'|'not_found'|'error', manifest-or-None)."""
        url = self._cfg.server_url.rstrip("/") + "/api/v1/agent/update" + _arch_query()
        headers = {"X-SRP-Token": self._cfg.ingest_token} if self._cfg.ingest_token else {}
        req = urllib.request.Request(url, headers=headers)
        try:
            # B310: scheme is the operator-configured server_url, not user input.
            with urllib.request.urlopen(req, timeout=self._cfg.http_timeout_sec) as resp:  # nosec B310
                data = json.loads(resp.read().decode("utf-8"))
            return ("ok", data if isinstance(data, dict) else None)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return ("not_found", None)
            log.warning("update manifest fetch failed: HTTP %d", exc.code)
            return ("error", None)
        # HTTPException (BadStatusLine...) is not an OSError -- urllib doesn't wrap it.
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            http.client.HTTPException,
        ) as exc:
            log.warning("update manifest fetch failed: %s", exc)
            return ("error", None)

    def _download(self, url: str, expected_sha256: str, expected_size: int) -> Optional[Path]:
        """Stream *url* into ``update/pkg.zip.part``, verify size+sha256, then rename.

        Aborts hard the instant more than *expected_size* bytes arrive -- a
        misbehaving or compromised server cannot make the agent buffer
        unbounded data on disk.
        """
        self._update_dir.mkdir(parents=True, exist_ok=True)
        part_path = self._update_dir / "pkg.zip.part"
        final_path = self._update_dir / "pkg.zip"
        headers = {"X-SRP-Token": self._cfg.ingest_token} if self._cfg.ingest_token else {}
        req = urllib.request.Request(url, headers=headers)
        digest = hashlib.sha256()
        written = 0
        try:
            # Parenthesized multi-context `with` is Python 3.10+ syntax; this
            # codebase holds a Python 3.9 floor, so the two managers stay nested.
            # B310: scheme is the operator-configured server_url, not user input.
            with urllib.request.urlopen(req, timeout=self._cfg.http_timeout_sec) as resp:  # nosec B310  # noqa: SIM117
                with part_path.open("wb") as fh:
                    while True:
                        chunk = resp.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > expected_size:
                            raise ValueError("package exceeds the size promised by the manifest")
                        digest.update(chunk)
                        fh.write(chunk)
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            TimeoutError,
            http.client.HTTPException,  # IncompleteRead on a cut body
        ) as exc:
            log.warning("update package download failed: %s", exc)
            with contextlib.suppress(OSError):
                part_path.unlink()
            return None

        if written != expected_size or digest.hexdigest() != expected_sha256:
            log.warning("update package failed verification (size or sha256 mismatch)")
            with contextlib.suppress(OSError):
                part_path.unlink()
            return None
        os.replace(part_path, final_path)
        return final_path

    # -- status bookkeeping -----------------------------------------------------#
    def _gate(self, payload: dict) -> Optional[dict]:
        """Suppress a repeat of unchanged good news -- within the same day.

        A "failed" state always gets through, and the day bucket re-sends an
        unchanged "ok" once a day, so ``update_checked_at`` on the dashboard
        keeps advancing -- proof the agent is still alive and checking, not
        the agent's start time weeks ago.
        """
        key = (payload["state"], payload.get("available_version"), int(time.time() // 86400))
        if payload["state"] != "failed" and key == self._last_status:
            return None
        self._last_status = key
        return payload

    def _failed(self, target: Optional[str], error: str) -> dict:
        return {
            "checked_at": _now_iso(),
            "state": "failed",
            "error": error[:500],
            "available_version": target,
        }

    def _attempts_for(self, target: str) -> int:
        state = self._read_state()
        if state is None or state.get("target_version") != target:
            return 0
        try:
            attempts = int(state.get("attempts", 0))
            staged_at = float(state.get("staged_at", 0))
        except (TypeError, ValueError):
            return 0
        if attempts >= _MAX_ATTEMPTS and time.time() - staged_at > _ATTEMPT_RETRY_SEC:
            return _MAX_ATTEMPTS - 1  # one more try; _apply re-stamps staged_at
        return attempts

    # -- state.json -------------------------------------------------------------#
    def _read_state(self) -> Optional[dict]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):  # ValueError: also a torn UTF-8 byte
            return None

    def _write_state(self, data: dict) -> None:
        self._update_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._state_path.write_text(json.dumps(data), encoding="utf-8")

    def _clear_state(self) -> None:
        for path in (self._state_path, self._update_dir / "pkg.zip"):
            with contextlib.suppress(OSError):
                path.unlink()
