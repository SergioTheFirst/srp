"""Пин З.7: агент полностью невидим -- каждый subprocess-вызов в client/
обязан передавать creationflags (CREATE_NO_WINDOW на Windows, 0 иначе), иначе
любой запуск может мигнуть консольным окном на рабочем столе пользователя.
"""

from __future__ import annotations

import ast
import pathlib

CLIENT_DIR = pathlib.Path(__file__).resolve().parents[1] / "client"
_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_output", "check_call"}


def _creationflags_value_ok(value: ast.expr) -> bool:
    """Reject a literal 0 (defeats the whole point) and bare-name references
    that don't obviously carry NO_WINDOW; accept everything else (bitwise-or
    combinations, attribute lookups on the shared constant, etc.) -- this is a
    regression pin, not a full evaluator, so it must not overfit."""
    if isinstance(value, ast.Constant) and value.value == 0:
        return False
    if isinstance(value, ast.Name):
        return "NO_WINDOW" in value.id
    if isinstance(value, ast.Attribute):
        return "NO_WINDOW" in value.attr
    return True


def _find_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted(CLIENT_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in _SUBPROCESS_CALLS
            ):
                continue
            flag_kw = next((kw for kw in node.keywords if kw.arg == "creationflags"), None)
            if flag_kw is None or not _creationflags_value_ok(flag_kw.value):
                offenders.append(f"{path.relative_to(CLIENT_DIR)}:{node.lineno}")
    return offenders


def test_every_subprocess_call_passes_creationflags():
    offenders = _find_offenders()
    assert offenders == [], f"subprocess вызовы без creationflags=NO_WINDOW: {offenders}"


def _parse_kw_value(expr: str) -> ast.expr:
    call = ast.parse(f"f({expr})", mode="eval").body
    assert isinstance(call, ast.Call)
    return call.keywords[0].value


def test_creationflags_value_check_rejects_bare_zero():
    """Regression pin (review finding): creationflags=0 would satisfy a naive
    'keyword is present' check while silently reintroducing the console flash."""
    assert _creationflags_value_ok(_parse_kw_value("creationflags=0")) is False


def test_creationflags_value_check_rejects_unrelated_name():
    assert _creationflags_value_ok(_parse_kw_value("creationflags=SOME_OTHER_FLAG")) is False


def test_creationflags_value_check_accepts_no_window_reference():
    assert _creationflags_value_ok(_parse_kw_value("creationflags=NO_WINDOW")) is True
    assert _creationflags_value_ok(_parse_kw_value("creationflags=_NO_WINDOW")) is True
    # CREATE_NO_WINDOW contains "NO_WINDOW" as a substring -> accepted too.
    assert (
        _creationflags_value_ok(_parse_kw_value("creationflags=subprocess.CREATE_NO_WINDOW"))
        is True
    )
    assert _creationflags_value_ok(_parse_kw_value("creationflags=win.NO_WINDOW")) is True


def test_pin_finds_real_subprocess_call_sites():
    """Sanity: the AST walk actually inspects the files we expect it to (a
    silently-empty walk would make the assertion above vacuously true)."""
    covered = {p.relative_to(CLIENT_DIR) for p in CLIENT_DIR.rglob("*.py")}
    expected = {
        pathlib.Path("tray") / "__main__.py",
        pathlib.Path("deploy") / "setup.py",
        pathlib.Path("collectors") / "ps.py",
        pathlib.Path("collectors") / "print_jobs.py",
    }
    assert expected <= covered
