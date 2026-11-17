"""Сборочный помощник (в работе не импортируется): заново пишет .ico трея сервера.

Запускать, когда меняются цвета или рисунок::

    python -m server.tray_icons._make_icons

Значок — «сервер»: скруглённый квадрат из трёх блоков стойки, 16×16, 32 бит BGRA. На ПК
сервера стоит и трей агента с кружком, поэтому форма другая. Только stdlib (без Pillow);
.ico лежат в git, скрипт их лишь воспроизводит. Образец — client/tray/assets/_make_icons.py.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

_SIZE = 16
# Токены дашборда: good #10d97a, warn #f59e0b; красный — как у трея агента. beat — светлый
# кадр «вспышки»: значок мигает им, когда пришёл новый сигнал агента.
_COLORS = {"ok": "10d97a", "beat": "a7f3d0", "warn": "f59e0b", "alert": "f43f5e"}


def _alpha(x: int, y: int) -> int:
    """Непрозрачность пикселя: квадрат 14×14 с радиусом углов 3, щели в строках 5 и 10."""
    if y in (5, 10):
        return 0
    # Расстояние со знаком от центра пикселя до края скруглённого квадрата; край сглажен.
    qx, qy = abs(x + 0.5 - 8) - 4, abs(y + 0.5 - 8) - 4
    dist = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - 3
    return round(255 * min(max(0.5 - dist, 0), 1))


def _ico(rgb: str) -> bytes:
    r, g, b = bytes.fromhex(rgb)
    rows = reversed(range(_SIZE))  # строки картинки в .ico идут снизу вверх
    xor = b"".join(bytes((b, g, r, _alpha(x, y))) for y in rows for x in range(_SIZE))
    and_mask = b"\x00" * (4 * _SIZE)  # 1 бит на пиксель, строка добита до 4 байт; правит альфа
    bih = struct.pack("<IiiHHIIiiII", 40, _SIZE, _SIZE * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    image = bih + xor + and_mask
    icondir = struct.pack("<HHH", 0, 1, 1)  # reserved, тип «значок», один кадр
    entry = struct.pack("<BBBBHHII", _SIZE, _SIZE, 0, 0, 1, 32, len(image), 6 + 16)
    return icondir + entry + image


def main() -> None:
    here = Path(__file__).resolve().parent
    for name, rgb in _COLORS.items():
        path = here / f"{name}.ico"
        path.write_bytes(_ico(rgb))
        print(path, path.stat().st_size)


if __name__ == "__main__":
    main()
