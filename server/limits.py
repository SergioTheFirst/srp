"""Границы сборки и демо-режима.

MAX_DEVICES — сколько компьютеров принимает /ingest; None = без ограничения.
В бесплатной версии — 3 (см. README, «Ограничения бесплатной версии»).
"""

import os
from typing import Optional

MAX_DEVICES: Optional[int] = 3

DEMO_MODE: bool = os.environ.get("SRP_DEMO", "") == "1"

DEMO_READONLY_DETAIL = "демо-режим: только чтение"


def limit_detail(n: int) -> str:
    return f"бесплатная версия: до {n} компьютеров — новый device_id отклонён"
