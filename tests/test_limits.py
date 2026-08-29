"""Тесты server/limits.py + db.count_devices/device_known (Задача 1 плана публичного релиза)."""

import importlib


def test_limits_defaults(monkeypatch):
    monkeypatch.delenv("SRP_DEMO", raising=False)
    from server import limits

    importlib.reload(limits)
    assert limits.MAX_DEVICES == 3
    assert limits.DEMO_MODE is False


def test_demo_mode_env(monkeypatch):
    monkeypatch.setenv("SRP_DEMO", "1")
    from server import limits

    importlib.reload(limits)
    assert limits.DEMO_MODE is True

    # не оставлять DEMO_MODE=True другим тестам: сначала снять env, потом reload
    monkeypatch.delenv("SRP_DEMO", raising=False)
    importlib.reload(limits)
    assert limits.DEMO_MODE is False


def test_count_and_known(client):
    from server import db

    assert db.count_devices() == 0
    assert db.device_known("nope") is False
    db.touch_device("dev-1", ts="2026-08-01T00:00:00Z", agent_version="0.3.0")
    assert db.count_devices() == 1
    assert db.device_known("dev-1") is True


# --------------------------------------------------------------------------- #
# Задача 6: лимит устройств в /ingest + плашка на дашборде
# --------------------------------------------------------------------------- #
def _hb(device_id: str) -> dict:
    return {
        "device_id": device_id,
        "msg_type": "heartbeat",
        "payload": {"cpu_pct": 5.0},
        "agent_version": "0.1.0",
    }


def test_fourth_device_rejected(client, monkeypatch):
    monkeypatch.setattr("server.limits.MAX_DEVICES", 3)
    for i in range(3):
        assert client.post("/api/v1/ingest", json=_hb(f"lim-{i}")).status_code == 200
    r = client.post("/api/v1/ingest", json=_hb("lim-3"))
    assert r.status_code == 403
    assert "до 3" in r.json()["detail"]
    # известный ходит свободно, даже когда лимит уже исчерпан
    assert client.post("/api/v1/ingest", json=_hb("lim-0")).status_code == 200


def test_delete_frees_slot(client, monkeypatch):
    monkeypatch.setattr("server.limits.MAX_DEVICES", 3)
    for i in range(3):
        client.post("/api/v1/ingest", json=_hb(f"s-{i}"))
    # admin_token/ingest_token пусты в client-фикстуре -> _require_admin_token
    # выходит рано, заголовки не нужны (constraints.md §2).
    assert client.post("/api/v1/devices/s-0/delete").status_code == 200
    assert client.post("/api/v1/ingest", json=_hb("s-new")).status_code == 200


def test_free_edition_limit_is_three(client):
    """Бесплатная версия принимает ровно 3 компьютера — то, что обещает README."""
    for i in range(3):
        assert client.post("/api/v1/ingest", json=_hb(f"free-{i}")).status_code == 200
    assert client.post("/api/v1/ingest", json=_hb("free-3")).status_code == 403


def test_fleet_shows_limit_note(client, monkeypatch):
    monkeypatch.setattr("server.limits.MAX_DEVICES", 3)
    for i in range(3):
        client.post("/api/v1/ingest", json=_hb(f"n-{i}"))
    page = client.get("/").text
    assert "лимит" in page.lower()
    # ревью-finding 1: плашка обязана печатать те же числа, что guard считает
    # (db.count_devices() == 3, лимит == 3), не де-дуплицированный summary.total
    assert "3/3" in page


def test_fleet_hides_limit_note_below_limit(client, monkeypatch):
    monkeypatch.setattr("server.limits.MAX_DEVICES", 3)
    client.post("/api/v1/ingest", json=_hb("below-0"))
    r = client.get("/")
    assert r.status_code == 200
    assert "below-0" in r.text  # страница реально отрисовала устройство, не пустышка
    assert "лимит" not in r.text.lower()


def test_fleet_hides_limit_note_in_demo_mode(client, monkeypatch):
    """Ruling controller #1: демо сеет 10 машин на лимите 3 -- плашка «10/3»
    бессмысленна и пугающа на витрине, поэтому в демо-режиме её не должно быть,
    даже когда total >= device_limit."""
    from server import db, limits

    monkeypatch.setattr(limits, "MAX_DEVICES", 3)
    monkeypatch.setattr(limits, "DEMO_MODE", True)
    for i in range(3):
        db.touch_device(f"demo-{i}", ts="2026-08-01T00:00:00Z", agent_version="0.3.0")
    r = client.get("/")
    assert r.status_code == 200
    assert "demo-0" in r.text  # страница реально отрисовала устройства, не пустышка
    assert "лимит" not in r.text.lower()


def test_fleet_banner_matches_guard_count_with_reinstall_ghost(client, monkeypatch):
    """Review finding 1: cap достигнут ТОЛЬКО за счёт призрака переустановки
    (тот же ПК под новым device_id после потери config.json). guard считает
    db.count_devices() (СЫРЫЕ строки) -- 2 == MAX_DEVICES, значит третье
    устройство уже отклоняется. Де-дуплицированный fleet-список видит только
    1 живую машину (summary.total), поэтому плашка на старом коде молчала бы
    ровно тогда, когда владельца реально отказывают -- худший случай, хуже
    отсутствия плашки вообще."""
    from server import db

    monkeypatch.setattr("server.limits.MAX_DEVICES", 2)
    for did in ("dev-old", "dev-new"):
        body = {**_hb(did), "hostname": "PC-1"}
        assert client.post("/api/v1/ingest", json=body).status_code == 200
    # состарить старую копию за порог OFFLINE -> сворачивается в «Скрытые дубликаты»
    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET last_seen=datetime('now','-2 hours') WHERE device_id='dev-old'"
        )

    assert db.count_devices() == 2  # то самое число, что видит guard
    r = client.post("/api/v1/ingest", json=_hb("dev-third"))
    assert r.status_code == 403  # guard уже отказывает -- значит и плашка обязана появиться

    page = client.get("/").text
    assert "лимит" in page.lower()
    assert "2/2" in page  # numerator == db.count_devices(), НЕ summary.total (который был бы 1)
    assert "Скрытые дубликаты" in page  # ghost действительно свёрнут, а не просто offline
    assert "переустановленных агентов" in page  # пояснение про занятые слоты показано


def test_device_id_case_is_not_normalized(client, monkeypatch):
    """Security-analysis finding, не защита: device_id -- TEXT PRIMARY KEY без
    COLLATE NOCASE и без strip/lower в Envelope, так что 'dev-x' и 'DEV-X' -- две
    РАЗНЫЕ строки БД. Они не дают обойти лимит (каждая всё равно тратит слот),
    но одна и та же физическая машина, отчитывающаяся под разным регистром,
    может съесть несколько слотов лимита. Тест документирует факт, а не чинит его."""
    monkeypatch.setattr("server.limits.MAX_DEVICES", 3)
    assert client.post("/api/v1/ingest", json=_hb("dev-x")).status_code == 200
    assert client.post("/api/v1/ingest", json=_hb("DEV-X")).status_code == 200
    from server import db

    assert db.count_devices() == 2
