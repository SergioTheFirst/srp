"""Горячая копия базы сервера: server/backup.py.

Копию делает `sqlite3.Connection.backup()`, а не `copy srp.db`. База открыта в
режиме WAL: свежие страницы лежат в файле `-wal`, и обычное копирование одного
`.db` уносит их мимо копии -- получается файл, который восстановится молча
испорченным. Отсюда состав проверок: копия видит незачекпойнченные страницы,
битый исходник не оставляет за собой огрызка копии, а чистка старых копий
трогает только свои файлы.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest
from server import backup

pytestmark = pytest.mark.unit


def _make_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def _count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        conn.close()


def test_creates_readable_copy(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db, rows=5)

    result = backup.run(db, tmp_path / "backups")

    copy = Path(result["path"])
    assert copy.exists()
    assert copy.parent == tmp_path / "backups"
    assert _count(copy) == 5
    assert result["size_bytes"] == copy.stat().st_size


def test_copy_sees_uncheckpointed_wal_pages(tmp_path: Path) -> None:
    """Главная причина backup API: живой сервер держит WAL незакрытым."""
    db = tmp_path / "srp.db"
    _make_db(db, rows=2)

    live = sqlite3.connect(str(db))
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO t (v) VALUES ('after-wal')")
        live.commit()
        assert (tmp_path / "srp.db-wal").exists(), "тест бессмыслен без файла WAL"

        result = backup.run(db, tmp_path / "backups")
    finally:
        live.close()

    assert _count(Path(result["path"])) == 3


def test_retention_keeps_newest_and_reports_removals(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db)
    backups = tmp_path / "backups"
    backups.mkdir()
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (backups / f"srp-{stamp}.db").write_bytes(b"old")

    result = backup.run(db, backups, keep=2)

    names = sorted(p.name for p in backups.glob("*.db"))
    assert len(names) == 2
    assert names[-1] == Path(result["path"]).name  # свежая копия всегда остаётся
    assert result["removed"] == 2


def test_retention_never_touches_foreign_files(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db)
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "srp-20260101-000000.db").write_bytes(b"old")
    (backups / "note.txt").write_text("важное")
    (backups / "other.db").write_bytes(b"foreign db")

    backup.run(db, backups, keep=1)

    assert (backups / "note.txt").exists()
    assert (backups / "other.db").exists()
    assert not (backups / "srp-20260101-000000.db").exists()


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup.run(tmp_path / "нет.db", tmp_path / "backups")


def test_broken_source_leaves_no_half_copy(tmp_path: Path) -> None:
    """Огрызок копии опаснее её отсутствия: его однажды восстановят."""
    db = tmp_path / "srp.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)  # заголовок есть, данных нет
    backups = tmp_path / "backups"

    with pytest.raises(sqlite3.DatabaseError):
        backup.run(db, backups)

    assert list(backups.glob("*.db")) == []


def test_keep_below_one_rejected(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db)

    with pytest.raises(ValueError):
        backup.run(db, tmp_path / "backups", keep=0)


def test_default_dir_is_backups_next_to_db(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db)

    result = backup.run(db)

    assert Path(result["path"]).parent == tmp_path / "backups"


def test_restore_puts_rows_back(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db, rows=2)
    saved = Path(backup.run(db, tmp_path / "backups")["path"])

    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO t (v) VALUES ('after-backup')")
    conn.commit()
    conn.close()
    assert _count(db) == 3

    result = backup.restore(saved, db)

    assert _count(db) == 2
    assert Path(result["previous"]).exists()  # прежняя база отложена, не стёрта


def test_restore_removes_stale_wal_and_shm(tmp_path: Path) -> None:
    """WAL от новой версии поверх старого файла базы -- это порча данных."""
    db = tmp_path / "srp.db"
    _make_db(db)
    saved = Path(backup.run(db, tmp_path / "backups")["path"])
    (tmp_path / "srp.db-wal").write_bytes(b"stale wal")
    (tmp_path / "srp.db-shm").write_bytes(b"stale shm")

    backup.restore(saved, db)

    assert not (tmp_path / "srp.db-wal").exists()
    assert not (tmp_path / "srp.db-shm").exists()


def test_restore_refuses_broken_backup(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db, rows=4)
    broken = tmp_path / "srp-20260101-000000.db"
    broken.write_bytes(b"not a database at all")

    with pytest.raises(sqlite3.DatabaseError):
        backup.restore(broken, db)

    assert _count(db) == 4  # рабочая база не тронута


def test_restore_without_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db)
    saved = Path(backup.run(db, tmp_path / "backups")["path"])
    db.unlink()

    result = backup.restore(saved, db)

    assert _count(db) == 3
    assert result["previous"] is None


def test_fresh_copy_survives_even_if_an_old_one_looks_newer(tmp_path: Path) -> None:
    """Свежая копия не удаляется ни при каком порядке mtime: две копии в одну
    секунду получают одинаковое время, и сортировка по имени решала бы неверно."""
    db = tmp_path / "srp.db"
    _make_db(db)
    backups = tmp_path / "backups"
    backups.mkdir()
    stale = backups / "srp-20260101-000000.db"
    stale.write_bytes(b"old")
    os.utime(stale, (time.time() + 3600, time.time() + 3600))

    result = backup.run(db, backups, keep=1)

    assert Path(result["path"]).exists()
    assert not stale.exists()


def test_restore_puts_the_live_db_back_when_the_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой посередине подмены не имеет права оставить место базы пустым:
    первый же старт сервера создал бы там пустую базу, и данные исчезли бы
    окончательно -- вместе с той, ради которой всё затевалось."""
    db = tmp_path / "srp.db"
    _make_db(db, rows=4)
    saved = Path(backup.run(db, tmp_path / "backups")["path"])
    (tmp_path / "srp.db-wal").write_bytes(b"live wal")

    real_replace = os.replace

    def flaky(src, dst):  # type: ignore[no-untyped-def]
        if str(src).endswith(".restoring"):
            raise OSError("на диске нет места")
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", flaky)

    with pytest.raises(OSError):
        backup.restore(saved, db)

    assert db.exists(), "живая база должна вернуться на своё место"
    # Журнал проверяем ДО открытия базы: sqlite сносит чужой -wal при первом же
    # подключении, и проверка после _count() ничего бы не значила.
    assert (tmp_path / "srp.db-wal").read_bytes() == b"live wal"
    assert not list(tmp_path.glob("*.restoring"))
    assert not list(tmp_path.glob("*.before-restore*"))
    assert _count(db) == 4


def test_second_copy_in_the_same_second_gets_a_suffix(tmp_path: Path) -> None:
    """Две копии в одну секунду не должны затирать друг друга."""
    stamp = datetime(2026, 9, 9, 17, 49, 56)
    first = backup._target(tmp_path, "srp", stamp)
    first.write_bytes(b"first")

    second = backup._target(tmp_path, "srp", stamp)

    assert first.name == "srp-20260909-174956.db"
    assert second.name == "srp-20260909-174956-2.db"


def test_settings_are_copied_next_to_the_db_copy(tmp_path: Path) -> None:
    """Откат кода без настроек ложный: в git лежит шаблон config.json без
    токенов, и `git reset --hard` выключил бы проверку ingest-токена совсем."""
    db = tmp_path / "srp.db"
    _make_db(db)
    config = tmp_path / "config.json"
    config.write_text('{"ingest_token": "боевой"}', encoding="utf-8")

    result = backup.run(db, tmp_path / "backups", extras=[config, tmp_path / "нет.json"])

    companion = Path(result["extras"][0])
    assert result["extras"] == [str(companion)]  # несуществующий файл молча пропущен
    assert companion.name == Path(result["path"]).stem + ".config.json"
    assert companion.read_text(encoding="utf-8") == '{"ingest_token": "боевой"}'


def test_settings_are_removed_together_with_their_db_copy(tmp_path: Path) -> None:
    db = tmp_path / "srp.db"
    _make_db(db)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    backups = tmp_path / "backups"

    first = backup.run(db, backups, keep=2, extras=[config])
    second = backup.run(db, backups, keep=1, extras=[config])

    assert not Path(first["extras"][0]).exists(), "настройки не должны пережить свою копию"
    assert Path(second["extras"][0]).exists()


def test_second_restore_does_not_overwrite_the_first_saved_db(tmp_path: Path) -> None:
    """Оператор возвращает не ту копию, потом правильную. Если второй возврат
    затрёт .before-restore, всё, что накопилось между ними, исчезнет молча."""
    db = tmp_path / "srp.db"
    _make_db(db, rows=1)
    saved = Path(backup.run(db, tmp_path / "backups")["path"])

    first = backup.restore(saved, db)
    second = backup.restore(saved, db)

    assert first["previous"] != second["previous"]
    assert Path(first["previous"]).exists()
    assert Path(second["previous"]).exists()
