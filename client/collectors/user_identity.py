r"""Read + STRICTLY validate the tray's personal-data spool (o5 блок F, F9).

Трей (сессия пользователя) кладёт ФИО/должность/телефон в ``C:\SRP\spool``;
SYSTEM-агент читает их здесь и подмешивает в конверт. Спул пишет НЕадминистратор,
поэтому вход считается враждебным: ограничены число файлов, размер файла, длины
строк и окно свежести; битый файл пропускается, исключения не поднимаются.

При нескольких файлах побеждает самый свежий по ``written_at``. Кандидаты
отбираются по времени изменения файла (не по алфавиту: иначе 50 файлов
``owner-0*.json`` навсегда затенили бы настоящий спул), при равенстве --
по имени, поэтому результат детерминирован и не зависит от порядка обхода.

Чистый stdlib (инвариант «агент без зависимостей»): лимиты дублируют max_length
из ``shared/schema.py`` и не превышают их, поэтому корректный агент никогда не
получит отказ на границе сервера.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from stat import S_ISREG
from typing import Any, Optional

_MAX_FILES = 50
_MAX_FILE_BYTES = 16 * 1024
_FRESH_SEC = 30 * 86_400  # спул уволившегося/неактивного пользователя стареет
_FUTURE_SLACK_SEC = 86_400  # запас на расхождение часов; дальше -- подлог

# Дублируют max_length из shared/schema.py (клиент stdlib-only, схему не импортирует).
_FULL_NAME_MAX = 140
_POSITION_MAX = 140
_PHONE_MAX = 32

SPOOL_GLOB = "owner-*.json"

_EMPTY: dict[str, Optional[str]] = {
    "owner_full_name": None,
    "owner_position": None,
    "owner_phone": None,
}


def _clip(value: Any, limit: int) -> Optional[str]:
    """Строка, обрезанная по лимиту; None для пустых и не-строк."""
    if not isinstance(value, str):
        return None
    text = value.strip()[:limit]
    return text or None


def _candidates(spool_dir: Path) -> list[Path]:
    """Обычные файлы спула, свежие первыми, не больше _MAX_FILES.

    Один stat на файл И персональный try/except: файл, исчезнувший между glob и
    stat, пропускается САМ, а не роняет чтение всего каталога. Внимание: в 3.10
    `Path.is_file()` пробрасывает OSError без «игнорируемого» errno, поэтому
    проверка типа делается по уже полученному st_mode, а не отдельным вызовом.
    Иначе любой пользователь (каталог доступен всем Users на запись) держал бы
    сбор выключенным, создавая и удаляя файлы -- тот же класс, что OverflowError.
    """
    try:
        raw = list(Path(spool_dir).glob(SPOOL_GLOB))
    except OSError:
        return []
    rows: list[tuple[float, str, Path]] = []
    for path in raw:
        try:
            info = path.stat()
            if not S_ISREG(info.st_mode):
                continue
            rows.append((-info.st_mtime, path.name, path))
        except OSError:
            continue
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows[:_MAX_FILES]]


def _load(path: Path, now: float) -> Optional[tuple[int, dict[str, Optional[str]]]]:
    """(written_at, поля) из одного файла спула; None, если файл негоден."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        # OverflowError -- НЕ подкласс ValueError: json.loads превращает 1e400 в
        # float('inf'), а int(inf) бросает именно его. Без этого один подложенный
        # файл на 70 байт валит чтение для ВСЕХ пользователей машины.
        written_at = int(data.get("written_at") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    # Верхняя граница обязательна: written_at -- это ключ выбора победителя, и
    # дата из будущего иначе навсегда закрепляет подложенные данные.
    if written_at <= 0 or written_at > now + _FUTURE_SLACK_SEC:
        return None
    if (now - written_at) > _FRESH_SEC:
        return None
    fields = {
        "owner_full_name": _clip(data.get("full_name"), _FULL_NAME_MAX),
        "owner_position": _clip(data.get("position"), _POSITION_MAX),
        "owner_phone": _clip(data.get("phone"), _PHONE_MAX),
    }
    if not any(fields.values()):
        return None
    return written_at, fields


def read_owner_identity(
    spool_dir: Path, *, now: Optional[float] = None
) -> dict[str, Optional[str]]:
    """Персональные данные владельца ПК из спула; все None, если данных нет."""
    moment = time.time() if now is None else now
    # Кап по СВЕЖЕСТИ файла, а не по алфавиту: лексикографический срез позволял
    # создать 50 файлов «owner-0*.json» и навсегда затенить настоящий спул.
    paths = _candidates(Path(spool_dir))

    best: Optional[tuple[int, dict[str, Optional[str]]]] = None
    for path in paths:  # пути уже отсортированы -> при равном written_at победит первый
        loaded = _load(path, moment)
        if loaded is None:
            continue
        if best is None or loaded[0] > best[0]:
            best = loaded
    return dict(best[1]) if best is not None else dict(_EMPTY)


def _spool_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path("C:/SRP")
    return base / "spool"


def collect_owner_identity() -> dict[str, Optional[str]]:
    """Обёртка над установочным каталогом (образец -- user_certs)."""
    return read_owner_identity(_spool_dir())
