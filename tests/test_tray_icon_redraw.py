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
