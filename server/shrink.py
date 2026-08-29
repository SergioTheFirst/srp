"""Разовая миграция существующей БД к компактному хранению (спека 2026-08-27).

Что делает (идемпотентно, батчами, каждый батч -- своя транзакция):

1. ``scores``: строки глубже новейших ``db._SCORES_KEEP_FULL`` на устройство
   ужимает до slim-вердикта (``db._slim_risk``); остальные переписывает
   компактно (``db._json_c``), если хранимая форма отличается.
2. ``printer_readings``: ``detail=NULL`` глубже новейших
   ``db._PRINTER_DETAIL_KEEP`` на принтер; у остальных -- компактный rewrite.
3. ``historical.payload``, ``net_topology_snapshots.graph``,
   ``net_device_readings.detail``, ``net_changes.detail``: компактный rewrite.
4. ``VACUUM`` (опционально) -- вернуть освобождённые страницы файлу.

Запуск ТОЛЬКО при остановленном сервере (обычный sqlite-файл, без координации
с _lock работающего процесса): ``python -m server.shrink --db srp.db``.
Повторный запуск -- no-op (счётчики нулевые). Битая строка пропускается
по-строчно и не роняет остальные.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

from server.db import _PRINTER_DETAIL_KEEP, _SCORES_KEEP_FULL, _json_c, _slim_risk

_BATCH = 500  # строк на транзакцию: ограничивает и память, и длину write-lock


def _keep_ids(conn: sqlite3.Connection, table: str, group_col: str, keep: int, max_id: int) -> set:
    """id новейших ``keep`` строк каждой группы (устройства/принтера) В ПРЕДЕЛАХ
    снимка ``max_id``: строки, вставленные живым процессом после старта, не
    участвуют в выборе «что оставить полным» (иначе свежая вставка вытесняла бы
    из keep-набора строку, которая на момент снимка была последней)."""
    ids: set = set()
    # nosec-примечание: {table}/{group_col}/{col} -- литералы call-site'ов этого
    # модуля, значения всегда идут связанными '?'-параметрами.
    for (gid,) in conn.execute(
        f"SELECT DISTINCT {group_col} FROM {table} WHERE id <= ?",  # nosec B608
        (max_id,),
    ):
        ids.update(
            r[0]
            for r in conn.execute(
                f"SELECT id FROM {table} WHERE {group_col}=? AND id <= ?"  # nosec B608
                " ORDER BY id DESC LIMIT ?",
                (gid, max_id, keep),
            )
        )
    return ids


def _rewrite_json_column(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    transform: Optional[Callable[[int, dict[str, Any]], Optional[dict[str, Any]]]] = None,
    max_id: int = 0,
) -> tuple[int, int, int]:
    """Пройти таблицу батчами; вернуть (переписано компактно, преобразовано, пропущено).

    ``transform(row_id, obj)`` возвращает новый объект (посчитается отдельно,
    например slim) или ``None`` -- тогда строка лишь переписывается компактно,
    если хранимая форма отличается от ``_json_c``. ``max_id`` -- верхняя граница
    снимка на старте run(): строки, вставленные живым процессом ВО ВРЕМЯ
    прогона, недосягаемы в принципе (ревью 2026-08-27: без границы прогон
    догонял свежие вставки и slim'ил ПОСЛЕДНИЙ полный вердикт устройства).
    Битая или не-dict строка пропускается и считается: полный провал миграции
    отличим от «уже мигрировано» по счётчику skipped."""
    rewritten = transformed = skipped = 0
    last_id = 0
    while True:
        rows = conn.execute(
            f"SELECT id, {col} FROM {table} WHERE id > ? AND id <= ?"  # nosec B608
            f" AND {col} IS NOT NULL ORDER BY id LIMIT ?",
            (last_id, max_id, _BATCH),
        ).fetchall()
        if not rows:
            break
        with conn:  # одна транзакция на батч
            for row_id, raw in rows:
                last_id = row_id
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict):
                        skipped += 1  # valid JSON, но не объект: инертна
                        continue
                    new_obj = transform(row_id, obj) if transform else None
                    if new_obj is not None:
                        conn.execute(
                            f"UPDATE {table} SET {col}=? WHERE id=?",  # nosec B608
                            (_json_c(new_obj), row_id),
                        )
                        transformed += 1
                        continue
                    compact = _json_c(obj)
                except (ValueError, TypeError, AttributeError, KeyError):
                    skipped += 1  # битая строка: пропустить, не ронять остальные
                    continue
                if compact != raw:
                    conn.execute(
                        f"UPDATE {table} SET {col}=? WHERE id=?",  # nosec B608
                        (compact, row_id),
                    )
                    rewritten += 1
    return rewritten, transformed, skipped


def _snapshot_max_id(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0])  # nosec B608


def run(db_path: Path | str, *, vacuum: bool = True) -> dict[str, int]:
    """Прогнать миграцию; вернуть счётчики по шагам.

    Все нули = уже мигрировано; ``*_skipped`` > 0 = битые строки, они
    оставлены как есть (и main() предупредит об этом)."""
    conn = sqlite3.connect(str(db_path))
    try:
        # Fail-fast при живом сервере: probe эксклюзивного лока без ожидания.
        # Это ловит частый случай (сервер пишет прямо сейчас); полную защиту
        # даёт снимок max_id ниже -- всё, что вставлено после старта, недосягаемо.
        conn.execute("PRAGMA busy_timeout=0")
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            raise SystemExit(
                "БД занята другим процессом — остановите сервер SRP перед ужатием"
            ) from exc

        stats: dict[str, int] = {}

        scores_max = _snapshot_max_id(conn, "scores")
        keep_full = _keep_ids(conn, "scores", "device_id", _SCORES_KEEP_FULL, scores_max)

        def _slim_old(row_id: int, obj: dict[str, Any]) -> Optional[dict[str, Any]]:
            if row_id in keep_full or obj.get("slim") == 1:
                return None
            return _slim_risk(obj)

        stats["scores_rewritten"], stats["scores_slimmed"], stats["scores_skipped"] = (
            _rewrite_json_column(conn, "scores", "risk", _slim_old, max_id=scores_max)
        )

        # detail=NULL глубже новейших K -- по одному принтеру за проход, тем же
        # подзапросом, что и store_printer_reading (ревью 2026-08-27: единый
        # NOT IN-список плейсхолдеров рос неограниченно с числом принтеров).
        prn_max = _snapshot_max_id(conn, "printer_readings")
        nulled = 0
        with conn:
            for (pid,) in conn.execute(
                "SELECT DISTINCT printer_id FROM printer_readings WHERE id <= ?", (prn_max,)
            ).fetchall():
                cur = conn.execute(
                    """UPDATE printer_readings SET detail=NULL
                        WHERE printer_id=? AND detail IS NOT NULL AND id <= ?
                          AND id NOT IN (SELECT id FROM printer_readings
                                         WHERE printer_id=? AND id <= ?
                                         ORDER BY id DESC LIMIT ?)""",
                    (pid, prn_max, pid, prn_max, _PRINTER_DETAIL_KEEP),
                )
                nulled += cur.rowcount
        stats["printer_detail_nulled"] = nulled
        stats["printer_detail_rewritten"], _, stats["printer_detail_skipped"] = (
            _rewrite_json_column(conn, "printer_readings", "detail", max_id=prn_max)
        )

        for key, table, col in [
            ("historical", "historical", "payload"),
            ("topology", "net_topology_snapshots", "graph"),
            ("net_readings", "net_device_readings", "detail"),
            ("net_changes", "net_changes", "detail"),
        ]:
            rewritten, _, skipped = _rewrite_json_column(
                conn, table, col, max_id=_snapshot_max_id(conn, table)
            )
            stats[f"{key}_rewritten"] = rewritten
            stats[f"{key}_skipped"] = skipped

        if vacuum:
            conn.execute("VACUUM")
        return stats
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Разовое ужатие srp.db (сервер остановлен!)")
    parser.add_argument("--db", default="srp.db", help="путь к файлу БД")
    parser.add_argument("--no-vacuum", action="store_true", help="не выполнять VACUUM")
    args = parser.parse_args()
    path = Path(args.db)
    before = path.stat().st_size
    stats = run(path, vacuum=not args.no_vacuum)
    after = path.stat().st_size
    for key, val in stats.items():
        print(f"{key}: {val}")
    skipped_total = sum(v for k, v in stats.items() if k.endswith("_skipped"))
    if skipped_total:
        print(
            f"ВНИМАНИЕ: {skipped_total} строк(и) с нечитаемым JSON пропущены и оставлены"
            " как есть — проверьте целостность БД (PRAGMA integrity_check)."
        )
    print(f"file: {before / 1048576:.1f} MB -> {after / 1048576:.1f} MB")


if __name__ == "__main__":
    main()
