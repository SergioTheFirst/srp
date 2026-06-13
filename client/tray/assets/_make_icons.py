"""Build helper (NOT imported at runtime): regenerate the 3 tray .ico files.

Run once when the status palette changes::

    python -m client.tray.assets._make_icons

Each icon is a 16x16 32-bit BGRA filled disc on a transparent background -- a
plain status dot, matching the dashboard good/warn/bad tokens. Pure stdlib so it
needs no Pillow. The .ico binaries are committed; this script only reproduces them.
"""

from __future__ import annotations

import struct
from pathlib import Path

_SIZE = 16
# BGRA, matching dashboard tokens: good #10d97a, warn #f59e0b, bad #f43f5e.
_COLORS = {
    "ok": (122, 217, 16),
    "warn": (11, 158, 245),
    "alert": (94, 63, 244),
}


def _disc_bgra(color: tuple[int, int, int]) -> bytes:
    """16x16 BGRA pixels, bottom-up, a filled antialiased-ish disc."""
    b, g, r = color
    cx = cy = (_SIZE - 1) / 2.0
    radius = 7.2
    rows: list[bytes] = []
    for y in range(_SIZE):
        row = bytearray()
        for x in range(_SIZE):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            alpha = 255 if dist <= radius else (110 if dist <= radius + 1.0 else 0)
            row += bytes((b, g, r, alpha))
        rows.append(bytes(row))
    return b"".join(reversed(rows))  # ICO XOR bitmap is bottom-up


def _ico(color: tuple[int, int, int]) -> bytes:
    xor = _disc_bgra(color)
    and_mask = b"\x00" * (4 * _SIZE)  # 1bpp, row-padded to 4 bytes; alpha governs
    bih = struct.pack("<IiiHHIIiiII", 40, _SIZE, _SIZE * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    image = bih + xor + and_mask
    icondir = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    entry = struct.pack("<BBBBHHII", _SIZE, _SIZE, 0, 0, 1, 32, len(image), 6 + 16)
    return icondir + entry + image


def main() -> None:
    here = Path(__file__).resolve().parent
    for name, color in _COLORS.items():
        path = here / f"srp_{name}.ico"
        path.write_bytes(_ico(color))
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
