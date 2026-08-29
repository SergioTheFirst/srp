"""o5 block F (F1-F7, server side only): owner_full_name/owner_position/owner_phone.

Mirrors the D6 operator-priority mechanism (comment/comment_operator) exactly:
agent writes the plain column via COALESCE-on-write; a dashboard edit lands in
the sibling *_operator column and wins on READ until cleared (empty string ->
NULL hands control back to the agent). Tray/agent/client/dashboard pieces
(F8-F13) are out of scope for this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import server.db as db
from fastapi.testclient import TestClient
from pydantic import ValidationError
from server.pipeline import ingest_envelope
from shared.schema import Envelope

from tests.conftest import envelope

pytestmark = pytest.mark.unit


def _seed(tmp_path: Path) -> Path:
    p = tmp_path / "srp.db"
    db.init_db(p)
    return p


# --------------------------------------------------------------------------- #
# F1 -- contract: three fields on Envelope
# --------------------------------------------------------------------------- #


def test_envelope_owner_fields_default_none() -> None:
    env = Envelope(device_id="dev-1", msg_type="heartbeat", payload={})
    assert env.owner_full_name is None
    assert env.owner_position is None
    assert env.owner_phone is None


def test_envelope_owner_fields_accepted_when_filled() -> None:
    env = Envelope(
        device_id="dev-1",
        msg_type="heartbeat",
        payload={},
        owner_full_name="Иванов Иван Иванович",
        owner_position="инженер",
        owner_phone="+7 900 000-00-00",
    )
    assert env.owner_full_name == "Иванов Иван Иванович"
    assert env.owner_position == "инженер"
    assert env.owner_phone == "+7 900 000-00-00"


def test_envelope_owner_full_name_over_140_rejected() -> None:
    with pytest.raises(ValidationError):
        Envelope(device_id="dev-1", msg_type="heartbeat", payload={}, owner_full_name="и" * 141)


def test_envelope_owner_full_name_at_140_accepted() -> None:
    env = Envelope(device_id="dev-1", msg_type="heartbeat", payload={}, owner_full_name="и" * 140)
    assert env.owner_full_name is not None and len(env.owner_full_name) == 140


# --------------------------------------------------------------------------- #
# F2 -- db: six columns + idempotent migration
# --------------------------------------------------------------------------- #

_OWNER_COLUMNS = (
    "owner_full_name",
    "owner_position",
    "owner_phone",
    "owner_full_name_operator",
    "owner_position_operator",
    "owner_phone_operator",
)


def test_devices_table_has_owner_columns(tmp_path: Path) -> None:
    _seed(tmp_path)
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)").fetchall()}
    for col in _OWNER_COLUMNS:
        assert col in cols


def test_init_db_idempotent_over_existing_owner_columns(tmp_path: Path) -> None:
    p = _seed(tmp_path)
    db.init_db(p)  # second startup over the same file must not raise


# --------------------------------------------------------------------------- #
# F3 -- db: write with operator priority (D6 mechanism)
# --------------------------------------------------------------------------- #


def test_upsert_device_saves_owner_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device(
        "dev-f3a",
        "2026-03-01T10:00:00+00:00",
        "0.1.0",
        owner_full_name="Иванов И.И.",
        owner_position="инженер",
        owner_phone="+7 900 000-00-00",
    )
    dev = db.get_device("dev-f3a")
    assert dev is not None
    assert dev["owner_full_name"] == "Иванов И.И."
    assert dev["owner_position"] == "инженер"
    assert dev["owner_phone"] == "+7 900 000-00-00"


def test_upsert_device_none_does_not_wipe_owner_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device("dev-f3b", "2026-03-01T10:00:00+00:00", "0.1.0", owner_full_name="Иванов И.И.")
    db.upsert_device("dev-f3b", "2026-03-01T11:00:00+00:00", "0.1.0")  # owner_* omitted -> None
    dev = db.get_device("dev-f3b")
    assert dev is not None and dev["owner_full_name"] == "Иванов И.И."


def test_touch_device_updates_owner_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.touch_device("dev-f3c", "2026-03-01T10:00:00+00:00", "0.1.0", owner_full_name="Петров П.П.")
    dev = db.get_device("dev-f3c")
    assert dev is not None and dev["owner_full_name"] == "Петров П.П."


def test_operator_owner_full_name_survives_agent_envelope(tmp_path: Path) -> None:
    """Ровно тест test_operator_comment_survives_agent_envelope (D6), но для
    owner_full_name: правка оператора побеждает навсегда, пока не очищена."""
    _seed(tmp_path)
    did = "dev-f3d"
    db.touch_device(did, "2026-03-01T10:00:00+00:00", "0.1.0", owner_full_name="from-agent")
    dev = db.get_device(did)
    assert dev is not None and dev["owner_full_name"] == "from-agent"

    assert db.set_device_owner_full_name(did, "from-operator") is True
    db.touch_device(did, "2026-03-01T11:00:00+00:00", "0.1.0", owner_full_name="from-agent")
    dev = db.get_device(did)
    assert dev is not None
    assert dev["owner_full_name"] == "from-operator", "агент затёр правку оператора"


# --------------------------------------------------------------------------- #
# F4 -- db: read (get_devices / get_device)
# --------------------------------------------------------------------------- #


def test_get_devices_returns_owner_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device(
        "dev-f4a",
        "2026-03-01T10:00:00+00:00",
        "0.1.0",
        owner_full_name="Иванов И.И.",
        owner_position="инженер",
        owner_phone="+7 900 000-00-00",
    )
    row = next(d for d in db.get_devices() if d["device_id"] == "dev-f4a")
    assert row["owner_full_name"] == "Иванов И.И."
    assert row["owner_position"] == "инженер"
    assert row["owner_phone"] == "+7 900 000-00-00"


def test_get_device_returns_owner_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device("dev-f4b", "2026-03-01T10:00:00+00:00", "0.1.0", owner_full_name="Петров П.П.")
    dev = db.get_device("dev-f4b")
    assert dev is not None
    assert dev["owner_full_name"] == "Петров П.П."
    assert dev["owner_position"] is None
    assert dev["owner_phone"] is None


def test_owner_fields_none_for_device_without_data(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device("dev-f4c", "2026-03-01T10:00:00+00:00", "0.1.0")
    row = next(d for d in db.get_devices() if d["device_id"] == "dev-f4c")
    assert row["owner_full_name"] is None
    assert row["owner_position"] is None
    assert row["owner_phone"] is None
    dev = db.get_device("dev-f4c")
    assert dev is not None
    assert dev["owner_full_name"] is None
    assert dev["owner_position"] is None
    assert dev["owner_phone"] is None


def test_get_devices_and_get_device_prefer_operator_value(tmp_path: Path) -> None:
    _seed(tmp_path)
    did = "dev-f4d"
    db.upsert_device(did, "2026-03-01T10:00:00+00:00", "0.1.0", owner_full_name="from-agent")
    assert db.set_device_owner_full_name(did, "from-operator") is True
    row = next(d for d in db.get_devices() if d["device_id"] == did)
    assert row["owner_full_name"] == "from-operator"
    dev = db.get_device(did)
    assert dev is not None and dev["owner_full_name"] == "from-operator"


# --------------------------------------------------------------------------- #
# F5 -- db: dashboard setters
# --------------------------------------------------------------------------- #

_SETTER_FIELDS = ("owner_full_name", "owner_position", "owner_phone")


@pytest.mark.parametrize("field", _SETTER_FIELDS)
def test_setter_writes_value(tmp_path: Path, field: str) -> None:
    _seed(tmp_path)
    did = f"dev-f5a-{field}"
    db.upsert_device(did, "2026-03-01T10:00:00+00:00", "0.1.0")
    setter = getattr(db, f"set_device_{field}")
    assert setter(did, "Сидоров С.С.") is True
    dev = db.get_device(did)
    assert dev is not None and dev[field] == "Сидоров С.С."


@pytest.mark.parametrize("field", _SETTER_FIELDS)
def test_setter_empty_string_deletes_personal_data(tmp_path: Path, field: str) -> None:
    """Ревью блока F (MEDIUM): у ПДн очистка обязана УДАЛЯТЬ, а не «возвращать
    управление агенту». Иначе значение неудаляемо: агент шлёт None, когда спул
    пуст, а COALESCE держит старое навсегда — для персональных данных недопустимо.
    Отклонение от чистого D6 сознательное и касается только owner_*-полей.
    Данные вернутся, только если они всё ещё есть в источнике (спул на машине)."""
    _seed(tmp_path)
    did = f"dev-f5b-{field}"
    db.touch_device(did, "2026-03-01T10:00:00+00:00", "0.1.0", **{field: "from-agent"})
    setter = getattr(db, f"set_device_{field}")
    setter(did, "from-operator")

    setter(did, "")  # очистка = удаление ПДн из карточки
    dev = db.get_device(did)
    assert dev is not None and dev[field] is None

    # Агент снова управляет полем: следующий конверт восстанавливает значение.
    db.touch_device(did, "2026-03-01T11:00:00+00:00", "0.1.0", **{field: "from-agent"})
    dev = db.get_device(did)
    assert dev is not None and dev[field] == "from-agent"


@pytest.mark.parametrize("field", _SETTER_FIELDS)
def test_setter_missing_device_returns_false(tmp_path: Path, field: str) -> None:
    _seed(tmp_path)
    setter = getattr(db, f"set_device_{field}")
    assert setter("no-such-device", "x") is False


# --------------------------------------------------------------------------- #
# F6 -- ingest: propagate into all seven msg_type branches
# --------------------------------------------------------------------------- #

_MIN_PAYLOAD: dict[str, dict[str, Any]] = {
    "inventory": {},
    "historical": {},
    "heartbeat": {},
    "events": {},
    "print_jobs": {},
    "liveness": {},
    "update_status": {"state": "ok"},
}


@pytest.mark.parametrize("msg_type", sorted(_MIN_PAYLOAD))
def test_ingest_envelope_carries_owner_fields_for_every_msg_type(
    tmp_path: Path, msg_type: str
) -> None:
    _seed(tmp_path)
    did = f"dev-f6-{msg_type}"
    env = Envelope(
        device_id=did,
        msg_type=msg_type,
        payload=_MIN_PAYLOAD[msg_type],
        owner_full_name="Иванов И.И.",
        owner_position="инженер",
        owner_phone="+7 900 000-00-00",
    )
    ingest_envelope(env)
    dev = db.get_device(did)
    assert dev is not None
    assert dev["owner_full_name"] == "Иванов И.И."
    assert dev["owner_position"] == "инженер"
    assert dev["owner_phone"] == "+7 900 000-00-00"


def test_ingest_envelope_owner_none_does_not_wipe(tmp_path: Path) -> None:
    _seed(tmp_path)
    did = "dev-f6-keep"
    ingest_envelope(
        Envelope(device_id=did, msg_type="heartbeat", payload={}, owner_full_name="Иванов И.И.")
    )
    ingest_envelope(Envelope(device_id=did, msg_type="heartbeat", payload={}))
    dev = db.get_device(did)
    assert dev is not None and dev["owner_full_name"] == "Иванов И.И."


# --------------------------------------------------------------------------- #
# F7 -- API: MetaPatch extension
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_patch_meta_owner_fields_stored_and_read_back(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-f7a", "heartbeat", {}))
    r = client.patch(
        "/api/v1/devices/dev-f7a/meta",
        json={
            "owner_full_name": "Иванов И.И.",
            "owner_position": "инженер",
            "owner_phone": "+7 900 000-00-00",
        },
    )
    assert r.status_code == 200
    dev = client.get("/api/v1/devices/dev-f7a").json()
    assert dev["owner_full_name"] == "Иванов И.И."
    assert dev["owner_position"] == "инженер"
    assert dev["owner_phone"] == "+7 900 000-00-00"


@pytest.mark.integration
def test_patch_meta_owner_full_name_over_140_rejected(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-f7b", "heartbeat", {}))
    r = client.patch("/api/v1/devices/dev-f7b/meta", json={"owner_full_name": "и" * 141})
    assert r.status_code == 422


@pytest.mark.integration
def test_patch_meta_owner_full_name_empty_clears(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-f7c", "heartbeat", {}))
    client.patch("/api/v1/devices/dev-f7c/meta", json={"owner_full_name": "Иванов И.И."})
    client.patch("/api/v1/devices/dev-f7c/meta", json={"owner_full_name": ""})
    dev = client.get("/api/v1/devices/dev-f7c").json()
    assert dev["owner_full_name"] is None


@pytest.mark.integration
def test_patch_meta_owner_fields_missing_device_404(client: TestClient) -> None:
    r = client.patch("/api/v1/devices/nope/meta", json={"owner_full_name": "x"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# F8 — трей: спул персональных данных                                         #
# --------------------------------------------------------------------------- #


def test_validate_owner_fields_accepts_valid_and_empty() -> None:
    from client.tray.owner_spool import validate_owner_fields

    assert validate_owner_fields("Иванов И.И.", "инженер", "+7 900 000-00-00") is None
    assert validate_owner_fields("", "", "") is None


def test_validate_owner_fields_reports_each_limit() -> None:
    from client.tray.owner_spool import (
        OWNER_PHONE_MAX,
        OWNER_POSITION_MAX,
        validate_owner_fields,
    )

    err = validate_owner_fields("я" * 141, "", "")
    assert err and "140" in err and "ФИО" in err
    err = validate_owner_fields("", "д" * (OWNER_POSITION_MAX + 1), "")
    assert err and "Должность" in err
    err = validate_owner_fields("", "", "7" * (OWNER_PHONE_MAX + 1))
    assert err and "Телефон" in err


def test_write_owner_spool_is_atomic_and_leaves_no_tmp(tmp_path) -> None:
    import json

    from client.tray.owner_spool import owner_spool_path, write_owner_spool

    assert write_owner_spool(tmp_path, "ivanov", "Иванов И.И.", "инженер", "+7900", now=1000.0)
    path = owner_spool_path(tmp_path, "ivanov")
    assert path.name == "owner-ivanov.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "owner": "ivanov",
        "written_at": 1000,
        "full_name": "Иванов И.И.",
        "position": "инженер",
        "phone": "+7900",
    }
    assert not list(tmp_path.glob("*.tmp")), "временный файл остался на диске"


def test_owner_spool_path_has_no_traversal(tmp_path) -> None:
    """Имя файла строится из имени пользователя — обход каталогов недопустим."""
    from client.tray.owner_spool import owner_spool_path

    path = owner_spool_path(tmp_path, "../../evil")
    assert path.parent == tmp_path  # разделители вычищены -> выход из каталога невозможен
    assert "/" not in path.name and "\\" not in path.name
    assert path.resolve().parent == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# F9 — агент: строгое чтение спула (вход враждебный)                          #
# --------------------------------------------------------------------------- #


def _spool(tmp_path, name: str, payload: dict) -> None:
    import json

    (tmp_path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_owner_identity_reads_valid_file(tmp_path) -> None:
    from client.collectors.user_identity import read_owner_identity

    _spool(
        tmp_path,
        "owner-ivanov.json",
        {"written_at": 1000, "full_name": "Иванов И.И.", "position": "инженер", "phone": "+7900"},
    )
    got = read_owner_identity(tmp_path, now=1000.0)
    assert got == {
        "owner_full_name": "Иванов И.И.",
        "owner_position": "инженер",
        "owner_phone": "+7900",
    }


def test_read_owner_identity_rejects_hostile_input(tmp_path) -> None:
    """Каждый враждебный файл проверяется ОТДЕЛЬНО и в момент, когда он ещё свеж.
    Ревью блока F: прежняя версия утверждала на now, где протухли ВСЕ файлы, и
    поэтому проходила независимо от работы защит."""
    from client.collectors import user_identity as ui

    cases = {
        "owner-broken.json": "{not json",
        "owner-list.json": "[1,2,3]",
        "owner-int.json": "42",
        "owner-null.json": "null",
        # 1e400 -> float('inf'); int(inf) бросает OverflowError, НЕ ValueError.
        "owner-inf.json": '{"written_at": 1e400, "full_name": "Подлог"}',
        "owner-nan.json": '{"written_at": NaN, "full_name": "Подлог"}',
        "owner-future.json": '{"written_at": 99999999999, "full_name": "Будущее"}',
        "owner-huge.json": '{"written_at": 1000, "full_name": "' + "я" * ui._MAX_FILE_BYTES + '"}',
    }
    for name, body in cases.items():
        sub = tmp_path / name.replace(".json", "")
        sub.mkdir()
        (sub / name).write_text(body, encoding="utf-8")
        got = ui.read_owner_identity(sub, now=1000.0)
        assert got == {
            "owner_full_name": None,
            "owner_position": None,
            "owner_phone": None,
        }, f"{name} прошёл защиту: {got}"


def test_hostile_file_never_breaks_other_users(tmp_path) -> None:
    """Ревью блока F: подложенный файл роняет чтение исключением -> валидный спул
    ДРУГОГО пользователя не читается вообще. Проверяем оба свойства сразу."""
    from client.collectors import user_identity as ui

    (tmp_path / "owner-aaa.json").write_text(
        '{"written_at": 1e400, "full_name": "Подлог"}', encoding="utf-8"
    )
    (tmp_path / "owner-zzz.json").write_text(
        '{"written_at": 1000, "full_name": "Настоящий"}', encoding="utf-8"
    )
    got = ui.read_owner_identity(tmp_path, now=1000.0)  # не должно бросить
    assert got["owner_full_name"] == "Настоящий"


def test_stale_spool_is_ignored(tmp_path) -> None:
    from client.collectors import user_identity as ui

    _spool(tmp_path, "owner-stale.json", {"written_at": 1000, "full_name": "Старый"})
    assert ui.read_owner_identity(tmp_path, now=1000.0 + ui._FRESH_SEC + 1) == {
        "owner_full_name": None,
        "owner_position": None,
        "owner_phone": None,
    }


def test_many_files_do_not_shadow_the_fresh_one(tmp_path) -> None:
    """Ревью блока F: кап отбирал файлы ПО АЛФАВИТУ — 50 файлов «owner-0*.json»
    навсегда затеняли настоящий спул. Отбор идёт по свежести файла."""
    import os
    import time

    from client.collectors import user_identity as ui

    now = time.time()
    for i in range(ui._MAX_FILES + 10):
        path = tmp_path / f"owner-0{i:03d}.json"
        _spool(tmp_path, path.name, {"written_at": int(now) - 100, "full_name": f"Шум{i}"})
        os.utime(path, (now - 1000, now - 1000))
    real = tmp_path / "owner-zzz-real.json"
    _spool(tmp_path, real.name, {"written_at": int(now), "full_name": "Настоящий"})
    os.utime(real, (now, now))

    assert ui.read_owner_identity(tmp_path, now=now)["owner_full_name"] == "Настоящий"


def test_read_owner_identity_clips_overlong_values(tmp_path) -> None:
    from client.collectors.user_identity import read_owner_identity

    _spool(tmp_path, "owner-x.json", {"written_at": 1000, "full_name": "я" * 200})
    got = read_owner_identity(tmp_path, now=1000.0)
    assert len(got["owner_full_name"]) == 140


def test_read_owner_identity_newest_file_wins(tmp_path) -> None:
    from client.collectors.user_identity import read_owner_identity

    _spool(tmp_path, "owner-a.json", {"written_at": 1000, "full_name": "Старый"})
    _spool(tmp_path, "owner-b.json", {"written_at": 2000, "full_name": "Новый"})
    assert read_owner_identity(tmp_path, now=2000.0)["owner_full_name"] == "Новый"


def test_read_owner_identity_ties_are_deterministic(tmp_path) -> None:
    """При равном written_at побеждает лексикографически меньшее имя файла."""
    from client.collectors.user_identity import read_owner_identity

    _spool(tmp_path, "owner-b.json", {"written_at": 1000, "full_name": "Б"})
    _spool(tmp_path, "owner-a.json", {"written_at": 1000, "full_name": "А"})
    assert read_owner_identity(tmp_path, now=1000.0)["owner_full_name"] == "А"


def test_read_owner_identity_ignores_tmp_files(tmp_path) -> None:
    """Недописанный *.tmp не должен подхватываться агентским глобом."""
    from client.collectors.user_identity import read_owner_identity

    _spool(tmp_path, "owner-x.json.tmp", {"written_at": 1000, "full_name": "Недописанный"})
    assert read_owner_identity(tmp_path, now=1000.0)["owner_full_name"] is None


def test_owner_spool_round_trip_tray_to_agent(tmp_path) -> None:
    """Контракт границы: что записал трей, то и прочитал агент."""
    from client.collectors.user_identity import read_owner_identity
    from client.tray.owner_spool import write_owner_spool

    write_owner_spool(tmp_path, "ivanov", "Иванов И.И.", "инженер", "+7 900", now=5000.0)
    assert read_owner_identity(tmp_path, now=5000.0) == {
        "owner_full_name": "Иванов И.И.",
        "owner_position": "инженер",
        "owner_phone": "+7 900",
    }


def test_client_owner_modules_are_pure_stdlib() -> None:
    """Инвариант: client/ — чистый stdlib, без импортов shared/ и зависимостей."""
    import re
    from pathlib import Path

    import client.collectors.user_identity as ui
    import client.tray.owner_spool as os_mod

    for mod in (ui, os_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M)
        for name in imports:
            root = name.split(".")[0]
            assert root != "shared", f"{mod.__name__} импортирует shared: {name}"
            assert root in {
                "__future__",
                "contextlib",
                "json",
                "os",
                "stat",
                "sys",
                "time",
                "pathlib",
                "typing",
                "client",
                "re",
                "datetime",
            }, f"{mod.__name__}: внешний импорт {name}"


# --------------------------------------------------------------------------- #
# F10 — клиент: поля конфига и конверта                                       #
# --------------------------------------------------------------------------- #


def test_client_config_round_trips_owner_fields(tmp_path) -> None:
    from client.config import ClientConfig, load_config, save_config

    path = tmp_path / "config.json"
    cfg = ClientConfig(
        server_url="http://x/",
        device_id="d1",
        owner_full_name="Иванов И.И.",
        owner_position="инженер",
        owner_phone="+7900",
    )
    save_config(cfg, path)
    back = load_config(path)
    assert back.owner_full_name == "Иванов И.И."
    assert back.owner_position == "инженер"
    assert back.owner_phone == "+7900"


def test_config_without_owner_fields_loads_empty(tmp_path) -> None:
    import json

    from client.config import load_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server_url": "http://x/", "device_id": "d1"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.owner_full_name == "" and cfg.owner_position == "" and cfg.owner_phone == ""


def test_envelope_carries_owner_fields(tmp_path) -> None:
    from client.config import ClientConfig
    from client.transport import Transport

    cfg = ClientConfig(
        server_url="http://127.0.0.1:9/",
        device_id="d1",
        buffer_path=str(tmp_path / "b.jsonl"),
        owner_full_name="Иванов И.И.",
        owner_position="инженер",
        owner_phone="+7900",
    )
    env = Transport(cfg)._envelope("heartbeat", {})
    assert env["owner_full_name"] == "Иванов И.И."
    assert env["owner_position"] == "инженер"
    assert env["owner_phone"] == "+7900"

    empty = ClientConfig(
        server_url="http://127.0.0.1:9/", device_id="d1", buffer_path=str(tmp_path / "b2.jsonl")
    )
    env2 = Transport(empty)._envelope("heartbeat", {})
    assert env2["owner_full_name"] is None


def test_push_owner_identity_sends_token_header(tmp_path, monkeypatch) -> None:
    """F11: одна best-effort прямая отправка, без буферизации и ретраев."""
    import json
    import urllib.request

    from client.tray.push import push_owner_identity

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"server_url": "http://srv/", "device_id": "dev-1", "ingest_token": "tok"}),
        encoding="utf-8",
    )

    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["token"] = req.get_header("X-srp-token")
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert push_owner_identity(config_path, "Иванов И.И.", "инженер", "+7900") is True
    assert captured["url"] == "http://srv/api/v1/ingest"
    assert captured["token"] == "tok"
    assert captured["timeout"] == 5

    # Пустой токен -> заголовок отсутствует.
    config_path.write_text(
        json.dumps({"server_url": "http://srv/", "device_id": "dev-1", "ingest_token": ""}),
        encoding="utf-8",
    )
    push_owner_identity(config_path, "x", "y", "z")
    assert captured["token"] is None

    # Исключение при отправке -> False, без буферизации и ретраев.
    def raise_urlopen(req, timeout=None):
        raise OSError("boom")

    monkeypatch.setattr(urllib.request, "urlopen", raise_urlopen)
    assert push_owner_identity(config_path, "x", "y", "z") is False


# --------------------------------------------------------------------------- #
# F12 — дашборд: строка во флоте и виджет на карточке                        #
# --------------------------------------------------------------------------- #


def _owner_envelope(device_id: str, **owner: Any) -> dict[str, Any]:
    from tests.conftest import envelope

    return {**envelope(device_id, "heartbeat", {}), **owner}


@pytest.mark.integration
def test_fleet_row_shows_owner_fields(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_owner_envelope(
            "dev-f12a",
            owner_full_name="Иванов И.И.",
            owner_position="инженер",
            owner_phone="+7 900 000-00-00",
        ),
    )
    html = client.get("/fleet/fragment").text
    assert "Иванов И.И., инженер, +7 900 000-00-00" in html


@pytest.mark.integration
def test_fleet_row_owner_only_full_name_no_dangling_comma(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_owner_envelope("dev-f12b", owner_full_name="Иванов И.И."))
    html = client.get("/fleet/fragment").text
    assert "Иванов И.И." in html
    assert "Иванов И.И.," not in html


@pytest.mark.integration
def test_fleet_row_no_owner_data_no_owner_string(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_owner_envelope("dev-f12c"))
    html = client.get("/fleet/fragment").text
    assert 'class="dev-sub">,' not in html


@pytest.mark.integration
def test_fleet_and_device_owner_full_name_xss_escaped(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_owner_envelope("dev-f12d", owner_full_name="<b>Иванов</b>"),
    )
    fleet_html = client.get("/fleet/fragment").text
    assert "<b>Иванов</b>" not in fleet_html
    assert "&lt;b&gt;Иванов&lt;/b&gt;" in fleet_html

    device_html = client.get("/device/dev-f12d").text
    assert "<b>Иванов</b>" not in device_html
    assert "&lt;b&gt;Иванов&lt;/b&gt;" in device_html


@pytest.mark.integration
def test_device_page_has_owner_edit_widget_prefilled(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_owner_envelope("dev-f12e", owner_full_name="Петров П.П."))
    html = client.get("/device/dev-f12e").text
    assert 'id="owner-name-input"' in html
    assert 'maxlength="140"' in html
    assert 'id="owner-position-input"' in html
    assert 'id="owner-phone-input"' in html
    assert 'maxlength="32"' in html
    assert 'value="Петров П.П."' in html
    assert 'id="owner-save"' in html
    assert 'id="owner-status"' in html


def test_agent_refreshes_owner_identity_into_config(tmp_path, monkeypatch) -> None:
    """F9: агент подмешивает данные из спула в конфиг В ПАМЯТИ, не трогая диск.

    Ревью блока F: прежняя версия патчила `agent.save_config`, которого в модуле
    нет, — утверждение не могло упасть. Теперь проверяем ФАЙЛ конфига.
    """
    import json

    from client import agent as ag
    from client.config import ClientConfig

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server_url": "http://x/", "device_id": "d1"}), "utf-8")
    before = config_path.read_bytes()

    cfg = ClientConfig(
        server_url="http://127.0.0.1:9/", device_id="d1", buffer_path=str(tmp_path / "b.jsonl")
    )
    a = ag.Agent(cfg)
    monkeypatch.setattr(
        ag,
        "collect_owner_identity",
        lambda: {
            "owner_full_name": "Иванов И.И.",
            "owner_position": "инженер",
            "owner_phone": "+7900",
        },
    )

    a._refresh_owner_identity()

    assert cfg.owner_full_name == "Иванов И.И."
    assert cfg.owner_position == "инженер"
    assert config_path.read_bytes() == before, "агент записал конфиг на диск"


def test_agent_survives_hostile_spool(tmp_path, monkeypatch) -> None:
    """Сбой чтения спула не должен ронять цикл агента."""
    from client import agent as ag
    from client.config import ClientConfig

    cfg = ClientConfig(
        server_url="http://127.0.0.1:9/", device_id="d1", buffer_path=str(tmp_path / "b.jsonl")
    )
    a = ag.Agent(cfg)

    def _boom():
        raise OSError("spool unreadable")

    monkeypatch.setattr(ag, "collect_owner_identity", _boom)
    a._refresh_owner_identity()  # не должно бросить
    assert cfg.owner_full_name == ""


def test_push_module_is_pure_stdlib() -> None:
    """client/tray/push.py — новейший модуль с внешним I/O, инвариант тот же."""
    import re
    from pathlib import Path

    import client.tray.push as push_mod

    src = Path(push_mod.__file__).read_text(encoding="utf-8")
    for name in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M):
        root = name.split(".")[0]
        assert root != "shared", f"push.py импортирует shared: {name}"
        assert root in {
            "__future__",
            "json",
            "urllib",
            "uuid",
            "datetime",
            "pathlib",
            "typing",
            "client",
        }, f"push.py: внешний импорт {name}"


def test_tray_push_does_not_pollute_scoring() -> None:
    """Ревью блока F (CRITICAL): пустой heartbeat из трея становился «последним»
    замером и стирал реальную телеметрию из скоринга (замерено 62.5 -> 100.0).
    Трей обязан слать liveness — та же ветка ingest несёт три поля владельца, но
    не пишет строк телеметрии и не рескорит."""
    from pathlib import Path

    import client.tray.push as push_mod

    src = Path(push_mod.__file__).read_text(encoding="utf-8")
    assert '"msg_type": "liveness"' in src
    assert '"msg_type": "heartbeat"' not in src


def test_owner_widget_sends_only_changed_fields() -> None:
    """Повторное ревью блока F: форма предзаполнена, а PATCH слал ВСЕ три поля —
    очистка одного закрепляла два других за оператором, и агент переставал их
    обновлять, хотя пользователь их не трогал."""
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "server/web/templates/device.html").read_text(
        encoding="utf-8"
    )
    widget = html[html.index('id="owner-save"') :]
    assert "var initial = snapshot();" in widget
    assert "JSON.stringify(changed)" in widget
    assert "JSON.stringify({" not in widget.split("owner-status")[1][:1500]


def test_operator_write_keeps_agent_value(tmp_path) -> None:
    """Повторное ревью блока F: запись оператора уничтожала агентскую колонку —
    разделение колонок теряло смысл. Оператор пишет только в свою колонку."""
    from server import db

    db.init_db(str(tmp_path / "own.db"))
    db.touch_device("x", "2026-03-01T10:00:00+00:00", "0.2.0", owner_full_name="ОтАгента")
    db.set_device_owner_full_name("x", "ОтОператора")

    with db._connect() as conn:
        row = conn.execute(
            "SELECT owner_full_name, owner_full_name_operator FROM devices WHERE device_id='x'"
        ).fetchone()
    assert row[0] == "ОтАгента", "агентское значение уничтожено записью оператора"
    assert row[1] == "ОтОператора"
    assert db.get_device("x")["owner_full_name"] == "ОтОператора"  # читатель отдаёт правку


def test_spool_read_survives_vanishing_file(tmp_path, monkeypatch) -> None:
    """Повторное ревью блока F: stat() в ключе сортировки мог бросить OSError и
    обнулить чтение ВСЕГО каталога — тот же класс, что OverflowError."""
    from client.collectors import user_identity as ui

    _spool(tmp_path, "owner-real.json", {"written_at": 1000, "full_name": "Настоящий"})
    ghost = tmp_path / "owner-ghost.json"
    ghost.write_text("{}", encoding="utf-8")

    real_stat = ui.Path.stat

    def _flaky(self, *a, **kw):
        if self.name == "owner-ghost.json":
            raise OSError("файл исчез между glob и stat")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(ui.Path, "stat", _flaky)
    assert ui.read_owner_identity(tmp_path, now=1000.0)["owner_full_name"] == "Настоящий"
