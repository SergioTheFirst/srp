"""Учётные записи просмотра из интернета: scrypt, вход одним вызовом, CLI (спека 2026-11-05 §4-5)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, List

import pytest
from server import db, users


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLI открывает базу из конфига сам: оба пути -- во временный каталог, боевые
    # server/config.json и srp.db тесты не трогают.
    monkeypatch.setenv("SRP_CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("SRP_DB_PATH", str(tmp_path / "t.db"))
    db.init_db(tmp_path / "t.db")


def _cli(*argv: str) -> int:
    return users.main(list(argv))


def _add(name: str) -> int:
    assert db.create_user(name, users.hash_password("pw"))
    user = db.get_user(name)
    assert user is not None
    return int(user["id"])


def _key(name: str) -> str:
    token = users.login(name, "pw")
    assert token is not None
    return users.token_sha256(token)


def _sessions() -> int:
    with db._connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def _printed_password(out: str) -> str:
    found = re.findall(r": ([A-Za-z0-9_-]{16})$", out, re.M)  # token_urlsafe(12)
    assert len(found) == 1, out
    return str(found[0])


def test_hash_with_other_params_verifies() -> None:
    # Параметры живут в строке хэша; n=2^15 не влезает в дефолтные 32 МиБ OpenSSL.
    salt = bytes(16)
    dk = hashlib.scrypt(b"pw", salt=salt, n=2**15, r=8, p=1, maxmem=64 * 2**20, dklen=32)
    stored = f"scrypt${2**15}$8$1${salt.hex()}${dk.hex()}"
    assert users.verify_password("pw", stored)
    assert not users.verify_password("pw2", stored)
    assert not users.verify_password("pw", "мусор")


def test_login_keeps_only_token_digest() -> None:
    _add("иванов")
    token = users.login("Иванов ", "pw")
    assert token is not None
    with db._connect() as conn:
        rows = [r[0] for r in conn.execute("SELECT token_sha256 FROM sessions")]
    assert rows == [hashlib.sha256(token.encode()).hexdigest()]
    assert db.get_session_user(rows[0]) == "иванов"


@pytest.mark.parametrize(
    ("name", "password"), [("никто", "pw"), ("отключён", "pw"), ("иванов", "wrong")]
)
def test_login_refusals_look_alike(
    monkeypatch: pytest.MonkeyPatch, name: str, password: str
) -> None:
    _add("иванов")
    db.set_user_disabled(_add("отключён"), True)
    calls: List[Any] = []
    real = users._scrypt

    def spy(*args: Any) -> bytes:
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(users, "_scrypt", spy)
    assert users.login(name, password) is None
    assert len(calls) == 1  # фиктивный хэш: время то же, что у неверного пароля
    assert _sessions() == 0


def test_name_casefold_is_one_record(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli("add", "Иванов") == 0
    assert _cli("add", "иванов") == 1
    assert [u["username"] for u in db.list_users()] == ["иванов"]
    assert users.login("ИВАНОВ", _printed_password(capsys.readouterr().out)) is not None


def test_window_and_cli_share_password_functions() -> None:
    # Окно паролей в меню значка зовёт те же функции, что users.bat: пароль -- результатом.
    first = users.add_user(" Иванов ")
    assert first is not None and users.login("иванов", first) is not None
    assert users.add_user("иванов") is None  # запись уже есть
    assert users.add_user("  ") is None
    assert [u["username"] for u in db.list_users()] == ["иванов"]
    user = db.get_user("иванов")
    assert user is not None
    second = users.reset_password(user["id"])
    assert second != first and len(second) == 16
    assert _sessions() == 0  # новый пароль гасит входы записи
    assert users.login("иванов", first) is None
    assert users.login("иванов", second) is not None


def test_cli_prints_working_password_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli("add", "иванов") == 0
    first = _printed_password(capsys.readouterr().out)
    assert users.login("иванов", first) is not None
    assert _cli("passwd", "иванов") == 0
    second = _printed_password(capsys.readouterr().out)
    assert second != first
    assert users.login("иванов", first) is None
    assert users.login("иванов", second) is not None


@pytest.mark.parametrize(
    ("argv", "petrov_left"),
    [
        (("passwd", "Иванов"), True),
        (("disable", "иванов"), True),
        (("logout-all", "иванов"), True),
        (("logout-all",), False),
    ],
)
def test_cli_kills_sessions(argv: tuple, petrov_left: bool) -> None:
    _add("иванов")
    _add("петров")
    ivanov, petrov = _key("иванов"), _key("петров")
    assert _cli(*argv) == 0
    assert db.get_session_user(ivanov) is None
    assert (db.get_session_user(petrov) == "петров") is petrov_left
    assert _sessions() == int(petrov_left)  # строка удалена, а не только скрыта


@pytest.mark.parametrize("cmd", ["passwd", "disable"])
def test_login_racing_users_bat_creates_no_session(
    monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    # users.bat (другой процесс) успел, пока шёл scrypt по прочитанной записи: его DELETE
    # сессий уже прошёл, поэтому сессию не даёт сама вставка.
    _add("иванов")
    real = users.verify_password

    def racing(password: str, stored: str) -> bool:
        ok = real(password, stored)
        assert _cli(cmd, "иванов") == 0
        return ok

    monkeypatch.setattr(users, "verify_password", racing)
    assert users.login("иванов", "pw") is None
    assert _sessions() == 0


def test_disabled_checked_on_every_request() -> None:
    # Строка сессии цела, а запись отключена (спека §5: disabled -- на каждом запросе).
    uid = _add("иванов")
    key = _key("иванов")
    with db._connect() as conn:
        conn.execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
    assert db.get_session_user(key) is None
    assert _sessions() == 1


def test_expired_session_refused_and_pruned_on_login() -> None:
    _add("иванов")
    old = _key("иванов")

    def age(**delta: float) -> None:
        with db._connect() as conn:
            conn.execute("UPDATE sessions SET created_at=?", (db._cutoff_iso(**delta),))

    age(days=db.SESSION_DAYS - 1)
    assert db.get_session_user(old) == "иванов"
    age(days=db.SESSION_DAYS, minutes=1)
    assert db.get_session_user(old) is None
    _key("иванов")  # вход заодно удаляет просроченные
    assert _sessions() == 1


def test_cli_list_enable_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    _add("иванов")
    assert _cli("disable", "иванов") == 0
    assert users.login("иванов", "pw") is None
    assert _cli("list") == 0
    out = capsys.readouterr().out
    assert "иванов" in out and "отключена" in out
    assert "scrypt$" not in out
    assert _cli("enable", "иванов") == 0
    assert users.login("иванов", "pw") is not None
    assert _cli("passwd", "никто") == 1
    assert _cli("add", "  ") == 2


def test_enable_keeps_sessions_of_an_active_account() -> None:
    # «enable» на уже включённой записи не выкидывает её устройства (спека §5: гасят только
    # выход, disable, passwd и logout-all).
    _add("иванов")
    assert users.login("иванов", "pw") is not None
    assert _cli("enable", "иванов") == 0
    assert _sessions() == 1
