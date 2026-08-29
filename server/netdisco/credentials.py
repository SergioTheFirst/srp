"""Phase 13: secure store for non-public SNMP credentials (SAFETY).

A non-public SNMP community is a secret. It must live on disk only encrypted
(Windows DPAPI, machine-scope) -- never plaintext -- and must never reach logs,
the API, or the dashboard. ``public`` is read-only, not a secret, so it stays
valid as plaintext config without any store.

Pure stdlib: DPAPI is reached through a thin ``ctypes`` wrapper over
``CryptProtectData``/``CryptUnprotectData`` (no third-party packages, matching
the agent/server no-heavy-deps stance). Off Windows there is no DPAPI, so the
only community we will hand out is ``public``.

SNMPv3 (user/authKey/privKey) is scaffolded in ``CredentialRef`` but not yet
implemented; v1 covers SNMP v2c community only. We never read a device's own
private keys (project invariant) -- only our own SNMP credentials are stored.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

_log = logging.getLogger("srp.netdisco")

# Serializes the read-modify-write in set_community/set_secret (stoperrors
# P2-5): two concurrent calls could otherwise both read the same on-disk
# snapshot, and whichever wrote back last silently dropped the other's change.
# Module-level (not per-instance): default_store() builds a fresh
# CredentialStore per call (scan.py/scheduler.py/reconcile.py/adapters/*), so an
# instance lock would not serialize the concurrent callers that actually share
# the same underlying file -- mirrors the module-level _dedup_lock/_rate_lock
# pattern in server/ingest_guards.py.
_write_lock = threading.Lock()

# DPAPI flag: protect under the machine key so the SYSTEM-run service (not a
# specific interactive user) can decrypt.
_CRYPTPROTECT_LOCAL_MACHINE = 0x4
# Sanity cap: the store only ever holds short blobs; refuse to parse a bloated
# (corrupt/hostile) file rather than read it whole into memory.
_MAX_STORE_BYTES = 1_048_576

ProtectFn = Callable[[bytes], bytes]
UnprotectFn = Callable[[bytes], bytes]


@dataclass(frozen=True)
class CredentialRef:
    """Pointer to a stored SNMP credential, with a forward seat for SNMPv3.

    v1 resolves ``name`` -> a v2c community. The SNMPv3 fields are scaffolding
    only (not implemented); they keep the contract additive when v3 lands.
    """

    name: str
    version: str = "v2c"
    user: Optional[str] = None
    auth_key: Optional[str] = None
    priv_key: Optional[str] = None

    def __repr__(self) -> str:
        # Never let SNMPv3 key material reach a repr/log line.
        return f"CredentialRef(name={self.name!r}, version={self.version!r})"

    __str__ = __repr__


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_available() -> bool:
    """True only on Windows with a loadable crypt32 -- DPAPI is reachable."""
    if sys.platform != "win32":
        return False
    try:
        ctypes.windll.crypt32  # noqa: B018  # touch to confirm it loads
        return True
    except (AttributeError, OSError):  # pragma: no cover - Windows-only path
        return False


def _blob_to_bytes(blob: _DataBlob) -> bytes:  # pragma: no cover - Windows-only path
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(data: bytes) -> bytes:  # pragma: no cover - Windows-only path
    """DPAPI-encrypt ``data`` under the machine key. Raises if unavailable."""
    if not dpapi_available():
        raise RuntimeError("DPAPI unavailable: cannot encrypt a non-public secret")
    buf = (ctypes.c_char * len(data)).from_buffer_copy(data)  # explicit owned buffer
    src = _DataBlob(len(buf), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(src), None, None, None, None, _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out)
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return _blob_to_bytes(out)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(blob: bytes) -> bytes:  # pragma: no cover - Windows-only path
    """DPAPI-decrypt ``blob``. Raises if unavailable or the blob is foreign."""
    if not dpapi_available():
        raise RuntimeError("DPAPI unavailable: cannot decrypt the secret store")
    buf = (ctypes.c_char * len(blob)).from_buffer_copy(blob)  # explicit owned buffer
    src = _DataBlob(len(buf), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out)
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return _blob_to_bytes(out)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


class CredentialStore:
    """JSON file of DPAPI-encrypted SNMP communities (base64-wrapped blobs).

    The file holds only ciphertext; a clear community is never written. ``set``
    requires an available cipher (we refuse to persist an unencrypted secret).
    ``get`` fails closed: any missing key, foreign blob, or absent cipher yields
    ``None`` so the caller falls back to the safe ``public`` default.
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        protect: ProtectFn = protect,
        unprotect: UnprotectFn = unprotect,
        available: Optional[bool] = None,
    ) -> None:
        self._path = Path(path)
        self._protect = protect
        self._unprotect = unprotect
        self._available = dpapi_available() if available is None else available

    @property
    def available(self) -> bool:
        return self._available

    def _load(self) -> dict[str, Any]:
        try:
            if self._path.stat().st_size > _MAX_STORE_BYTES:
                return {}  # corrupt/hostile: never read a bloated store whole
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_community(self, name: str, community: str) -> None:
        if not self._available:
            raise RuntimeError("DPAPI unavailable: refusing to store a community unencrypted")
        blob = self._protect(community.encode("utf-8"))
        with _write_lock:
            data = self._load()
            communities = data.get("communities")
            if not isinstance(communities, dict):
                communities = {}
            communities[name] = base64.b64encode(blob).decode("ascii")
            data["communities"] = communities
            # Atomic write: a power-cut mid-write must never leave a torn store
            # that silently parses empty and drops the secret. Same-volume
            # replace is atomic on Windows NTFS. The lock (not just the atomic
            # replace) is what stops two concurrent callers from each reading
            # the same pre-write snapshot and one clobbering the other's change.
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)

    def get_community(self, name: str) -> Optional[str]:
        if not self._available:
            return None
        communities = self._load().get("communities")
        if not isinstance(communities, dict) or name not in communities:
            return None
        try:
            blob = base64.b64decode(communities[name], validate=True)
            return self._unprotect(blob).decode("utf-8")
        except (ValueError, OSError, RuntimeError):
            return None

    def set_secret(self, name: str, secret: str) -> None:
        """Store an adapter secret (password / token / JSON blob) DPAPI-encrypted.

        A separate ``secrets`` namespace from SNMP ``communities`` (an adapter
        credential is not a community). Same invariant: refuse to persist a secret
        unencrypted, atomic same-volume replace so a torn write never drops it."""
        if not self._available:
            raise RuntimeError("DPAPI unavailable: refusing to store a secret unencrypted")
        blob = self._protect(secret.encode("utf-8"))
        with _write_lock:
            data = self._load()
            secrets = data.get("secrets")
            if not isinstance(secrets, dict):
                secrets = {}
            secrets[name] = base64.b64encode(blob).decode("ascii")
            data["secrets"] = secrets
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)

    def get_secret(self, name: str) -> Optional[str]:
        """The decrypted adapter secret, or ``None`` (fail-closed): a missing key,
        foreign blob, or absent cipher all yield ``None`` so a caller never acts on
        a half-resolved credential."""
        if not self._available:
            return None
        secrets = self._load().get("secrets")
        if not isinstance(secrets, dict) or name not in secrets:
            return None
        try:
            blob = base64.b64decode(secrets[name], validate=True)
            return self._unprotect(blob).decode("utf-8")
        except (ValueError, OSError, RuntimeError):
            return None


def default_store() -> Optional[CredentialStore]:
    """Store next to the server package; ``None`` off Windows (no DPAPI).

    Confidentiality of the on-disk store rests on the file's read ACL as well as
    DPAPI: the ciphertext is machine-scoped, so any local principal that can read
    the file can also decrypt it. In production the installer hardens the install
    prefix (``icacls /inheritance:r`` on ``C:\\SRP`` -- see installer ACL note),
    which covers this path. Outside that prefix (e.g. a dev checkout) the file is
    only as protected as the surrounding directory.
    """
    if not dpapi_available():
        return None
    path = Path(__file__).resolve().parents[1] / "netdisco_secrets.json"
    return CredentialStore(path)


def resolve_community(cfg: Any, store: Optional[CredentialStore] = None) -> str:
    """Community for SNMP probes; never raises, never leaks a secret on failure.

    No ``snmp_credential_ref`` -> the (plaintext) ``snmp_community``, default
    ``public``. With a ref, resolve from the store; any failure (no store, no
    DPAPI, missing/foreign blob) falls back to ``public`` -- read-only and safe.
    """
    ref = getattr(cfg, "snmp_credential_ref", "")
    if not ref:
        if cfg.snmp_community != "public":
            # Warn (never log the value): a real secret should go through the
            # DPAPI store via snmp_credential_ref, not sit plaintext in config.
            _log.warning(
                "netdisco snmp_community is non-public but no snmp_credential_ref "
                "is set -- a secret community should use the encrypted store"
            )
        return cfg.snmp_community
    if store is None:
        store = default_store()
    secret = store.get_community(ref) if store is not None else None
    return secret if secret else "public"
