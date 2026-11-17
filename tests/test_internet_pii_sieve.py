"""Сито ПДн: из интернета (порт прокси за Caddy) ни один GET не отдаёт персональных
данных; из сети те же ответы отдают их все (спека 2026-11-05-dashboard-login §3).

Уникальная строка -- в КАЖДЫЙ источник: ФИО/должность/телефон/комментарий агента и
оператора (на разных ПК: операторское побеждает агентское), заметка «принято», личный и
машинный сертификаты, печать, задания IPP, текст события, сырые mDNS (base64),
необъявленное поле агента (extra="allow") и admin_token. Обходятся все GET-маршруты
приложения, кроме закрытых шлюзом: новый маршрут попадает в сито сам.
"""

from __future__ import annotations

import base64
import itertools
import re
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from server import db, internet_view, users

from tests.conftest import envelope, healthy

_A, _B, _PRN, _NID = "sieve-a", "sieve-b", "prn-sieve", "nd-ip-sieve"
# Параметр пути -> каждый засеянный id; новый вид параметра -- KeyError, а не пропуск.
_IDS = {"device_id": (_A, _B), "printer_id": (_PRN,), "device_nid": (_NID,)}
_QUERY = {"/api/v1/device-card": "?device_id={device_id}"}  # без ключа -- 422
_NOW = datetime.now(timezone.utc)
_SOON = (_NOW + timedelta(days=200)).isoformat()  # сертификат в силе -- страница его рисует

_PII = {
    "owner_full_name": "PiiAgentFio",
    "owner_position": "PiiAgentPos",
    "owner_phone": "PiiAgentTel",
    "comment": "PiiAgentComment",
    "operator_full_name": "PiiOperFio",
    "operator_position": "PiiOperPos",
    "operator_phone": "PiiOperTel",
    "operator_comment": "PiiOperComment",
    "ack_note": "PiiAckNote",
    "cert_owner": "PiiCertOwner",
    "cert_subject": "PiiCertSubject",
    "cert_issuer": "PiiCertIssuer",
    "cert_future": "PiiCertFuture",
    "machine_subject": "PiiMachineSubject",
    "machine_issuer": "PiiMachineIssuer",
    "print_user": "PiiPrintUser",
    "ipp_user": "PiiIppUser",
    "ipp_document": "PiiIppDocument",
    "event_login": "PiiEventLogin",
    "lan_hint": base64.b64encode(b"PiiLanHint: Ivans-iPhone._companion-link").decode(),
    "inventory_future": "PiiInventoryFuture",
    "historical_future": "PiiHistoricalFuture",
    "heartbeat_future": "PiiHeartbeatFuture",
    "admin_token": "PiiAdminToken",
}


def _ingest(c: TestClient, device_id: str, msg_type: str, payload: dict, **top: Any) -> None:
    resp = c.post("/api/v1/ingest", json={**envelope(device_id, msg_type, payload), **top})
    assert resp.status_code == 200, resp.text


def _inventory(hostname: str, serial: str, **extra: Any) -> dict:
    inv = healthy("inventory")
    inv["disks"][0]["serial_hash"] = serial  # разные диски: fleet_canon не склеит два ПК
    return {**inv, "hostname": hostname, **extra}


def _historical() -> dict:
    personal = {
        "subject": "CN=" + _PII["cert_subject"],
        "issuer": "CN=" + _PII["cert_issuer"],
        "owner": "CORP\\" + _PII["cert_owner"],
        "not_after": _SOON,
        "zz_future": _PII["cert_future"],
    }
    # Самоподписанный (издатель = субъект): издатель рендерится в таблице машинных сертификатов.
    machine = {
        "subject": "CN=" + _PII["machine_subject"],
        "issuer": "CN=" + _PII["machine_issuer"],
        "not_after": _SOON,
    }
    hint = {"ip": "192.168.1.50", "source": "mdns", "data_b64": _PII["lan_hint"]}
    return {
        **healthy("historical"),
        "certificates": [machine],
        "user_certificates": [personal],
        "lan_hints": [hint],
        "zz_future": _PII["historical_future"],
    }


def _seed_agent_pc(c: TestClient) -> None:
    """ПК A: всё, что шлёт агент, -- владелец в каждом конверте, как у настоящего."""
    owner = {k: _PII[k] for k in ("owner_full_name", "owner_position", "owner_phone", "comment")}
    event = {
        "ts": _NOW.isoformat(),
        "log": "Application",
        "source": "Application Error",
        "event_id": 1000,
        "level": "Error",
        "message": f"Faulting module C:\\Users\\{_PII['event_login']}\\AppData\\Local\\app.exe",
    }
    job = {
        "job_id": 7,
        "ts": (_NOW - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00"),  # окно 30 дней
        "printer": "HP LaserJet",
        "pages": 3,
        "size_bytes": 1000,
        "user_name": _PII["print_user"],
    }
    inventory = _inventory("SIEVE-A", "aa000001", zz_future=_PII["inventory_future"])
    _ingest(c, _A, "inventory", inventory, **owner)
    _ingest(c, _A, "historical", _historical(), **owner)
    heartbeat = {**healthy("heartbeat"), "zz_future": _PII["heartbeat_future"]}
    _ingest(c, _A, "heartbeat", heartbeat, **owner)
    _ingest(c, _A, "events", {"window_hours": 24.0, "events": [event]}, **owner)
    _ingest(c, _A, "print_jobs", {"jobs": [job], "window_from": None}, **owner)


def _seed(c: TestClient) -> None:
    _seed_agent_pc(c)
    # ПК B: правки оператора (db сливает *_operator в те же ключи) и заметка «принято».
    _ingest(c, _B, "inventory", _inventory("SIEVE-B", "bb000002"))
    for msg_type in ("historical", "heartbeat", "events"):
        _ingest(c, _B, msg_type, healthy(msg_type))
    assert db.set_device_owner_full_name(_B, _PII["operator_full_name"])
    assert db.set_device_owner_position(_B, _PII["operator_position"])
    assert db.set_device_owner_phone(_B, _PII["operator_phone"])
    assert db.set_device_comment(_B, _PII["operator_comment"])
    db.set_ack(_B, _PII["ack_note"], _NOW.isoformat())
    reading = {"ip": "192.168.1.60", "online": True, "model": "HP LJ", "hostname": "PRN-SIEVE"}
    db.store_printer_reading(_PRN, {**reading, "serial": "S1", "total_pages": 100})
    ipp = {"job_id": 1, "name": _PII["ipp_document"], "user_name": _PII["ipp_user"]}
    db.store_printer_ipp_jobs(_PRN, [{**ipp, "impressions": 2}], received_at=_NOW.isoformat())
    # Отдельный сетевой узел: связанный с ПК /netdisco/device/{nid} уводит редиректом.
    db.upsert_net_device({"device_nid": _NID, "ip": "10.9.9.9", "dev_type": "switch"})
    c.app.state.admin_token = _PII["admin_token"]


def _paths(app: Any) -> list[str]:
    """Каждый GET-маршрут, кроме закрытых шлюзом (оттуда -- голый 404, test_external_access)."""
    paths: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods:
            continue
        if internet_view.is_closed(route.path) or route.path == "/login":
            continue
        template = route.path + _QUERY.get(route.path, "")
        names = re.findall(r"\{(\w+)\}", template)
        for ids in itertools.product(*(_IDS[name] for name in names)):
            paths.append(template.format(**dict(zip(names, ids))))
    return paths


def _internet(app: Any) -> TestClient:
    """Зритель из интернета: Caddy -> 127.0.0.1:8001 с https и сессией записи «сито»."""
    assert db.create_user("сито", users.hash_password("pw"))
    token = users.login("сито", "pw")
    assert token
    return TestClient(
        app,
        base_url="https://testserver:8001",
        cookies={"jspace_session": token},
        follow_redirects=False,
    )


def test_internet_gets_no_personal_data_and_the_network_gets_it_all(client) -> None:
    _seed(client)
    inet, lan = _internet(client.app), TestClient(client.app, follow_redirects=False)
    inet_text, lan_text = [], []
    for path in _paths(client.app):
        a, b = lan.get(path), inet.get(path)
        assert a.status_code < 400 and b.status_code == a.status_code, (path, a, b)
        # Без учёта регистра: шаблоны печатают копии строк через |lower (поиск по таблицам).
        assert [k for k, v in _PII.items() if v.lower() in b.text.lower()] == [], path
        # Контроль на каждом маршруте: что видно из сети, видно и из интернета (не пустая страница).
        assert all(h in b.text for h in ("SIEVE-A", "SIEVE-B", "PRN-SIEVE") if h in a.text), path
        lan_text.append(a.text)
        inet_text.append(b.text)
    # Сито засеяно: из сети видна каждая строка -- иначе «нет утечки» ничего не значит.
    assert [k for k, v in _PII.items() if v not in "".join(lan_text)] == []
    # Позитивный контроль: из интернета страницы не пусты -- ПК в них есть.
    assert all(host in "".join(inet_text) for host in ("SIEVE-A", "SIEVE-B"))


class _Pair(NamedTuple):
    site: str
    devices: list


def test_redact_walks_tuples_and_leaves_the_input_alone() -> None:
    # Парк на странице -- список кортежей (объект, [ПК]): dashboard._group_by_site.
    row = {"hostname": "PC-1", "owner_phone": "1", "ack": {"note": "x", "acked_at": "t"}}
    ctx = {"groups": [("офис", [row])], "pair": _Pair("офис", [row]), "users": [{"pages": 1}]}
    out = internet_view.redact(ctx)
    clean = [{"hostname": "PC-1", "ack": {"acked_at": "t"}}]
    assert out["groups"] == [("офис", clean)] and out["users"] == []
    assert isinstance(out["pair"], _Pair) and out["pair"].devices == clean
    assert row == {"hostname": "PC-1", "owner_phone": "1", "ack": {"note": "x", "acked_at": "t"}}
