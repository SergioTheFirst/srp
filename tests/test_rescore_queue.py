"""W4.0 RescoreQueue: коалесценция, изоляция ошибок, drain, hook в pipeline."""

from __future__ import annotations

import threading
import time

from server.rescore_queue import RescoreQueue


def test_coalesces_burst_for_one_device() -> None:
    release = threading.Event()
    calls: list[str] = []

    def recompute(did: str) -> None:
        calls.append(did)
        release.wait(2.0)  # держим воркер, чтобы сабмиты легли в pending

    q = RescoreQueue(recompute)
    q.start()
    q.submit("d1")
    for _ in range(200):  # дождаться, пока воркер займёт d1
        if calls:
            break
        time.sleep(0.01)
    q.submit("d1")
    q.submit("d1")
    q.submit("d1")
    release.set()
    assert q.drain(5.0)
    q.stop()
    # первый прогон + ОДИН доп. прогон за весь шторм из трёх сабмитов
    assert calls == ["d1", "d1"]


def test_recompute_error_does_not_kill_worker() -> None:
    calls: list[str] = []

    def recompute(did: str) -> None:
        calls.append(did)
        if did == "boom":
            raise RuntimeError("scoring exploded")

    q = RescoreQueue(recompute)
    q.start()
    q.submit("boom")
    assert q.drain(5.0)
    q.submit("ok")
    assert q.drain(5.0)
    q.stop()
    assert "ok" in calls


def test_drain_true_on_idle_queue() -> None:
    q = RescoreQueue(lambda _did: None)
    q.start()
    assert q.drain(1.0)
    q.stop()


def test_pipeline_enqueues_instead_of_inline_when_queue_set(seeded_client) -> None:
    from server import pipeline

    class FakeQueue:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        def submit(self, device_id: str) -> None:
            self.submitted.append(device_id)

    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    fq = FakeQueue()
    pipeline.set_rescore_queue(fq)  # conftest-autouse вернёт None после теста
    body = {
        "device_id": did,
        "agent_version": "0.2.0",
        "msg_type": "heartbeat",
        "ts": "2026-07-12T00:00:00+00:00",
        "payload": {"cpu_load_pct": 5.0, "mem_used_pct": 40.0},
        "source_health": {},
    }
    r = seeded_client.post("/api/v1/ingest", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["scores"] is None and data["scores_updated"] is False
    assert fq.submitted == [did]
