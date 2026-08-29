"""З.7: трей не должен перерисовывать иконку без изменений (потенциальное
мигание). ``TrayIcon`` сама создаёт Win32-окно на конструкторе и не тестируется
напрямую (см. её докстринг); решение "перерисовывать или нет" вынесено в
чистую функцию ``_should_skip_redraw`` специально ради юнит-теста.
"""

from __future__ import annotations

from client.tray.icon import _should_skip_redraw


def test_skips_when_state_and_tooltip_unchanged():
    key = ("ok", "SRP · всё хорошо")
    assert _should_skip_redraw(True, key, key) is True


def test_redraws_when_state_changes():
    assert _should_skip_redraw(True, ("ok", "SRP"), ("warn", "SRP")) is False


def test_redraws_when_tooltip_changes():
    assert _should_skip_redraw(True, ("ok", "SRP · a"), ("ok", "SRP · b")) is False


def test_never_skips_first_add():
    """added=False (icon not yet on screen, e.g. after TaskbarCreated) must
    always redraw regardless of what a stale _last_shown still holds."""
    key = ("ok", "SRP")
    assert _should_skip_redraw(False, key, key) is False


def test_never_skips_when_nothing_shown_yet():
    assert _should_skip_redraw(True, None, ("ok", "SRP")) is False


def test_tray_icon_requires_on_owner(monkeypatch) -> None:
    """F11: конструктор кладёт on_owner в карту обработчиков под ID_OWNER.

    ``ctypes.WinDLL`` подменяется общим фейком (образец --
    ``tests/test_tray_exit.py::test_acquire_single_instance_survives_null_handle``):
    конструктору нужны только truthy-возвраты от Register*/CreateWindowExW, а не
    реальный Win32.
    """
    import ctypes

    from client.tray.icon import ID_OWNER, TrayIcon

    class _FakeDLL:
        def __getattr__(self, _name):
            return lambda *a, **k: 1

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _FakeDLL())

    owner_calls: list = []
    icon = TrayIcon(
        on_open=lambda: None,
        on_refresh=lambda: None,
        on_about=lambda: None,
        on_exit=lambda: None,
        on_owner=lambda: owner_calls.append(1),
    )
    assert ID_OWNER in icon._on
    icon._on[ID_OWNER]()
    assert owner_calls == [1]


def test_win32_dlls_are_loaded_with_use_last_error() -> None:
    """o5-C13: ctypes.windll.* не сохраняет errno -- ctypes.get_last_error() всегда 0,
    и диагностика падений трея («CreateWindowExW failed (0)») бесполезна."""
    from pathlib import Path

    from client.tray import icon as icon_mod

    for mod_name in ("icon", "__main__"):
        src = Path(icon_mod.__file__).with_name(f"{mod_name}.py").read_text(encoding="utf-8")
        loads = [ln for ln in src.splitlines() if "ctypes.windll." in ln]
        assert loads == [], f"{mod_name}.py: ctypes.windll без use_last_error: {loads}"
        assert "use_last_error=True" in src, f"{mod_name}.py: нет use_last_error=True"
