"""CLAUDE.md §5: a module stays under 800 lines. The ones already over it are split only
in their own refactor branch -- this set may only shrink (storage.py reached 805 in stage 3
and was split; trends.py and health.py too)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_OVERSIZED = {
    "client/deploy/setup.py",
    "server/api.py",
    "server/db.py",
    "server/pipeline.py",
    "server/web/dashboard.py",
}


def _lines(path: Path) -> int:
    with path.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def test_no_module_crosses_the_800_line_cap() -> None:
    over = {
        path.relative_to(_ROOT).as_posix()
        for pkg in ("server", "shared", "client")
        for path in (_ROOT / pkg).rglob("*.py")
        if _lines(path) >= 800
    }
    assert over - _OVERSIZED == set()
