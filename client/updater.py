"""Agent self-update: check the server's manifest, verify, stage, hand off.

Pure stdlib (urllib + zipfile + hmac) -- like the rest of client/, this module
carries zero third-party dependencies. Mechanism: fetch a small JSON manifest
from the server -> compare its version against ours -> download the zip ->
verify sha256 (+ HMAC on the manifest, when a token is configured) -> unpack
into ``update/staging`` -> register and fire a ONE-SHOT SYSTEM scheduled task
("SRP Agent Update") that runs ``staging\\setup.exe --update`` -> this process
exits so setup.exe can safely replace it.

A dedicated scheduled task is used instead of spawning setup.exe as a child
process: Task Scheduler kills every process in a task's job object when the
task's own process exits, so a child updater would die together with the agent
it is trying to replace. A second, independent task has its own job and
survives this process exiting.

Integrity + authenticity: the manifest's sha256 is mandatory; when the
deployment has a signing secret (``update_hmac_secret``, falling back to
``ingest_token`` -- see ``_signing_secret``, P0-4) the manifest must also carry
``hmac = HMAC-SHA256(secret, "<version>|<sha256>")`` so a man-in-the-middle
without the secret cannot forge a package -- an agent WITH a secret refuses a
manifest that lacks one (fail-closed). Only a strictly newer version is ever
applied, which closes off downgrade/replay of an old, once-valid manifest.

Security-review finding (2026-07-03): without a secret, sha256 alone is only an
integrity check, not an authenticity one -- a LAN MITM impersonating the server
could self-sign a malicious package. An agent with NO signing secret therefore
never downloads/applies at all (``check()`` reports availability only); auto-
*apply* is only reachable once both sides share a secret, so HMAC verification
is unconditional wherever code actually runs. P0-4 (stoperrors.md): the secret
defaults to ``ingest_token`` but SHOULD be ``update_hmac_secret`` -- reusing
ingest_token (which also rides a plaintext bearer header on every request) as
the signing key let a passive LAN eavesdropper forge a valid manifest.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
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

from client.config import ClientConfig
from client.winflags import NO_WINDOW

log = logging.getLogger(__name__)

TASK_NAME = "SRP Agent Update"
_MAX_PACKAGE_SIZE = 200 * 1024 * 1024  # 200 MiB cap, mirrors the server-side manifest guard
_MAX_ATTEMPTS = 3  # per target version -- stop re-downloading a package that never applies
_STALE_STAGE_SEC = 900  # 15 min grace period for the update task to finish the restart
_CHUNK_SIZE = 64 * 1024
_REQUIRED_MEMBERS = ("setup.exe", "payload/srp-agent.exe")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_version(value: Optional[str]) -> Optional[tuple[int, int, int]]:
    """Parse a strict MAJOR.MINOR.PATCH string into a tuple; None if malformed.

    Duplicated from shared.schema.parse_version, not imported -- client stays
    pure stdlib (same policy as transport.AGENT_VERSION vs CONTRACT_VERSION).
    """
    if not value or not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    return (nums[0], nums[1], nums[2])


def compute_hmac(token: str, version: str, sha256_hex: str) -> str:
    """HMAC-SHA256(token, "<version>|<sha256_hex>") hex digest."""
    msg = f"{version}|{sha256_hex}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _is_sha256_hex(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def select_update(manifest: dict, current_version: str, has_token: bool) -> Optional[dict]:
    """Validate *manifest* and return it iff it names a strictly newer version.

    Fail-closed: any malformed field, or a missing ``hmac`` while we hold a
    token, is treated as "nothing to do" rather than guessed at. An equal or
    older remote version is also None -- guards against downgrade/replay of an
    old, once-valid manifest.
    """
    if not isinstance(manifest, dict):
        return None
    remote = _parse_version(manifest.get("version"))
    current = _parse_version(current_version)
    if remote is None or current is None:
        return None
    if not _is_sha256_hex(manifest.get("sha256")):
        return None
    size = manifest.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not (0 < size <= _MAX_PACKAGE_SIZE):
        return None
    if has_token and not manifest.get("hmac"):
        return None  # a stripping MITM must not silently downgrade us to sha256-only
    if remote <= current:
        return None
    return manifest


def _check_member_name(name: str) -> None:
    """Zip-slip guard for one archive member name."""
    if not name or name.startswith("/") or name.startswith("\\") or ":" in name:
        raise ValueError(f"unsafe path in update package: {name!r}")
    if any(segment == ".." for segment in name.replace("\\", "/").split("/")):
        raise ValueError(f"unsafe path in update package: {name!r}")


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract *zip_path* into *dest*, which is wiped first.

    Every member name is validated BEFORE any file touches disk: absolute
    paths, drive letters, leading slashes/backslashes and ".." segments are
    all rejected (split on both "/" and "\\" -- the zip spec stores forward
    slashes, but a hostile archive can still smuggle a backslash for Windows).
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for name in names:
            _check_member_name(name)
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
    return ["schtasks", "/run", "/tn", TASK_NAME]


def _run_schtasks(argv: list[str]) -> int:
    # Static argv (schtasks + fixed literals / a validated staging path), shell=False.
    proc = subprocess.run(argv, capture_output=True, creationflags=NO_WINDOW)  # nosec B603
    return proc.returncode


class Updater:
    """Per-agent update checker/applier bound to one :class:`ClientConfig`."""

    def __init__(self, cfg: ClientConfig) -> None:
        self._cfg = cfg
        base = cfg.resolved_buffer_path().parent
        self._update_dir = base / "update"
        self._staging_dir = self._update_dir / "staging"
        self._state_path = self._update_dir / "state.json"
        # Last status sent this run -- lets check() skip repeating unchanged
        # good news every cycle; a "failed" always gets through (see _gate).
        self._last_status: Optional[tuple[str, Optional[str]]] = None

    def _signing_secret(self) -> str:
        """P0-4: dedicated update-HMAC key, falling back to ingest_token (which
        also rides a plaintext bearer header on every request -- less safe, kept
        for deployments that haven't set update_hmac_secret yet)."""
        return self._cfg.update_hmac_secret or self._cfg.ingest_token

    # -- public API ---------------------------------------------------------- #
    def reconcile_after_restart(self, current_version: str) -> Optional[dict]:
        """Called once at agent startup to close the loop on a staged update.

        No state.json -> nothing was ever staged, nothing to report. Target
        reached -> success, clean up. Target still not reached after a
        generous grace period -> the update task likely never ran or
        setup.exe failed; report it but LEAVE state.json in place so the
        attempts ceiling keeps working. Staged too recently -> the task may
        still be running -- say nothing yet.
        """
        state = self._read_state()
        if state is None:
            return None
        target = state.get("target_version")
        if target == current_version:
            self._clear_state()
            return {"checked_at": _now_iso(), "state": "ok"}
        staged_at = state.get("staged_at")
        if not isinstance(staged_at, (int, float)) or (time.time() - staged_at) <= _STALE_STAGE_SEC:
            return None
        return self._failed(target, "обновление не применилось после перезапуска")

    def check(self, current_version: str) -> tuple[Optional[dict], bool]:
        """Fetch the manifest, decide, and (if frozen AND a token is set) apply it.

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

        update = select_update(manifest or {}, current_version, bool(self._signing_secret()))
        if update is None:
            return (self._gate({"checked_at": _now_iso(), "state": "ok"}), False)

        remote = str(update["version"])
        if not getattr(sys, "frozen", False):
            log.info("update %s available but not applying -- not a frozen build", remote)
            payload = {"checked_at": _now_iso(), "state": "ok", "available_version": remote}
            return (self._gate(payload), False)

        if not self._signing_secret():
            # No shared secret -> the manifest/package carry sha256 only, no HMAC.
            # A LAN MITM impersonating the server could self-consistently sign a
            # malicious zip and reach setup.exe --update as SYSTEM. Refuse to
            # download/apply without a token; still report availability so the
            # dashboard shows the pending update (security-review finding 1).
            log.warning(
                "update %s available but not applied -- no signing secret configured", remote
            )
            payload = {"checked_at": _now_iso(), "state": "ok", "available_version": remote}
            return (self._gate(payload), False)

        attempts = self._attempts_for(remote)
        if attempts >= _MAX_ATTEMPTS:
            error = f"превышен лимит попыток обновления до {remote}"
            return (self._gate(self._failed(remote, error)), False)

        return self._apply(update, remote, attempts)

    # -- apply ----------------------------------------------------------------#
    def _apply(self, manifest: dict, remote: str, attempts: int) -> tuple[Optional[dict], bool]:
        """Download, verify, stage and hand off *manifest* (known to be newer).

        state.json is written for this attempt BEFORE anything else so a
        failure at ANY step -- including a network failure during download --
        still counts against the 3-attempt ceiling; otherwise a permanently
        broken package would be re-downloaded forever, every check cycle.
        """
        self._write_state(
            {"target_version": remote, "attempts": attempts + 1, "staged_at": time.time()}
        )
        token = self._signing_secret()
        if token:
            expected = compute_hmac(token, remote, manifest["sha256"])
            if not hmac.compare_digest(expected, str(manifest.get("hmac", ""))):
                return (self._gate(self._failed(remote, "подпись пакета не сошлась")), False)

        pkg_url = self._cfg.server_url.rstrip("/") + "/api/v1/agent/update/package"
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
        url = self._cfg.server_url.rstrip("/") + "/api/v1/agent/update"
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
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
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
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
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
        """Suppress a repeat of unchanged good news.

        A "failed" state always gets through so ``update_checked_at`` on the
        dashboard keeps advancing -- proof the agent is still alive and
        retrying, not a stale timestamp from hours ago.
        """
        key = (payload["state"], payload.get("available_version"))
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
            return int(state.get("attempts", 0))
        except (TypeError, ValueError):
            return 0

    # -- state.json -------------------------------------------------------------#
    def _read_state(self) -> Optional[dict]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def _write_state(self, data: dict) -> None:
        self._update_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._state_path.write_text(json.dumps(data), encoding="utf-8")

    def _clear_state(self) -> None:
        for path in (self._state_path, self._update_dir / "pkg.zip"):
            with contextlib.suppress(OSError):
                path.unlink()
