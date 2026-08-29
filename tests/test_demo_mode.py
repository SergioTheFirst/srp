"""Демо-режим (SRP_DEMO=1): read-only гейт на 9 мутирующих ручках + бейдж ДЕМО."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Coroutine, Iterator, Optional

import pytest
from fastapi.testclient import TestClient
from server import limits, main
from server.config import ServerConfig
from server.main import create_app

# ack/purge требуют тело запроса, схема-валидное (AckBody/PurgeBody — все поля с
# дефолтами, {} проходит pydantic-валидацию) -- иначе FastAPI отдаёт 422 до того,
# как хендлер вообще начнёт выполняться, и гейт не тестируется по-настоящему.
MUTATING = [
    ("post", "/api/v1/ingest", {"device_id": "d", "msg_type": "heartbeat", "payload": {}}),
    ("post", "/api/v1/devices/d/ack", {}),
    ("post", "/api/v1/discovery/poll", None),
    ("post", "/api/v1/topology/poll", None),
    ("post", "/api/v1/network-map/collect", None),
    ("patch", "/api/v1/devices/d/meta", {}),
    ("post", "/api/v1/devices/d/delete", None),
    ("post", "/api/v1/devices/purge", {}),
    ("post", "/api/v1/printers/poll", None),
]


@pytest.fixture
def demo_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Тот же способ сборки приложения, что и conftest.client, но с
    DEMO_MODE=True, выставленным ДО создания app (гейт читает
    ``limits.DEMO_MODE`` в рантайме через атрибут модуля, поэтому setattr
    достаточно -- reload не нужен)."""
    monkeypatch.setattr(limits, "DEMO_MODE", True)
    db_file = tmp_path / "test_srp_demo.db"
    app = create_app(
        ServerConfig(
            db_path=str(db_file),
            org_directory_path=str(tmp_path / "org_directory.json"),
        )
    )
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("method,path,body", MUTATING)
def test_demo_blocks_mutations(
    demo_client: TestClient, method: str, path: str, body: Optional[dict]
) -> None:
    resp = getattr(demo_client, method)(path, json=body)
    assert resp.status_code == 403, resp.text
    assert "демо" in resp.json()["detail"]


def test_demo_get_pages_work(demo_client: TestClient) -> None:
    for path in ("/", "/health", "/pipeline", "/api/v1/devices"):
        assert demo_client.get(path).status_code == 200


def test_demo_badge_rendered(demo_client: TestClient) -> None:
    assert "ДЕМО" in demo_client.get("/").text


def test_no_demo_badge_outside_demo_mode(client: TestClient) -> None:
    """Контроль: вне демо-режима бейдж не должен появляться (иначе тест бейджа
    не способен упасть — проверяем оба состояния глобала)."""
    assert "ДЕМО" not in client.get("/").text


def test_missing_token_banner_hidden_in_demo(demo_client: TestClient) -> None:
    """Публичное демо работает без ingest_token -- жёлтый баннер «БЕЗ
    аутентификации» на витрине не нужен (constraints.md §3)."""
    assert "БЕЗ аутентификации" not in demo_client.get("/").text


def test_missing_token_banner_shown_outside_demo(client: TestClient) -> None:
    """Контроль: вне демо-режима баннер остаётся как был (client fixture --
    ingest_token="" по умолчанию)."""
    assert "БЕЗ аутентификации" in client.get("/").text


def _patch_sweeps(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    monkeypatch.setattr(main, "_run_disk_readings_backfill", lambda: calls.append("backfill"))
    monkeypatch.setattr(main, "_run_retention_sweep", lambda cfg: calls.append("retention"))
    monkeypatch.setattr(main, "_run_maintenance_sweep", lambda cfg: calls.append("maintenance"))

    # _source_staleness_loop is a background asyncio task, not a synchronous
    # startup call -- record the call the moment lifespan builds the coroutine
    # (asyncio.create_task(_source_staleness_loop(cfg))), not when its body
    # eventually runs, so the assertion doesn't race the event loop.
    def _fake_staleness_loop(cfg: ServerConfig) -> Coroutine[Any, Any, None]:
        calls.append("staleness")

        async def _noop() -> None:
            return None

        return _noop()

    monkeypatch.setattr(main, "_source_staleness_loop", _fake_staleness_loop)


def test_demo_skips_startup_sweeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пин на review-фикс: стартовый пропуск бэкфилла/ретеншна/rollup, а также
    фоновых circles (ретеншн-loop, staleness-loop, printer/netdisco) в
    демо-режиме несёт реальную нагрузку (демо-база собрана сидером заранее,
    её таймстемпы тянутся до "сейчас") -- регрессия, включающая свипы или
    циклы обратно, не должна проходить незамеченной."""
    monkeypatch.setattr(limits, "DEMO_MODE", True)
    calls: list = []
    _patch_sweeps(monkeypatch, calls)
    app = create_app(
        ServerConfig(db_path=str(tmp_path / "t.db"), org_directory_path=str(tmp_path / "org.json"))
    )
    with TestClient(app):
        pass
    assert calls == []


def test_no_demo_runs_startup_sweeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Контроль: вне демо-режима стартовые свипы и запуск staleness-loop
    происходят как раньше."""
    calls: list = []
    _patch_sweeps(monkeypatch, calls)
    app = create_app(
        ServerConfig(db_path=str(tmp_path / "t.db"), org_directory_path=str(tmp_path / "org.json"))
    )
    with TestClient(app):
        pass
    assert calls == ["backfill", "retention", "maintenance", "staleness"]
