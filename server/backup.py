"""Горячая копия базы сервера перед обновлением.

Почему не `copy srp.db`: база открыта в режиме WAL, свежие страницы лежат в
соседнем файле `-wal`, и копирование одного `.db` уносит их мимо копии. Такой
файл либо не откроется, либо откроется молча неполным -- и выяснится это в тот
единственный день, когда копия понадобилась. `sqlite3.Connection.backup()`
снимает согласованный слепок вместе с WAL, после чего `PRAGMA quick_check`
проверяет результат.

Копия пишется во временный `.part` и переименовывается только после проверки:
огрызок копии опаснее её отсутствия, потому что однажды его восстановят.

Запуск:
    python -m server.backup                 -- база и каталог из server/config.json
    python -m server.backup --db srp.db --dir backups --keep 10
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, TypedDict, Union


class BackupResult(TypedDict):
    path: str
    size_bytes: int
    removed: int
    extras: List[str]


class RestoreResult(TypedDict):
    restored: str
    previous: Optional[str]


DEFAULT_KEEP = 10
_DIR_NAME = "backups"
# Чистка удаляет только собственные копии: в каталог кладут и посторонние файлы
# (заметка оператора, чужая база), потерять их из-за нашей ретенции нельзя.
_STAMP = r"\d{8}-\d{6}(?:-\d+)?"


def _stamp_re(stem: str) -> "re.Pattern[str]":
    return re.compile(r"^" + re.escape(stem) + r"-" + _STAMP + r"\.db$")


def _target(directory: Path, stem: str, now: datetime) -> Path:
    """Свободное имя копии; суффикс -N спасает от двух копий в одну секунду."""
    base = now.strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{stem}-{base}.db"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{base}-{counter}.db"
        counter += 1
    return candidate


def _copy(src_path: Path, part: Path) -> None:
    src = sqlite3.connect(str(src_path), timeout=30.0)
    try:
        dst = sqlite3.connect(str(part))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _verify(part: Path) -> None:
    conn = sqlite3.connect(str(part), timeout=30.0)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if not row or row[0] != "ok":
        raise sqlite3.DatabaseError(
            f"копия не прошла quick_check: {row[0] if row else 'нет ответа'}"
        )


def _copy_extras(final: Path, extras: Iterable[Union[str, Path]]) -> List[Path]:
    """Положить рядом с копией базы настройки сервера.

    Без них откат кода ложный: `server/config.json` и `org_directory.json` лежат
    в git, но на боевом сервере их правят на месте — там `ingest_token`,
    `admin_token`, `update_hmac_secret` и настоящий справочник организаций.
    `git reset --hard` вернул бы шаблон из репозитория, то есть выключил бы
    проверку токена вообще. Копия рядом с базой делает откат безопасным.
    """
    saved: List[Path] = []
    for extra in extras:
        source = Path(extra)
        if not source.is_file():
            continue
        companion = final.with_name(f"{final.stem}.{source.name}")
        try:
            shutil.copy2(source, companion)
        except OSError:
            continue  # настройки — приятный бонус, из-за них копию базы не рушим
        saved.append(companion)
    return saved


def _prune(directory: Path, stem: str, keep: int, fresh: Path) -> int:
    """Удалить лишние копии вместе с их файлами настроек. Только что снятую не
    трогаем ни при каком раскладе: две копии в одну секунду получают одинаковый
    mtime, и сортировка решала бы их судьбу по имени -- а имя с суффиксом ``-2``
    сортируется РАНЬШЕ обычного."""
    pattern = _stamp_re(stem)
    mine = [
        p
        for p in directory.iterdir()
        if p.is_file() and pattern.match(p.name) and p.name != fresh.name
    ]
    mine.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    removed = 0
    for stale in mine[keep - 1 :]:
        for companion in directory.glob(f"{glob.escape(stale.stem)}.*"):
            if companion != stale:
                with contextlib.suppress(OSError):
                    companion.unlink()
        try:
            stale.unlink()
            removed += 1
        except OSError:
            # Занятый файл -- не повод рушить уже сделанную копию: место
            # освободит следующий запуск.
            pass
    return removed


def run(
    db_path: Union[str, Path],
    backups_dir: Optional[Union[str, Path]] = None,
    *,
    keep: int = DEFAULT_KEEP,
    extras: Iterable[Union[str, Path]] = (),
) -> BackupResult:
    """Снять копию базы, проверить её и подчистить старые. Вернуть путь и размер.

    ``extras`` — файлы настроек, которые лягут рядом с копией под её именем.
    Каталог копий принадлежит ОДНОМУ серверу: чистка удаляет всё, что подходит
    под ``<имя базы>-<штамп>.db``, поэтому общий каталог на несколько серверов
    SRP означает, что они будут удалять копии друг друга.
    """
    if keep < 1:
        raise ValueError("keep должен быть не меньше 1: без копий обновляться нельзя")

    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"базы нет: {source}")

    directory = Path(backups_dir) if backups_dir is not None else source.parent / _DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)

    final = _target(directory, source.stem, datetime.now())
    part = final.with_suffix(".part")
    try:
        _copy(source, part)
        _verify(part)
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    saved = _copy_extras(final, extras)
    return {
        "path": str(final),
        "size_bytes": final.stat().st_size,
        "removed": _prune(directory, source.stem, keep, final),
        "extras": [str(p) for p in saved],
    }


def restore(backup_path: Union[str, Path], db_path: Union[str, Path]) -> RestoreResult:
    """Вернуть базу из копии. Сервер должен быть остановлен.

    Копию сперва проверяем, и только потом трогаем рабочий файл: восстановить
    испорченную копию поверх живой базы -- худший исход из возможных.

    Живая тройка (``.db``, ``-wal``, ``-shm``) не удаляется, а отодвигается в
    ``.before-restore*``: журнал ``-wal`` принадлежит НОВОЙ версии, и SQLite
    накатил бы его на старую базу, смешав два состояния в одно нечитаемое, --
    но и стереть его нельзя, в нём могут лежать последние транзакции. Если
    подмена сорвётся посередине, все переносы откатываются в обратном порядке:
    остаться вообще без файла базы хуже, чем не восстановиться.
    """
    source = Path(backup_path)
    target = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"копии нет: {source}")

    _verify(source)

    staged = target.with_name(f"{target.stem}.{os.getpid()}.restoring")
    shutil.copy2(source, staged)
    # Штамп в имени: иначе второй подряд возврат затирает первый .before-restore,
    # и всё, что сервер успел накопить между двумя возвратами, исчезает молча.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    aside_base = f"{target.name}.before-restore-{stamp}"
    counter = 2
    while target.with_name(aside_base).exists():  # два возврата в одну секунду
        aside_base = f"{target.name}.before-restore-{stamp}-{counter}"
        counter += 1
    moved: List[Tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            live = target.with_name(target.name + suffix)
            if live.exists():
                aside = target.with_name(aside_base + suffix)
                os.replace(live, aside)
                moved.append((live, aside))
        os.replace(staged, target)
    except BaseException:
        for live, aside in reversed(moved):
            # Вернуть не удалось -- файл остаётся под именем .before-restore*,
            # и _warn_if_db_vanished покажет оператору, где он лежит.
            with contextlib.suppress(OSError):
                os.replace(aside, live)
        staged.unlink(missing_ok=True)
        raise

    previous = next((aside for live, aside in moved if live == target), None)

    return {"restored": str(target), "previous": str(previous) if previous else None}


def _default_db() -> Path:
    from server.config import load_config

    return load_config().resolved_db_path()


def _default_extras() -> List[Path]:
    """Настройки сервера: они правятся на месте и в git не совпадают с боевыми."""
    from server.config import load_config

    return [Path(__file__).with_name("config.json"), load_config().resolved_org_directory_path()]


def _warn_if_db_vanished(db_path: Path) -> None:
    """Последний рубеж: откат переносов мог сам не сработать (файл держат).
    Молчать тут нельзя -- на месте базы пусто, и первый же старт сервера
    создаст вместо неё пустую."""
    if db_path.exists():
        return
    kept = sorted(
        p
        for p in db_path.parent.glob(f"{glob.escape(db_path.name)}.before-restore-*")
        if not p.name.endswith(("-wal", "-shm"))  # это журналы той же тройки, не база
    )
    print(f"ВНИМАНИЕ: файла базы {db_path} сейчас НЕТ.", file=sys.stderr)
    if kept:
        aside = kept[-1]
        print(f"прежняя база лежит здесь: {aside}", file=sys.stderr)
        print(f'верните её командой:  move "{aside}" "{db_path}"', file=sys.stderr)
    print("НЕ запускайте сервер до этого: он создаст на её месте пустую.", file=sys.stderr)


def _restore_cli(backup_path: Path, db_path: Path) -> int:
    try:
        result = restore(backup_path, db_path)
    except PermissionError:
        # Windows не даёт подменить файл, который держит открытым живой сервер --
        # это и спасает от восстановления «на ходу».
        print(f"база занята: остановите сервер и повторите ({db_path})", file=sys.stderr)
        _warn_if_db_vanished(db_path)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"база не восстановлена: {exc}", file=sys.stderr)
        _warn_if_db_vanished(db_path)
        return 1

    print(f"база восстановлена из {backup_path}")
    if result["previous"]:
        print(f"прежний файл отложен: {result['previous']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Резервная копия базы сервера SRP")
    parser.add_argument(
        "--db", default=None, help="путь к базе (по умолчанию из server/config.json)"
    )
    parser.add_argument(
        "--dir", default=None, help="каталог копий (по умолчанию backups/ рядом с базой)"
    )
    parser.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP, help=f"сколько копий хранить ({DEFAULT_KEEP})"
    )
    parser.add_argument(
        "--restore", default=None, help="вернуть базу из этой копии (сервер должен быть остановлен)"
    )
    parser.add_argument("--quiet", action="store_true", help="печатать только путь копии")
    opts = parser.parse_args(argv)

    db_path = Path(opts.db) if opts.db else _default_db()
    if opts.restore:
        return _restore_cli(Path(opts.restore), db_path)

    try:
        result = run(db_path, opts.dir, keep=opts.keep, extras=_default_extras())
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"копия не создана: {exc}", file=sys.stderr)
        return 1

    if opts.quiet:
        # Только путь: его читает update.ps1, любая лишняя строка ему помеха.
        print(result["path"])
    else:
        size_mb = float(result["size_bytes"]) / (1024 * 1024)
        print(f"копия: {result['path']} ({size_mb:.1f} МБ), удалено старых: {result['removed']}")
        for extra in result["extras"]:
            print(f"настройки: {extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
