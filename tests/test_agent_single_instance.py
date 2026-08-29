"""Агент-демон должен быть единственным на машине.

Живой инцидент 2026-08-02: одновременно работали два srp-agent.exe (задача
SYSTEM и запущенный вручную). Они делят один C:\\SRP\buffer.jsonl, а flush_buffer
читает файл целиком, отправляет всё и записывает остаток обратно — поэтому запись
одного процесса ВОСКРЕШАЛА строки, которые другой уже доставил. Буфер не пустел
никогда, в журнале сервера шёл бесконечный поток успешных POST'ов.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_agent_exposes_single_instance_guard() -> None:
    from client import agent as ag

    assert hasattr(ag, "acquire_single_instance"), "у агента нет защиты от второго экземпляра"


def test_second_daemon_instance_refuses_to_start(monkeypatch) -> None:
    """Второй демон обязан завершиться, а не встать в тот же буфер."""
    import ctypes

    from client import agent as ag

    class _Fn:
        argtypes: list = []
        restype = None

        def __call__(self, *_a):
            return 12345  # непустой хэндл

    class _K32:
        CreateMutexW = _Fn()

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _K32())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 183)  # ERROR_ALREADY_EXISTS
    assert ag.acquire_single_instance() is None

    # У обычного пользователя нет права создавать Global-объекты: отказ в доступе
    # тоже означает «второй копией не стартуем».
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)  # ERROR_ACCESS_DENIED
    assert ag.acquire_single_instance() is None


def test_first_daemon_instance_starts(monkeypatch) -> None:
    import ctypes

    from client import agent as ag

    class _Fn:
        argtypes: list = []
        restype = None

        def __call__(self, *_a):
            return 4242

    class _K32:
        CreateMutexW = _Fn()

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _K32())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0)

    assert ag.acquire_single_instance() == 4242


def test_guard_survives_non_windows(monkeypatch) -> None:
    """На не-Windows (и при отказе CreateMutexW) агент обязан работать, а не падать."""
    import ctypes

    from client import agent as ag

    def _boom(*_a, **_k):
        raise AttributeError("no WinDLL here")

    monkeypatch.setattr(ctypes, "WinDLL", _boom)
    assert ag.acquire_single_instance() == 0  # 0 = «защиты нет, но работаем»
