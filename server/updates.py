"""Agent auto-update package source: ``server/updates/`` fail-closed reader.

The operator drops two files produced by ``build.bat`` into ``updates_dir``:
``srp-agent-update-<version>.zip`` and a ``manifest.json`` shaped like
``{"version": "...", "file": "...", "sha256": "...", "size": ...}``. The server
never trusts the manifest blindly -- it recomputes the zip's sha256 (cached on
the pair of file mtimes) and cross-checks every field against the file that is
actually on disk. Any mismatch means the package is NOT offered to the fleet
(logged as a WARNING): a broken or half-copied deployment must never reach
agents.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any, Optional

from shared.schema import parse_version

_log = logging.getLogger("srp.updates")

_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming reads -- zips run up to hundreds of MB
_HEX_CHARS = set("0123456789abcdef")

# str(updates_dir) -> (manifest_mtime, zip_mtime, validated info dict without "hmac").
# Only successful validations are cached; a failing manifest is re-checked (and
# re-logged) on every call, which is fine -- misconfiguration should stay loud.
_cache: dict[str, tuple[float, float, dict[str, Any]]] = {}


def reset_cache() -> None:
    """Drop the cached validation result. Tests only."""
    _cache.clear()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX_CHARS for c in value)


def _flat_name(file_name: Any) -> Optional[str]:
    """A plain file name with no path separators -- guards against zip-slip-style
    traversal via a manifest ``file`` field pointing outside ``updates_dir``."""
    if isinstance(file_name, str) and file_name and Path(file_name).name == file_name:
        return file_name
    return None


def _validate(updates_dir: Path, manifest: dict) -> Optional[dict[str, Any]]:
    version = manifest.get("version")
    if parse_version(version) is None:
        _log.warning("update manifest: bad version %r", version)
        return None
    file_name = _flat_name(manifest.get("file"))
    if file_name is None:
        _log.warning("update manifest: unsafe or missing file name %r", manifest.get("file"))
        return None
    zip_path = updates_dir / file_name
    if not zip_path.is_file():
        _log.warning("update manifest: package %s not found in %s", file_name, updates_dir)
        return None
    claimed_sha = manifest.get("sha256")
    if not _is_hex64(claimed_sha):
        _log.warning("update manifest: sha256 is not a 64-char lowercase hex string")
        return None
    claimed_size = manifest.get("size")
    actual_size = zip_path.stat().st_size
    if not isinstance(claimed_size, int) or claimed_size != actual_size:
        _log.warning(
            "update manifest: size mismatch (claimed=%r actual=%d)", claimed_size, actual_size
        )
        return None
    actual_sha = _sha256_file(zip_path)
    if actual_sha != claimed_sha:
        _log.warning("update manifest: sha256 mismatch for %s", file_name)
        return None
    return {
        "version": version,
        "file": file_name,
        "path": str(zip_path.resolve()),
        "sha256": actual_sha,
        "size": actual_size,
    }


def get_update_info(updates_dir: Path, token: str = "") -> Optional[dict]:  # nosec B107 -- empty = no token
    """Return the validated update package info, or None if none is offered.

    A missing/unparsable manifest is the normal "no package staged" state (logged
    at debug). With a non-empty ``token``, the result carries an ``hmac`` field --
    HMAC-SHA256(token, "<version>|<sha256>") -- proving authenticity against a LAN
    MITM (both sides already hold the shared ingest token; binding the version into
    the signed material cuts off a replay/downgrade swap).
    """
    manifest_path = updates_dir / "manifest.json"
    if not manifest_path.is_file():
        _log.debug("no update manifest at %s", manifest_path)
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.debug("update manifest at %s is unreadable or not valid JSON", manifest_path)
        return None
    if not isinstance(raw, dict):
        _log.debug("update manifest at %s is not a JSON object", manifest_path)
        return None

    safe_name = _flat_name(raw.get("file"))
    zip_path = updates_dir / safe_name if safe_name else None
    manifest_mtime = manifest_path.stat().st_mtime
    zip_mtime = zip_path.stat().st_mtime if zip_path is not None and zip_path.is_file() else -1.0

    cache_key = str(updates_dir)
    cached = _cache.get(cache_key)
    info: Optional[dict[str, Any]]
    if cached is not None and cached[0] == manifest_mtime and cached[1] == zip_mtime:
        info = dict(cached[2])
    else:
        info = _validate(updates_dir, raw)
        if info is None:
            return None
        _cache[cache_key] = (manifest_mtime, zip_mtime, dict(info))

    if token:
        material = f"{info['version']}|{info['sha256']}".encode()
        info["hmac"] = hmac.new(token.encode(), material, hashlib.sha256).hexdigest()
    return info
