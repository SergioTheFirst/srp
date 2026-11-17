"""J-Space: значок сервера в трее и надзор за сервером (запуск — server_tray.bat).

Каталог этого файла — корень и sys.path[0]: импорт server работает из любого текущего каталога
(значение Run запускает pythonw из System32). Spec: 2026-11-07-server-tray-design.md.
"""

import contextlib
import sys
import time
import traceback
from pathlib import Path


def _crash(logs: Path, alert) -> int:
    """Трей не поднялся: трассировка — в logs\\tray.log, окно — владельцу (pythonw молчит)."""
    with contextlib.suppress(OSError):
        logs.mkdir(exist_ok=True)
        with open(logs / "tray.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} трей не запустился\n")
            fh.write(traceback.format_exc())
    alert(f"Значок сервера J-Space не запустился.\n\nПодробности: {logs / 'tray.log'}")
    return 1


def _alert(text: str) -> None:
    import ctypes

    ctypes.WinDLL("user32").MessageBoxW(None, text, "J-Space сервер", 0x10)  # MB_ICONERROR


if __name__ == "__main__":
    try:
        from server.tray import main

        code = main(sys.argv[1:])
    except Exception:
        code = _crash(Path(__file__).resolve().parent / "logs", _alert)
    sys.exit(code)
