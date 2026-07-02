"""Liveness-конверт: контракт, ingest (только last_seen), порог offline ≤10 мин."""

from __future__ import annotations

import pytest
from client.agent import TASKS
from client.collectors.liveness import collect_liveness
from server import db
from shared.schema import Envelope, parse_payload
from tests.conftest import envelope

pytestmark = pytest.mark.integration


def test_contract_accepts_liveness_msg_type() -> None:
    env = Envelope(device_id="dev-1", msg_type="liveness", payload={"alive": True})
    assert env.msg_type == "liveness"
    parsed = parse_payload("liveness", {"alive": True})
    assert parsed.alive is True  # type: ignore[attr-defined]


def test_agent_has_liveness_task_with_own_interval() -> None:
    tasks = {name: attr for name, _, attr in TASKS}
    assert tasks.get("liveness") == "liveness_interval_sec"


def test_liveness_payload_is_truthy_so_transport_never_skips() -> None:
    # transport.send пропускает конверт с falsy payload И falsy source_health --
    # пустой {} тихо убил бы весь механизм. Пин: payload непустой.
    result = collect_liveness()
    assert result.payload


def test_ingest_liveness_touches_last_seen_without_rows_or_scores(client) -> None:
    r = client.post("/api/v1/ingest", json=envelope("dev-lv", "liveness", {"alive": True}))
    assert r.status_code == 200
    body = r.json()
    assert body["scores_updated"] is False  # liveness не дёргает рескоринг
    d = db.get_device("dev-lv")
    assert d is not None and d["last_seen"]  # устройство создано, last_seen проставлен
    assert d["latest_heartbeat"] is None  # ни одной строки телеметрии не записано
    assert d["historical"] is None
    assert d["events"] == []


def test_stale_threshold_default_600_and_configurable() -> None:
    assert db.STALE_AFTER_SEC == 600
    db.set_stale_threshold(1200)
    try:
        assert db.STALE_AFTER_SEC == 1200
        assert db._STALE_AFTER_SEC == 1200
    finally:
        db.set_stale_threshold(600)


def test_stale_threshold_floor_is_60_seconds() -> None:
    db.set_stale_threshold(5)
    try:
        assert db.STALE_AFTER_SEC == 60  # защита от нулевого/отрицательного конфига
    finally:
        db.set_stale_threshold(600)


def test_liveness_envelope_cannot_smuggle_fake_trust_reading(client) -> None:
    """Security-review finding: source_health -- добавленный поверх liveness --
    НЕ должен доходить до evaluate_trust (иначе поддельный конверт фабрикует
    полностью доверенное 'ok'-чтение источника без реального сбора)."""
    payload = {
        "device_id": "dev-forge",
        "msg_type": "liveness",
        "payload": {"alive": True, "free_space_pct": 97.5},
        "source_health": {"free_space": {"status": "ok"}},
    }
    r = client.post("/api/v1/ingest", json=payload)
    assert r.status_code == 200
    assert db.get_source_trusts("dev-forge") == {}
