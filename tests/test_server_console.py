"""QE-1 (2026-10-15): щелчок в окне start_server.bat (conhost QuickEdit) блокирует
запись в консоль, а лог доступа uvicorn пишется из event loop -- замирал ВЕСЬ
сервер (дашборд, ingest, скачивание пакета обновления) до нажатия клавиши."""

from __future__ import annotations

import ctypes
import sys

import pytest
from server import main as main_mod

pytestmark = pytest.mark.unit


class _Fn:
    """Stands in for a ctypes function pointer: callable, accepts argtypes/restype."""

    def __init__(self, impl) -> None:
        self._impl = impl

    def __call__(self, *args):
        return self._impl(*args)


def _fake_kernel32(console_mode: int, calls: dict):
    def _get_mode(_handle, ptr) -> int:
        if not console_mode:
            return 0  # stdin is not a console (service, redirected, pytest)
        ptr._obj.value = console_mode
        return 1

    k32 = type("K32", (), {})()
    k32.GetStdHandle = _Fn(lambda _which: 42)
    k32.GetConsoleMode = _Fn(_get_mode)
    k32.SetConsoleMode = _Fn(lambda _handle, mode: calls.setdefault("mode", mode) or 1)
    return k32


def _patch(monkeypatch: pytest.MonkeyPatch, console_mode: int, calls: dict) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda _name: _fake_kernel32(console_mode, calls), raising=False
    )


def test_quickedit_is_switched_off_other_input_flags_kept(monkeypatch) -> None:
    calls: dict = {}
    _patch(monkeypatch, 0x01F7, calls)  # a fresh conhost window: QuickEdit on

    main_mod._disable_quickedit()

    assert calls["mode"] == 0x01B7  # -QUICK_EDIT (0x40), +EXTENDED_FLAGS (0x80) kept


def test_no_console_means_no_change(monkeypatch) -> None:
    calls: dict = {}
    _patch(monkeypatch, 0, calls)

    main_mod._disable_quickedit()

    assert "mode" not in calls
