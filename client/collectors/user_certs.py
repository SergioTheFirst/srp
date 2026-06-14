r"""Read + STRICTLY validate the tray's personal-cert spool (tray spec §8).

The tray runs in each user's session and can see ``Cert:\CurrentUser\My``; the
SYSTEM agent cannot. Each user's tray drops a small spool file into
``C:\SRP\spool\``; the agent (SYSTEM) reads them here and folds the metadata into
its historical payload so the dashboard can show whose personal signature expires.

The spool is written by a NON-admin user, so everything here treats it as hostile
input: bounded file count + size, ``usercerts-*.json`` in exactly this dir (no
recursion), per-field type/length caps, epoch->ISO conversion, dedup, a hard total
cap, and a freshness window so a departed user's spool ages out. Never any
private-key material -- metadata only. A bad file is skipped, never raised.

Pure stdlib (agent zero-deps invariant): the caps mirror ``shared.schema``'s
``USER_CERTS_MAX`` and stay <= the contract cap so a compliant agent is never
rejected at the server boundary.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_MAX_TOTAL = 64  # mirrors shared.schema.USER_CERTS_MAX (agent cap <= contract cap)
_MAX_FILES = 50
_MAX_FILE_BYTES = 64 * 1024
_MAX_PER_FILE = 64
_MAX_STR = 256
_MAX_THUMB = 80
_MAX_OWNER = 64
_FRESH_SEC = 30 * 86_400  # ignore spool files older than this (logged-off/departed users)

SPOOL_GLOB = "usercerts-*.json"


def _iso(epoch: Any) -> Optional[str]:
    """Epoch seconds -> UTC ISO-8601 (matches the machine-cert format); None if bad."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _clip(value: Any, limit: int) -> str:
    return str(value)[:limit] if value is not None else ""


def _valid_cert(entry: Any, owner: str) -> Optional[dict[str, Optional[str]]]:
    if not isinstance(entry, dict):
        return None
    thumb = entry.get("thumbprint")
    if not isinstance(thumb, str) or not thumb.strip():
        return None
    not_after = _iso(entry.get("not_after"))
    if not_after is None:
        return None  # an undatable cert is useless for expiry -> drop
    raw_nb = entry.get("not_before")
    return {
        "subject": _clip(entry.get("subject"), _MAX_STR),
        "issuer": _clip(entry.get("issuer"), _MAX_STR),
        "thumbprint": _clip(thumb, _MAX_THUMB),
        "not_after": not_after,
        "not_before": _iso(raw_nb) if raw_nb is not None else None,
        "owner": owner,
    }


def _read_file(path: Path, *, now: float) -> list[dict[str, Optional[str]]]:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return []
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    written = doc.get("written_at")
    if isinstance(written, (int, float)) and now - float(written) > _FRESH_SEC:
        # stale: a logged-off user's spool ages out instead of lingering. written_at
        # is forgeable (user-written), but the impact is benign by design -- this is
        # reminder data, consumed by no trust/scoring path, so don't "harden" it.
        return []
    owner = _clip(doc.get("owner"), _MAX_OWNER)
    certs = doc.get("certs")
    if not isinstance(certs, list):
        return []
    out: list[dict[str, Optional[str]]] = []
    for entry in certs[:_MAX_PER_FILE]:
        cert = _valid_cert(entry, owner)
        if cert is not None:
            out.append(cert)
    return out


def read_user_certs(
    spool_dir: Path, *, now: Optional[float] = None
) -> list[dict[str, Optional[str]]]:
    """All valid personal certs across per-user spool files; ``[]`` if none/dir absent.

    Hostile-input safe (the spool is user-writable): bounded files/size/counts,
    strict per-field validation, dedup by (owner, thumbprint), hard total cap.
    """
    moment = time.time() if now is None else now
    try:
        files = sorted(p for p in Path(spool_dir).glob(SPOOL_GLOB) if p.is_file())
    except OSError:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Optional[str]]] = []
    for path in files[:_MAX_FILES]:
        for cert in _read_file(path, now=moment):
            key = (cert["owner"] or "", cert["thumbprint"] or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(cert)
            if len(out) >= _MAX_TOTAL:
                return out
    return out


def _spool_dir() -> Path:
    r"""``C:\SRP\spool`` next to the frozen agent exe; dev fallback won't exist (-> [])."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path("C:/SRP")
    return base / "spool"


def collect_user_certs() -> list[dict[str, Optional[str]]]:
    """Read the install-location spool (best effort; never raises)."""
    return read_user_certs(_spool_dir())
