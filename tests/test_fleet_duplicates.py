"""Реинсталл-дубли: свёртка на ЧТЕНИИ, без записи (2026-08-27).

Переустановка агента с потерей config.json минтит новый device_id → старая
строка того же ПК висит дублем в fleet. Безопасное решение (после того как
адверсарное ревью зарубило удаление-по-конверту): дубли НЕ пишутся и НЕ
удаляются на ingest — fleet-вид их выводит из уже прочитанных строк. При
одинаковом hostname в основном списке остаётся самая свежая запись, а
OFFLINE-тёзки постарше уезжают в свёрнутый блок «Скрытые дубликаты». Удаление
осталось только на старых безопасных путях (✕, bulk purge, 30-дневный
clock-свип). Вернувшееся устройство само выходит из блока следующим конвертом.

Эти тесты пинят чистую функцию свёртки, а также что ingest по-прежнему ничего
не удаляет (регрессия против зарубленного дизайна — в test_device_cleanup.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server.web import dashboard

pytestmark = pytest.mark.unit


def _dev(device_id, hostname, age, *, stale=None, site=None, org=None, dept=None, flags=None):
    """Мини-строка в форме обогащённого get_devices(): свёртке нужны эти поля."""
    return {
        "device_id": device_id,
        "hostname": hostname,
        "last_seen_age_sec": age,
        "stale": (age is not None and age > 600) if stale is None else stale,
        "site_code": site,
        "org_code": org,
        "dept_code": dept,
        "flags": flags or [],
    }


def _ids(rows):
    return [d["device_id"] for d in rows]


# --------------------------------------------------------------------------- #
# _split_duplicates — ядро
# --------------------------------------------------------------------------- #
def test_older_stale_twin_hidden_newest_kept():
    fresh = _dev("new", "PC-01", 30)  # на связи
    ghost = _dev("old", "PC-01", 4000)  # молчит
    live, dupes = dashboard._split_duplicates([ghost, fresh])
    assert _ids(live) == ["new"]
    assert _ids(dupes) == ["old"]


def test_both_live_nothing_hidden():
    a = _dev("a", "PC-01", 20)
    b = _dev("b", "PC-01", 40)
    live, dupes = dashboard._split_duplicates([a, b])
    assert set(_ids(live)) == {"a", "b"}
    assert dupes == []


def test_different_hostnames_nothing_hidden():
    a = _dev("a", "PC-01", 4000)
    b = _dev("b", "PC-02", 20)
    live, dupes = dashboard._split_duplicates([a, b])
    assert set(_ids(live)) == {"a", "b"}
    assert dupes == []


def test_case_and_whitespace_group_together():
    fresh = _dev("new", "pc-01", 20)
    ghost = _dev("old", " PC-01 ", 4000)
    live, dupes = dashboard._split_duplicates([ghost, fresh])
    assert _ids(live) == ["new"]
    assert _ids(dupes) == ["old"]


def test_empty_hostname_never_hidden():
    a = _dev("a", "", 4000)
    b = _dev("b", "", 20)
    c = _dev("c", None, 4000)
    live, dupes = dashboard._split_duplicates([a, b, c])
    assert set(_ids(live)) == {"a", "b", "c"}
    assert dupes == []


def test_unparseable_age_never_hidden_never_keeper():
    """last_seen_age_sec is None -> строка не участвует: не скрывается и не
    может быть keeper'ом (UNKNOWN over false confidence)."""
    unknown = _dev("u", "PC-01", None)
    ghost = _dev("old", "PC-01", 4000)
    live, dupes = dashboard._split_duplicates([unknown, ghost])
    # только одна строка с известным возрастом -> сравнивать не с чем, скрывать нечего
    assert set(_ids(live)) == {"u", "old"}
    assert dupes == []


def test_three_copies_newest_kept_two_hidden():
    fresh = _dev("new", "PC-01", 15)
    g1 = _dev("g1", "PC-01", 2000)
    g2 = _dev("g2", "PC-01", 5000)
    live, dupes = dashboard._split_duplicates([g1, fresh, g2])
    assert _ids(live) == ["new"]
    assert set(_ids(dupes)) == {"g1", "g2"}


def test_two_offline_twins_nothing_hidden():
    """Обе offline: нет живого преемника -> не доказано, что это переустановка
    (может быть один упавший ПК). Ничего не прячем (UNKNOWN over false confidence)."""
    newer = _dev("newer", "PC-01", 1000)
    older = _dev("older", "PC-01", 9000)
    live, dupes = dashboard._split_duplicates([older, newer])
    assert set(_ids(live)) == {"newer", "older"}
    assert dupes == []


def test_same_hostname_different_site_not_hidden():
    """Разные объекты с одинаковым generic-именем -- разные машины, не дубли:
    упавший ПК другого объекта не должен исчезнуть из списка (закрытие HIGH
    финального ревью)."""
    live_a = _dev("a", "KASSA", 20, site="msk", org="o1")
    broken_b = _dev("b", "KASSA", 900000, site="spb", org="o2")  # реально в отказе
    live, dupes = dashboard._split_duplicates([live_a, broken_b])
    assert set(_ids(live)) == {"a", "b"}
    assert dupes == []


def test_risky_stale_twin_never_hidden():
    """Падающую машину (сигналит о проблеме) не прячем даже как тёзку-призрака."""
    fresh = _dev("new", "PC-01", 20)
    failing = _dev("old", "PC-01", 4000, flags=["at_risk"])
    live, dupes = dashboard._split_duplicates([failing, fresh])
    assert set(_ids(live)) == {"new", "old"}
    assert dupes == []


def test_input_order_preserved():
    a = _dev("a", "H-A", 10)
    g = _dev("g", "H-B", 4000)
    b = _dev("b", "H-B", 20)
    live, _ = dashboard._split_duplicates([a, g, b])
    assert _ids(live) == ["a", "b"]  # исходный порядок сохранён


# --------------------------------------------------------------------------- #
# Рендер: свёрнутый блок в /fleet
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_fleet_page_collapses_reinstall_ghost(client: TestClient):
    from tests.conftest import envelope

    for did in ("dev-old", "dev-new"):
        body = {**envelope(did, "liveness", {}), "hostname": "PC-01"}
        assert client.post("/api/v1/ingest", json=body).status_code == 200
    # состарить старую копию за порог OFFLINE (последняя связь = приём сервера)
    from server import db

    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET last_seen=datetime('now','-2 hours') WHERE device_id='dev-old'"
        )

    html = client.get("/").text
    assert "Скрытые дубликаты" in html
    head, _, tail = html.partition('class="site dupes"')
    assert "dev-new" in head  # свежая — в основном списке
    assert "dev-old" in tail  # старая — в свёрнутом блоке
    assert "dev-old" not in head


@pytest.mark.integration
def test_fleet_fragment_has_no_dupes_block_when_unique(client: TestClient):
    from tests.conftest import envelope

    for did, host in (("d1", "PC-01"), ("d2", "PC-02")):
        body = {**envelope(did, "liveness", {}), "hostname": host}
        assert client.post("/api/v1/ingest", json=body).status_code == 200
    html = client.get("/fleet/fragment").text
    assert "Скрытые дубликаты" not in html


@pytest.mark.integration
def test_first_seen_lights_new7d_kpi(client: TestClient):
    """Регрессия: get_devices() снова отдаёт first_seen, поэтому счётчик/бейдж
    «новых ≤7д» перестал быть вечным нулём (finding финального ревью)."""
    from server import db

    from tests.conftest import envelope

    body = {**envelope("d1", "liveness", {}), "hostname": "PC-NEW"}
    assert client.post("/api/v1/ingest", json=body).status_code == 200

    rows = db.get_devices()
    assert rows and rows[0].get("first_seen")  # больше не выпадает из SELECT
    ctx = dashboard._fleet_context(rows)
    assert ctx["summary"]["new7d"] >= 1
