"""Server-side org/department directory (tray spec §7).

Codes travel in telemetry; full names live ONLY here and are decoded at render
time -- never written to the DB, so a rename reflects across all history at
once. Reloaded on mtime change; a broken file keeps the last good copy; a
missing file is an empty directory. This is a settings file, not an admin UI:
same trust model as the ingest token, no migrations, backup = copy the file.
An unknown code is surfaced (code + chip), never used to reject telemetry.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, NamedTuple, Optional

logger = logging.getLogger("srp.org_directory")

_NO_DEPT = "Без отдела"


class Label(NamedTuple):
    """A rendered code: the text to show + whether the code was found.

    ``known=False`` means "show the raw code and a 'not in directory' chip" --
    it catches a typo'd code in a deploy BAT without dropping the telemetry.
    """

    text: str
    known: bool


class _Org(NamedTuple):
    name: Optional[str]
    departments: dict[str, str]  # dept_code -> dept_name


def _coerce_code(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _parse(data: Any) -> dict[str, _Org]:
    """Pure: build a code->_Org map from already-parsed JSON.

    Tolerant of malformed entries -- a bad row is skipped, never raised, so one
    typo cannot blind the whole directory.
    """
    orgs: dict[str, _Org] = {}
    if not isinstance(data, dict):
        return orgs
    for raw in data.get("organizations") or []:
        if not isinstance(raw, dict):
            continue
        code = _coerce_code(raw.get("code"))
        if not code:
            continue
        depts: dict[str, str] = {}
        for dep in raw.get("departments") or []:
            if not isinstance(dep, dict):
                continue
            dcode = _coerce_code(dep.get("code"))
            dname = dep.get("name")
            if dcode and isinstance(dname, str) and dname:
                depts[dcode] = dname
        name = raw.get("name")
        orgs[code] = _Org(name=name if isinstance(name, str) and name else None, departments=depts)
    return orgs


class OrgDirectory:
    """Code->name lookups backed by a JSON file, refreshed on mtime change."""

    def __init__(self, path: Optional[Path]) -> None:
        self._path: Optional[Path] = Path(path) if path is not None else None
        self._mtime: Optional[float] = None
        self._orgs: dict[str, _Org] = {}
        self._lock = threading.Lock()
        self.reload_if_changed()

    def reload_if_changed(self) -> None:
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return  # missing/unreadable -> keep what we have (empty on first load)
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            parsed = _parse(data)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # Never crash on a bad edit: keep the last good copy, log once, and
            # record the mtime so we don't re-read the same broken file each call.
            logger.warning("org_directory: keeping last good copy (%s): %s", self._path, exc)
            self._mtime = mtime
            return
        with self._lock:
            self._orgs = parsed
            self._mtime = mtime

    def org_name(self, code: Optional[str]) -> Optional[str]:
        self.reload_if_changed()
        org = self._orgs.get(_coerce_code(code))
        return org.name if org else None

    def dept_name(self, org_code: Optional[str], dept_code: Optional[str]) -> Optional[str]:
        self.reload_if_changed()
        org = self._orgs.get(_coerce_code(org_code))
        if org is None:
            return None
        return org.departments.get(_coerce_code(dept_code))

    def as_picker(self) -> list[dict[str, Any]]:
        """JSON-ready org+dept list for the /deploy command generator (tray §7).

        Codes only travel in telemetry; this exposes the code->name map so the
        deploy page can offer a typo-proof picker. Sorted by code for a stable
        render; never includes secrets (the directory has none).
        """
        self.reload_if_changed()
        with self._lock:
            items = sorted(self._orgs.items())
            return [
                {
                    "code": code,
                    "name": org.name or "",
                    "departments": [
                        {"code": dcode, "name": dname}
                        for dcode, dname in sorted(org.departments.items())
                    ],
                }
                for code, org in items
            ]

    def org_display(self, code: Optional[str]) -> Label:
        coerced = _coerce_code(code)
        if not coerced:
            return Label("", True)  # nothing assigned -> nothing to flag
        name = self.org_name(coerced)
        return Label(name, True) if name else Label(coerced, False)

    def dept_display(
        self,
        org_code: Optional[str],
        dept_code: Optional[str],
        legacy_department: Optional[str] = None,
    ) -> Label:
        """Decode a department for display (tray spec §7 COALESCE policy).

        Known code -> name. Unknown code -> the code + chip (a typo must not be
        masked by stale free text). No code -> the legacy free-text
        ``devices.department`` (deprecated), else "Без отдела".
        """
        dcode = _coerce_code(dept_code)
        if dcode:
            name = self.dept_name(org_code, dcode)
            return Label(name, True) if name else Label(dcode, False)
        legacy = (legacy_department or "").strip()
        if legacy:
            return Label(legacy, True)
        return Label(_NO_DEPT, True)


# --------------------------------------------------------------------------- #
# Module singleton (wired from server config at startup)
# --------------------------------------------------------------------------- #

_DIRECTORY: Optional[OrgDirectory] = None
_DIR_LOCK = threading.Lock()


def init_directory(path: Optional[Path]) -> OrgDirectory:
    """(Re)initialize the process-wide directory from a path."""
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
