"""Минимальный рост БД (спека 2026-08-27-db-minimal-growth-design.md).

Три механизма + разовая миграция:
* Ш1 -- компактная сериализация JSON при записи (ensure_ascii=False, без пробелов);
* Ш2 -- история scores глубже новейших K строк ужимается до slim-вердикта,
  сохраняющего РОВНО те поля, что читает хоть один потребитель истории
  (health целиком, score100.storage_risk.band/coords.flags, errchain.stage);
* Ш3 -- возрастной срез scores в prune_aged (scores_raw_days);
* Ш5 -- printer_readings.detail глубже новейших K на принтер -> NULL
  (историю detail не читает никто: оба читателя берут LIMIT 1);
* server.shrink -- разовая идемпотентная миграция существующих строк + VACUUM.

Pure SQLite; no network, no FastAPI.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _full_risk(idx: float = 77.0) -> dict:
    """Полный вердикт: и «тяжёлые» ключи (русская проза), и всё, что читает история."""
    return {
        "classes": [{"name": "power_thermal", "label": "Питание / перегрев", "prob": 0.4}],
        "domains": {"storage": {"reasons": ["Диск деградирует", "Ошибки чтения"]}},
        "day1_factors": [{"factor": "возраст диска", "weight": 0.3}],
        "trajectory": {"performance": {"slope": -0.1}},
        "top": [["storage", 0.5]],
        "overall": 0.4,
        "device_trust": 0.9,
        "health": {
            "index": idx,
            "band": "watch",
            "state": "degrading",
            "dominant": "damage",
            "delta_7d": -2.5,
            "damage": {"value": 30.0, "band": "watch"},
            "resilience": {"value": 60.0},
            "observability": {"value": 0.8},
        },
        "score100": {
            "storage_risk": {
                "band": "bad",
                "coords": {"flags": ["pending_gt10", "recurrence"]},
                "source_lineage": {"worst_disk": "hashabc"},
            },
            "cpu_perf": {"band": "good"},
        },
        "errchain": {"stage": 2, "events": ["whea", "kp41"]},
    }


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _stored_risk(db, device_id: str) -> list[str]:
    """Сырые strings колонки risk, oldest-first."""
    with sqlite3.connect(db._db_path) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT risk FROM scores WHERE device_id=? ORDER BY id ASC", (device_id,)
            )
        ]


# --------------------------------------------------------------------------- #
# Ш1: компактная сериализация при записи
# --------------------------------------------------------------------------- #


def test_scores_risk_stored_without_ascii_escapes_and_spaces(db_init):
    db = db_init
    db.store_scores("dev-1", _ts(0), {"performance": 90.0, "risk": _full_risk()})
    raw = _stored_risk(db, "dev-1")[0]
    assert "\\u04" not in raw  # кириллица хранится как есть, не с...
    assert "Питание" in raw
    assert '", "' not in raw and '": ' not in raw  # компактные separators


def test_historical_payload_stored_compact(db_init):
    db = db_init
    db.store_historical("dev-1", _ts(0), {"note": "Диск деградирует", "v": 1})
    with sqlite3.connect(db._db_path) as conn:
        raw = conn.execute("SELECT payload FROM historical").fetchone()[0]
    assert "\\u04" not in raw and '": ' not in raw
    assert json.loads(raw)["note"] == "Диск деградирует"


def test_printer_detail_stored_compact(db_init):
    db = db_init
    db.store_printer_reading("prn-1", {"ip": "192.168.1.9", "status": "ok", "note": "тонер"})
    with sqlite3.connect(db._db_path) as conn:
        raw = conn.execute("SELECT detail FROM printer_readings").fetchone()[0]
    assert "\\u04" not in raw and '": ' not in raw


def test_topology_snapshot_stored_compact(db_init):
    db = db_init
    db.store_topology_snapshot({"nodes": [{"id": "n1", "label": "Свитч"}], "links": []})
    with sqlite3.connect(db._db_path) as conn:
        raw = conn.execute("SELECT graph FROM net_topology_snapshots").fetchone()[0]
    assert "\\u04" not in raw and '": ' not in raw


# --------------------------------------------------------------------------- #
# Ш2: slim-история scores
# --------------------------------------------------------------------------- #


def test_scores_beyond_keep_full_become_slim(db_init):
    db = db_init
    n = db._SCORES_KEEP_FULL + 3
    for i in range(n):
        db.store_scores("dev-1", _ts(n - i), {"performance": 90.0, "risk": _full_risk(70.0 + i)})
    raws = _stored_risk(db, "dev-1")
    slim, full = raws[: n - db._SCORES_KEEP_FULL], raws[n - db._SCORES_KEEP_FULL :]
    for raw in slim:
        j = json.loads(raw)
        assert j.get("slim") == 1
        assert "classes" not in j and "domains" not in j and "day1_factors" not in j
    for raw in full:
        assert json.loads(raw).get("slim") is None
        assert "classes" in json.loads(raw)


def test_slim_preserves_every_history_consumer_field(db_init):
    db = db_init
    n = db._SCORES_KEEP_FULL + 2
    for i in range(n):
        db.store_scores("dev-1", _ts(n - i), {"performance": 90.0, "risk": _full_risk(70.0 + i)})
    oldest = json.loads(_stored_risk(db, "dev-1")[0])
    assert oldest["slim"] == 1
    # sparklines + delta-7d + fleet-deltas: блок health целиком
    assert oldest["health"]["index"] == 70.0
    assert oldest["health"]["state"] == "degrading"
    assert oldest["health"]["band"] == "watch"
    assert oldest["health"]["damage"]["value"] == 30.0
    # rulestats: storage_risk band + coords.flags, errchain.stage
    assert oldest["score100"]["storage_risk"]["band"] == "bad"
    assert oldest["score100"]["storage_risk"]["coords"]["flags"] == [
        "pending_gt10",
        "recurrence",
    ]
    assert oldest["errchain"]["stage"] == 2


def test_slim_rows_visible_to_series_and_sql_json_extract(db_init):
    db = db_init
    n = db._SCORES_KEEP_FULL + 2
    for i in range(n):
        db.store_scores("dev-1", _ts(n - i), {"performance": 90.0, "risk": _full_risk(70.0 + i)})
    series = db.get_score_series("dev-1", limit=n)
    assert len(series) == n
    assert all(((r["risk"] or {}).get("health") or {}).get("index") is not None for r in series)
    with sqlite3.connect(db._db_path) as conn:
        states = [
            r[0]
            for r in conn.execute(
                "SELECT json_extract(risk,'$.health.state') FROM scores WHERE device_id=?",
                ("dev-1",),
            )
        ]
    assert states == ["degrading"] * n


def test_latest_rows_keep_full_verdict(db_init):
    db = db_init
    for i in range(db._SCORES_KEEP_FULL + 5):
        db.store_scores("dev-1", _ts(20 - i), {"performance": 90.0, "risk": _full_risk()})
    latest = db.get_score_series("dev-1", limit=1)[0]
    assert "classes" in latest["risk"]  # карточка устройства получает полный вердикт


def test_store_scores_survives_malformed_and_nondict_risk_rows(db_init):
    """Ревью 2026-08-27 (HIGH): sqlite json_extract БРОСАЕТ OperationalError на
    невалидном JSON (не возвращает NULL), а _slim_risk падал AttributeError на
    valid-JSON-не-dict. Одна битая строка не должна навсегда останавливать
    скоринг устройства."""
    db = db_init
    with sqlite3.connect(db._db_path) as conn:
        conn.execute(
            "INSERT INTO scores (device_id, ts, risk) VALUES (?,?,?)",
            ("dev-1", _ts(30), "not json {"),
        )
        conn.execute(
            "INSERT INTO scores (device_id, ts, risk) VALUES (?,?,?)",
            ("dev-1", _ts(29), "null"),
        )
        conn.execute(
            "INSERT INTO scores (device_id, ts, risk) VALUES (?,?,?)",
            ("dev-1", _ts(28), json.dumps({"errchain": ["not", "a", "dict"], "health": {}})),
        )
    for i in range(db._SCORES_KEEP_FULL + 2):
        db.store_scores("dev-1", _ts(20 - i), {"performance": 90.0, "risk": _full_risk()})
    # все свежие вердикты записались, битые строки остались инертными
    assert len(db.get_score_series("dev-1", limit=50)) == db._SCORES_KEEP_FULL + 5


def test_store_scores_slims_only_boundary_row_backlog_left_to_shrink(db_init):
    """Ревью 2026-08-27 (HIGH): инкрементальное ужатие -- O(1) на вставку
    (строка на границе K), а не скан всей истории под глобальным локом;
    массовый бэклог -- забота server.shrink."""
    db = db_init
    with sqlite3.connect(db._db_path) as conn:
        for i in range(db._SCORES_KEEP_FULL + 5):
            conn.execute(
                "INSERT INTO scores (device_id, ts, risk) VALUES (?,?,?)",
                ("dev-1", _ts(40 - i), json.dumps(_full_risk())),
            )
    db.store_scores("dev-1", _ts(1), {"performance": 90.0, "risk": _full_risk()})
    raws = _stored_risk(db, "dev-1")
    slim_flags = [json.loads(r).get("slim") == 1 for r in raws]
    assert slim_flags.count(True) == 1  # ровно граничная строка
    assert slim_flags[-(db._SCORES_KEEP_FULL + 1)] is True


def test_scores_device_below_keep_full_untouched(db_init):
    db = db_init
    for i in range(3):
        db.store_scores("dev-1", _ts(3 - i), {"performance": 90.0, "risk": _full_risk()})
    assert all(json.loads(r).get("slim") is None for r in _stored_risk(db, "dev-1"))


# --------------------------------------------------------------------------- #
# Ш3: возрастной срез scores в prune_aged
# --------------------------------------------------------------------------- #


def test_prune_aged_scores_leg_deletes_old_keeps_young(db_init):
    db = db_init
    db.store_scores("dev-1", _ts(200), {"performance": 90.0, "risk": _full_risk()})
    db.store_scores("dev-1", _ts(1), {"performance": 90.0, "risk": _full_risk()})
    deleted = db.prune_aged(
        heartbeat_raw_days=0, events_raw_days=0, rollup_days=0, scores_raw_days=120
    )
    assert deleted["scores"] == 1
    assert len(db.get_score_series("dev-1", limit=10)) == 1


def test_prune_aged_scores_keeps_latest_row_per_device(db_init):
    """Даже очень старый вердикт не удаляется, если он ПОСЛЕДНИЙ у устройства:
    карточка живого устройства со сломанным rescore не должна опустеть."""
    db = db_init
    db.store_scores("dev-old", _ts(300), {"performance": 90.0, "risk": _full_risk()})
    deleted = db.prune_aged(
        heartbeat_raw_days=0, events_raw_days=0, rollup_days=0, scores_raw_days=120
    )
    assert deleted["scores"] == 0
    assert len(db.get_score_series("dev-old", limit=10)) == 1


def test_prune_aged_scores_leg_zero_disables(db_init):
    db = db_init
    db.store_scores("dev-1", _ts(400), {"performance": 90.0, "risk": _full_risk()})
    deleted = db.prune_aged(
        heartbeat_raw_days=0, events_raw_days=0, rollup_days=0, scores_raw_days=0
    )
    assert "scores" not in deleted
    assert len(db.get_score_series("dev-1", limit=10)) == 1


def test_prune_aged_historical_leg_keeps_latest_and_young(db_init):
    """Возрастной срез historical: старьё удаляется, свежие строки и ПОСЛЕДНЯЯ
    строка каждого устройства (карточка) -- нет."""
    db = db_init
    db.store_historical("dev-1", _ts(400), {"v": 1}, received_at=_ts(400))
    db.store_historical("dev-1", _ts(300), {"v": 2}, received_at=_ts(300))
    db.store_historical("dev-1", _ts(1), {"v": 3}, received_at=_ts(1))
    db.store_historical("dev-quiet", _ts(400), {"v": 4}, received_at=_ts(400))
    deleted = db.prune_aged(
        heartbeat_raw_days=0, events_raw_days=0, rollup_days=0, historical_raw_days=180
    )
    assert deleted["historical"] == 2  # два старых dev-1; последняя dev-quiet цела
    assert len(db.get_historical_series("dev-1", limit=10)) == 1
    assert len(db.get_historical_series("dev-quiet", limit=10)) == 1


def test_prune_aged_historical_leg_zero_disables(db_init):
    db = db_init
    db.store_historical("dev-1", _ts(400), {"v": 1}, received_at=_ts(400))
    db.store_historical("dev-1", _ts(399), {"v": 2}, received_at=_ts(399))
    deleted = db.prune_aged(heartbeat_raw_days=0, events_raw_days=0, rollup_days=0)
    assert "historical" not in deleted
    assert len(db.get_historical_series("dev-1", limit=10)) == 2


# --------------------------------------------------------------------------- #
# Ш4: historical retain по умолчанию 500
# --------------------------------------------------------------------------- #


def test_historical_default_retain_is_500():
    import inspect

    from server import db

    assert inspect.signature(db.init_db).parameters["retain_historical"].default == 500


def test_config_passes_retain_historical_and_scores_days(tmp_path):
    from server.config import ServerConfig

    cfg = ServerConfig()
    assert cfg.retain_historical == 500
    assert cfg.scores_raw_days == 120
    assert cfg.historical_raw_days == 180


# --------------------------------------------------------------------------- #
# Ш5: printer_readings.detail глубже новейших K -> NULL
# --------------------------------------------------------------------------- #


def test_printer_detail_nulled_beyond_keep(db_init):
    db = db_init
    n = db._PRINTER_DETAIL_KEEP + 2
    for i in range(n):
        db.store_printer_reading(
            "prn-1", {"ip": "192.168.1.9", "status": "ok", "total_pages": 1000 + i, "extra": "x"}
        )
    with sqlite3.connect(db._db_path) as conn:
        rows = conn.execute(
            "SELECT detail, total_pages FROM printer_readings ORDER BY id ASC"
        ).fetchall()
    assert len(rows) == n
    for detail, pages in rows[:2]:
        assert detail is None
        assert pages is not None  # скаляры (графики страниц) не тронуты
    for detail, _pages in rows[2:]:
        assert detail is not None
    # читатель "detail последней строки" жив
    with sqlite3.connect(db._db_path) as conn:
        last = conn.execute(
            "SELECT detail FROM printer_readings ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert json.loads(last)["total_pages"] == 1000 + n - 1


# --------------------------------------------------------------------------- #
# server.shrink: разовая миграция существующих строк, идемпотентная
# --------------------------------------------------------------------------- #


def _legacy_bloat(db) -> None:
    """Строки «как раньше»: ensure_ascii, пробелы, полный вердикт по всей истории."""
    with sqlite3.connect(db._db_path) as conn:
        for i in range(db._SCORES_KEEP_FULL + 4):
            conn.execute(
                "INSERT INTO scores (device_id, ts, performance, risk) VALUES (?,?,?,?)",
                ("dev-legacy", _ts(30 - i), 90.0, json.dumps(_full_risk(50.0 + i))),
            )
        for i in range(db._PRINTER_DETAIL_KEEP + 3):
            conn.execute(
                "INSERT INTO printer_readings (printer_id, received_at, total_pages, detail)"
                " VALUES (?,?,?,?)",
                ("prn-legacy", _ts(10 - i), 500 + i, json.dumps({"note": "тонер", "i": i})),
            )
        conn.execute(
            "INSERT INTO historical (device_id, ts, payload, received_at) VALUES (?,?,?,?)",
            ("dev-legacy", _ts(5), json.dumps({"note": "Диск деградирует"}), _ts(5)),
        )
        conn.commit()


def test_shrink_migrates_and_is_idempotent(db_init):
    from server import shrink

    db = db_init
    _legacy_bloat(db)
    before = sum(len(r) for r in _stored_risk(db, "dev-legacy"))

    first = shrink.run(db._db_path, vacuum=False)
    raws = _stored_risk(db, "dev-legacy")
    after_first = sum(len(r) for r in raws)
    assert after_first < before  # компакт+slim суммарно ужали хранение
    for raw in raws[:4]:  # slim-строки в разы меньше полного вердикта
        assert len(raw) < len(raws[-1]) / 2
    assert first["scores_slimmed"] == 4
    assert first["scores_rewritten"] >= db._SCORES_KEEP_FULL  # компакт свежих полных
    assert first["printer_detail_nulled"] == 3
    assert first["historical_rewritten"] == 1

    second = shrink.run(db._db_path, vacuum=False)
    assert second["scores_slimmed"] == 0
    assert second["scores_rewritten"] == 0
    assert second["printer_detail_nulled"] == 0
    assert second["historical_rewritten"] == 0


def test_shrink_skips_poisoned_rows_counts_them_and_continues(db_init):
    """Ревью 2026-08-27: битая/не-dict строка не валит миграцию (AttributeError
    раньше выходил мимо except и намертво клинил повторные прогоны), а полный
    провал отличим от «уже мигрировано» счётчиком skipped."""
    from server import shrink

    db = db_init
    _legacy_bloat(db)
    with sqlite3.connect(db._db_path) as conn:
        conn.execute("UPDATE scores SET risk='not json {' WHERE id=(SELECT MIN(id) FROM scores)")
        conn.execute("UPDATE scores SET risk='null' WHERE id=(SELECT MIN(id)+1 FROM scores)")
    stats = shrink.run(db._db_path, vacuum=False)
    assert stats["scores_skipped"] == 2
    assert stats["scores_slimmed"] == 2  # остальные старые строки ужаты
    raws = _stored_risk(db, "dev-legacy")
    assert raws[0] == "not json {" and raws[1] == "null"  # инертны, не потеряны


def test_shrink_keeps_consumer_fields_and_latest_full(db_init):
    from server import shrink

    db = db_init
    _legacy_bloat(db)
    shrink.run(db._db_path, vacuum=False)
    oldest = json.loads(_stored_risk(db, "dev-legacy")[0])
    assert oldest["slim"] == 1
    assert oldest["health"]["index"] == 50.0
    assert oldest["score100"]["storage_risk"]["coords"]["flags"]
    assert oldest["errchain"]["stage"] == 2
    latest = json.loads(_stored_risk(db, "dev-legacy")[-1])
    assert "classes" in latest and "\\u04" not in _stored_risk(db, "dev-legacy")[-1]
