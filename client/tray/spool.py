r"""Write the personal-cert spool the SYSTEM agent reads (tray spec §8).

The tray (user session) can see ``Cert:\CurrentUser\My``; the SYSTEM agent cannot.
We drop the metadata (NEVER key material) into a per-user file under the install
spool dir (``C:\SRP\spool``) so the agent can fold it into telemetry. One file per
user so multi-user PCs don't clobber; atomic write; failures are swallowed (a
spool hiccup must never break the tray). Pure stdlib.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from client.tray.certs import CertInfo

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_OWNER = 64


def _username() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "user"


def _safe_name(user: str) -> str:
    """Filesystem-safe per-user filename component (no traversal, bounded)."""
    safe = _SAFE.sub("_", user)[:_MAX_OWNER]
    return safe or "user"


def build_spool(owner: str, certs: list[CertInfo], now: float) -> dict[str, Any]:
    """The spool document: owner + epoch + metadata-only certs (no key material)."""
    return {
        "owner": owner[:_MAX_OWNER],
        "written_at": int(now),
        "certs": [
            {
                "subject": c.subject,
                "issuer": c.issuer,
                "thumbprint": c.thumbprint,
                "not_before": c.not_before,
                "not_after": c.not_after,
            }
            for c in certs
        ],
    }


def spool_path(spool_dir: Path, owner: str) -> Path:
    return Path(spool_dir) / f"usercerts-{_safe_name(owner)}.json"


def write_spool(
    spool_dir: Path, owner: str, certs: list[CertInfo], *, now: Optional[float] = None
) -> bool:
    """Atomically write this user's spool file; True on success, False on any failure."""
    moment = time.time() if now is None else now
    path = spool_path(spool_dir, owner)
    tmp = path.with_name(path.name + ".tmp")  # not *.json -> the agent glob skips it
    try:
        Path(spool_dir).mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(build_spool(owner, certs, moment), ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False


def _install_spool_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path("C:/SRP")
    return base / "spool"


def publish_user_certs(certs: Optional[list[CertInfo]]) -> None:
    """Tray hook: spool the current user's signing certs (None = PS failed -> skip)."""
    if certs is None:
        return
    write_spool(_install_spool_dir(), _username(), certs)
