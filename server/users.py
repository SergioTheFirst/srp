"""Учётные записи просмотра дашборда из интернета (спека 2026-11-05 §4-5).

Пароль придумывает не человек: ``add`` и ``passwd`` генерируют его
(``secrets.token_urlsafe(12)``, ~96 бит) и печатают один раз; в базе -- только
scrypt-хэш. Управление -- только на сервере, веб-страницы нет: окно в меню значка
сервера (server/tray_tools.py, те же функции) или запасной путь:

    users.bat add <имя>            -- новая запись
    users.bat passwd <имя>         -- новый пароль, сессии записи гаснут
    users.bat disable <имя>        -- отключить, сессии записи гаснут
    users.bat enable <имя>
    users.bat list
    users.bat logout-all [<имя>]   -- погасить сессии записи, без имени -- все
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import secrets
import sys
from typing import Any, List, Optional

from server import db
from server.config import load_config

# ~40 мс и 16 МиБ на проверку; параметры пишутся в строку хэша.
_N, _R, _P = 2**14, 8, 1


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # OpenSSL требует 128*r*(n+p+2) байт; его потолок по умолчанию (32 МиБ) не пустил
    # бы хэш уже с n=2^15.
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * r * (n + p + 2), dklen=32
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${_scrypt(password, salt, _N, _R, _P).hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        got = _scrypt(password, bytes.fromhex(salt), int(n), int(r), int(p))
        want = bytes.fromhex(dk)
    except (ValueError, OverflowError):  # чужой формат хэша: не пускаем, а не роняем вход
        return False
    return algo == "scrypt" and hmac.compare_digest(got, want)


def token_sha256(token: str) -> str:
    """Ключ сессии в базе: сам токен cookie сервер не хранит."""
    return hashlib.sha256(token.encode()).hexdigest()


def _norm(name: str) -> str:
    # NOCASE SQLite не складывает кириллицу; хвостовой пробел подставляет клавиатура телефона.
    return name.strip().casefold()


def new_password() -> str:
    return secrets.token_urlsafe(12)


# Неизвестное имя и отключённая запись проверяются по нему: ответ и время те же, что у
# неверного пароля (спека §4).
_DUMMY_HASH = hash_password(new_password())


def add_user(name: str) -> Optional[str]:
    """Новая запись (users.bat add и окно трея): пароль или None -- имя пустое или занято."""
    name, password = _norm(name), new_password()
    if not name or not db.create_user(name, hash_password(password)):
        return None
    return password


def reset_password(user_id: int) -> str:
    """Новый пароль записи (users.bat passwd и окно трея); её сессии гаснут."""
    password = new_password()
    db.set_user_password(user_id, hash_password(password))
    return password


def login(name: str, password: str) -> Optional[str]:
    """Вся транзакция входа -- один вызов из пула потоков: токен для cookie или None."""
    user = db.get_user(_norm(name))
    if user is None or user["disabled"]:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    if not db.create_session(token_sha256(token), user["id"], user["password_hash"]):
        return None  # passwd/disable успели, пока шла проверка
    return token


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="users.bat", description="Учётные записи просмотра дашборда из интернета"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd, text in (
        ("add", "новая запись; пароль печатается один раз"),
        ("passwd", "новый пароль (печатается один раз); сессии записи гаснут"),
        ("disable", "отключить запись; её сессии гаснут"),
        ("enable", "включить запись"),
    ):
        sub.add_parser(cmd, help=text).add_argument("name", help="имя записи")
    sub.add_parser("list", help="все записи")
    sub.add_parser("logout-all", help="погасить сессии записи; без имени -- все").add_argument(
        "name", nargs="?", help="имя записи"
    )
    return p


def _print_password(password: str) -> None:
    print(f"пароль (показывается один раз, передайте лично): {password}")


def _add(name: str) -> int:
    password = add_user(name)
    if password is None:
        print(f"запись «{name}» уже есть", file=sys.stderr)
        return 1
    print(f"запись «{name}» создана")
    _print_password(password)
    return 0


def _edit(cmd: str, user: dict[str, Any]) -> int:
    name = user["username"]
    if cmd == "passwd":
        password = reset_password(user["id"])
        print(f"пароль записи «{name}» сменён, её сессии погашены")
        _print_password(password)
    elif cmd == "disable":
        db.set_user_disabled(user["id"], True)
        print(f"запись «{name}» отключена, её сессии погашены")
    elif cmd == "enable":
        db.set_user_disabled(user["id"], False)
        print(f"запись «{name}» включена")
    else:  # logout-all <имя>
        print(f"погашено сессий записи «{name}»: {db.delete_sessions(user['id'])}")
    return 0


def _list() -> int:
    rows = db.list_users()
    if not rows:
        print("записей нет")
    for u in rows:
        state = "отключена" if u["disabled"] else "активна"
        print(
            f"{u['username']}\t{state}\tсоздана {u['created_at'][:10]}"
            f"\tпароль от {u['password_changed_at'][:10]}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    db.init_db(load_config().resolved_db_path())
    if args.cmd == "list":
        return _list()
    name = _norm(args.name or "")
    if args.cmd == "logout-all" and not name:
        print(f"погашено сессий: {db.delete_sessions()}")
        return 0
    if not name:
        print("имя записи не может быть пустым", file=sys.stderr)
        return 2
    if args.cmd == "add":
        return _add(name)
    user = db.get_user(name)
    if user is None:
        print(f"записи «{name}» нет", file=sys.stderr)
        return 1
    return _edit(args.cmd, user)


if __name__ == "__main__":
    raise SystemExit(main())
