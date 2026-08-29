r"""Write the personal-data spool the SYSTEM agent reads (o5 блок F, F8).

Пользователь вводит ФИО/должность/телефон в трее (сессия пользователя), а конверт
на сервер шлёт SYSTEM-агент. Прямой путь «трей пишет config.json» не работает:
``C:\SRP`` закрыт на запись для Users (``icacls /inheritance:r``), а ``save_config``
дополнительно требует пароль. Поэтому канал тот же, что у личных сертификатов:
файл в ``C:\SRP\spool`` -- единственном каталоге, где Users могут писать, -- а
агент его строго валидирует (``client/collectors/user_identity.py``).

Один файл на пользователя (многопользовательский ПК не затирает сам себя),
атомарная запись, любые ошибки глушатся: сбой спула не должен ронять трей.
Чистый stdlib.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from client.tray.spool import _install_spool_dir, _safe_name, _username

# Дублируют max_length из shared/schema.py: клиент stdlib-only и не может
# импортировать pydantic-схему. Значения обязаны совпадать с контрактом.
OWNER_FULL_NAME_MAX = 140
OWNER_POSITION_MAX = 140
OWNER_PHONE_MAX = 32

_MAX_OWNER = 64
# Кап на чтение своего же спула: файл лежит в каталоге, доступном всем Users.
_MAX_READ_BYTES = 16 * 1024

_FIELDS = (
    ("ФИО", OWNER_FULL_NAME_MAX),
    ("Должность", OWNER_POSITION_MAX),
    ("Телефон", OWNER_PHONE_MAX),
)


def validate_owner_fields(full_name: str, position: str, phone: str) -> Optional[str]:
    """Русский текст ошибки при превышении лимита, иначе None.

    Чистая функция без tkinter: её зовёт и окно трея, и тесты. Пустые значения
    допустимы -- пользователь вправе не заполнять поле.
    """
    for value, (label, limit) in zip((full_name, position, phone), _FIELDS):
        if len(str(value).strip()) > limit:
            return f"«{label}»: не больше {limit} символов."
    return None


def build_owner_spool(
    owner: str, full_name: str, position: str, phone: str, now: float
) -> dict[str, Any]:
    """Документ спула: владелец + эпоха + три поля, обрезанные по лимитам."""
    return {
        "owner": str(owner)[:_MAX_OWNER],
        "written_at": int(now),
        "full_name": str(full_name).strip()[:OWNER_FULL_NAME_MAX],
        "position": str(position).strip()[:OWNER_POSITION_MAX],
        "phone": str(phone).strip()[:OWNER_PHONE_MAX],
    }


def owner_spool_path(spool_dir: Path, owner: str) -> Path:
    return Path(spool_dir) / f"owner-{_safe_name(owner)}.json"


def write_owner_spool(
    spool_dir: Path,
    owner: str,
    full_name: str,
    position: str,
    phone: str,
    *,
    now: Optional[float] = None,
) -> bool:
    """Атомарно записать спул этого пользователя; True при успехе."""
    moment = time.time() if now is None else now
    path = owner_spool_path(spool_dir, owner)
    tmp = path.with_name(path.name + ".tmp")  # не *.json -> агентский glob его пропустит
    try:
        Path(spool_dir).mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(
                build_owner_spool(owner, full_name, position, phone, moment), ensure_ascii=False
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False


def read_own_spool() -> dict[str, str]:
    """Текущие значения этого пользователя (для предзаполнения окна); {} если нет."""
    path = owner_spool_path(_install_spool_dir(), _username())
    try:
        # ValueError, а не json.JSONDecodeError: read_text бросает
        # UnicodeDecodeError (тоже ValueError) на не-UTF-8 байтах. Каталог спула
        # доступен на запись всем Users, поэтому чужой файл не должен ронять окно.
        if path.stat().st_size > _MAX_READ_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "full_name": str(data.get("full_name") or "")[:OWNER_FULL_NAME_MAX],
        "position": str(data.get("position") or "")[:OWNER_POSITION_MAX],
        "phone": str(data.get("phone") or "")[:OWNER_PHONE_MAX],
    }


def publish_owner_identity(full_name: str, position: str, phone: str) -> bool:
    """Хук трея: записать спул текущего пользователя в установочный каталог."""
    return write_owner_spool(_install_spool_dir(), _username(), full_name, position, phone)
