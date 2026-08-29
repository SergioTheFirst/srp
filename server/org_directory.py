"""PUBLIC STUB replacement for ``server/org_directory.py``.

The private/commercial module reads an operator-maintained
``org_directory.json`` (organisation/department code -> display name) and
supports multi-organisation deployments (Full edition, see the public
README's «Полная версия»). The public/Free edition ships no multi-org
directory at all, so this stub keeps the exact public surface the rest of
the server imports -- ``Label``, ``OrgDirectory``, ``init_directory``,
``get_directory`` -- but is permanently empty and NEVER reads a file: every
code passed in comes back as an "unknown" label (the raw code + a
not-in-directory chip), exactly the same fallback the real module already
uses when no ``org_directory.json`` is present. No caller needs to change.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, NamedTuple, Optional

_NO_DEPT = "Без отдела"


class Label(NamedTuple):
    """A rendered code: the text to show + whether the code was found."""

    text: str
    known: bool


class OrgDirectory:
    """PUBLIC STUB: an always-empty directory. Never reads ``path``."""

    def __init__(self, path: Optional[Path]) -> None:
        self._path: Optional[Path] = Path(path) if path is not None else None

    def reload_if_changed(self) -> None:
        """No-op -- the stub has no file to reload."""
        return None

    def org_name(self, code: Optional[str], *, check_reload: bool = True) -> Optional[str]:
        return None

    def dept_name(
        self, org_code: Optional[str], dept_code: Optional[str], *, check_reload: bool = True
    ) -> Optional[str]:
        return None

    def as_picker(self) -> list[dict[str, Any]]:
        return []

    def org_display(self, code: Optional[str], *, check_reload: bool = True) -> Label:
        coerced = str(code).strip() if code else ""
        if not coerced:
            return Label("", True)  # nothing assigned -> nothing to flag
        return Label(coerced, False)  # unknown code -> raw code + "not in directory" chip

    def dept_display(
        self,
        org_code: Optional[str],
        dept_code: Optional[str],
        legacy_department: Optional[str] = None,
        *,
        check_reload: bool = True,
    ) -> Label:
        dcode = str(dept_code).strip() if dept_code else ""
        if dcode:
            return Label(dcode, False)  # unknown code -> raw code + chip
        legacy = (legacy_department or "").strip()
        if legacy:
            return Label(legacy, True)
        return Label(_NO_DEPT, True)


# --------------------------------------------------------------------------- #
# Module singleton -- same shape as the real module's (server/main.py,
# server/api.py, server/web/dashboard.py all import these two names).
# --------------------------------------------------------------------------- #

_DIRECTORY: Optional[OrgDirectory] = None
_DIR_LOCK = threading.Lock()


def init_directory(path: Optional[Path]) -> OrgDirectory:
    """(Re)initialize the process-wide directory. ``path`` is accepted for
    signature compatibility but is never opened -- see module docstring."""
    global _DIRECTORY
    with _DIR_LOCK:
        _DIRECTORY = OrgDirectory(path)
    return _DIRECTORY


def get_directory() -> OrgDirectory:
    """The process-wide directory; an empty one until ``init_directory`` runs."""
    global _DIRECTORY
    if _DIRECTORY is None:
        with _DIR_LOCK:
            if _DIRECTORY is None:
                _DIRECTORY = OrgDirectory(None)
    return _DIRECTORY
