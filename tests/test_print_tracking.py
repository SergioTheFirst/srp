"""Print tracking — DB, pipeline, API, and collector unit tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import envelope

pytestmark = pytest.mark.integration

# Вчерашняя метка, не хардкод даты: print-аналитика смотрит скользящее
# окно (days=30) от текущего момента -- тот же приём, что job_ts в smoke.py.
_JOB_TS = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pj_envelope(device_id: str, jobs: list) -> dict:
    return envelope(device_id, "print_jobs", {"jobs": jobs, "window_from": None})


def _job(printer: str = "HP LaserJet", pages: int = 2, user: str = "alice") -> dict:
    return {
        "job_id": 42,
        "ts": _JOB_TS,
        "printer": printer,
        "pages": pages,
        "size_bytes": 12000,
        "user_name": user,
    }


# ---------------------------------------------------------------------------
# Pipeline: print_jobs msg_type
# ---------------------------------------------------------------------------


def test_print_jobs_ingest_returns_200(client: TestClient) -> None:
    r = client.post("/api/v1/ingest", json=_pj_envelope("dev-1", [_job()]))
    assert r.status_code == 200
    body = r.json()
    assert body["msg_type"] == "print_jobs"
    # print_jobs must NOT trigger score recompute
    assert body["scores_updated"] is False


def test_print_jobs_empty_jobs_list_accepted(client: TestClient) -> None:
    r = client.post("/api/v1/ingest", json=_pj_envelope("dev-1", []))
    assert r.status_code == 200


def test_print_jobs_does_not_overwrite_scores(client: TestClient) -> None:
    """Ingesting print_jobs after inventory must leave scores intact."""
    client.post(
        "/api/v1/ingest",
        json=envelope(
            "dev-2",
            "inventory",
            {
                "hostname": "PC-PRINT",
                "manufacturer": "Dell",
                "model": "OptiPlex",
                "chassis": "desktop",
            },
        ),
    )
    before = client.get("/api/v1/devices/dev-2").json()
    client.post("/api/v1/ingest", json=_pj_envelope("dev-2", [_job()]))
    after = client.get("/api/v1/devices/dev-2").json()
    assert before.get("risk_exposure") == after.get("risk_exposure")


# ---------------------------------------------------------------------------
# API: /api/v1/devices/{id}/print
# ---------------------------------------------------------------------------


def test_device_print_returns_zero_when_no_jobs(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-3", "inventory", {"hostname": "PC3"}))
    r = client.get("/api/v1/devices/dev-3/print?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 0
    assert body["total_jobs"] == 0
    assert body["printers"] == []


def test_device_print_404_for_unknown_device(client: TestClient) -> None:
    r = client.get("/api/v1/devices/no-such-device/print")
    assert r.status_code == 404


def test_device_print_counts_pages(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-4", "inventory", {"hostname": "PC4"}))
    jobs = [
        {**_job(pages=5), "job_id": 101},
        {**_job(pages=3, printer="Brother MFC"), "job_id": 102},
    ]
    client.post("/api/v1/ingest", json=_pj_envelope("dev-4", jobs))
    r = client.get("/api/v1/devices/dev-4/print?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 8
    assert body["total_jobs"] == 2
    assert len(body["printers"]) == 2


# ---------------------------------------------------------------------------
# API: /api/v1/fleet/print
# ---------------------------------------------------------------------------


def test_fleet_print_empty(client: TestClient) -> None:
    r = client.get("/api/v1/fleet/print?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 0
    assert body["total_jobs"] == 0
    assert body["printer_count"] == 0


def test_fleet_print_aggregates_across_devices(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-a", [_job(pages=10)]))
    client.post("/api/v1/ingest", json=_pj_envelope("dev-b", [_job(pages=7)]))
    r = client.get("/api/v1/fleet/print?days=30")
    body = r.json()
    assert body["total_pages"] == 17
    assert body["total_jobs"] == 2


def test_fleet_print_today_empty(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/print?today=1").json()
    assert body["today"] is True
    assert body["period_days"] == 0
    assert body["total_pages"] == 0
    assert body["total_jobs"] == 0


def test_fleet_print_today_counts_only_current_day(client: TestClient) -> None:
    # One job stamped "now" (today) and one stamped two days ago: the today view
    # counts only the first; the 30-day window still counts both.
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    jobs = [
        {**_job(pages=4), "job_id": 301, "ts": today_ts},
        {**_job(pages=9), "job_id": 302, "ts": old_ts},
    ]
    client.post("/api/v1/ingest", json=_pj_envelope("dev-today", jobs))

    today = client.get("/api/v1/fleet/print?today=1").json()
    assert today["today"] is True
    assert today["total_pages"] == 4
    assert today["total_jobs"] == 1

    window = client.get("/api/v1/fleet/print?days=30").json()
    assert window["total_pages"] == 13
    assert window["total_jobs"] == 2


# ---------------------------------------------------------------------------
# API: /api/v1/fleet/print/analytics
# ---------------------------------------------------------------------------


def test_fleet_print_analytics_shape(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj_envelope("dev-c", [_job(printer="Xerox WorkCentre", pages=4, user="bob")]),
    )
    r = client.get("/api/v1/fleet/print/analytics?days=30")
    assert r.status_code == 200
    body = r.json()
    assert "daily" in body
    assert "printers" in body
    assert "users" in body
    assert "departments" in body
    assert body["total_pages"] == 4


def test_fleet_print_analytics_daily_has_date_and_pages(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-d", [_job(pages=3)]))
    body = client.get("/api/v1/fleet/print/analytics?days=30").json()
    for row in body["daily"]:
        assert "date" in row
        assert "pages" in row


# ---------------------------------------------------------------------------
# API: /api/v1/fleet/print/export.csv
# ---------------------------------------------------------------------------


def test_fleet_print_export_csv_headers(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-e", [_job(pages=2)]))
    r = client.get("/api/v1/fleet/print/export.csv?days=30")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    lines = r.text.splitlines()
    assert lines[0].startswith("ts,")
    assert "pages" in lines[0]
    assert len(lines) >= 2  # header + at least one row


def test_fleet_print_export_csv_empty_when_no_jobs(client: TestClient) -> None:
    r = client.get("/api/v1/fleet/print/export.csv?days=30")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert len(lines) == 1  # header only


# ---------------------------------------------------------------------------
# API: /api/v1/devices/{id}/meta PATCH (department)
# ---------------------------------------------------------------------------


def test_patch_meta_sets_department(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-f", "inventory", {"hostname": "PC-F"}))
    r = client.patch("/api/v1/devices/dev-f/meta", json={"department": "IT"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_patch_meta_404_unknown_device(client: TestClient) -> None:
    r = client.patch("/api/v1/devices/ghost/meta", json={"department": "HR"})
    assert r.status_code == 404


def test_patch_meta_null_department_accepted(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-g", "inventory", {"hostname": "PC-G"}))
    r = client.patch("/api/v1/devices/dev-g/meta", json={"department": None})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Collector: _parse_job unit (no PowerShell)
# ---------------------------------------------------------------------------


def test_collector_parse_job_valid() -> None:
    from client.collectors.print_jobs import _parse_job

    raw = {
        "job_id": 1,
        "ts": "2026-06-09T10:00:00Z",
        "printer": "HP LaserJet",
        "pages": 4,
        "size_bytes": 8000,
        "user_name": "alice",
    }
    result = _parse_job(raw)
    assert result is not None
    assert result["pages"] == 4
    assert result["printer"] == "HP LaserJet"


def test_collector_parse_job_clips_names_to_contract_cap() -> None:
    """Ревью LOW-2 (2026-08-21): кап 256 недостижим по ДОПУЩЕНИЮ, не по конструкции.

    Events-режим берёт имя как `$p[4].Value`; если индекс попадёт на DocumentName
    (текст от приложения — заголовок вкладки, URL — Windows его НЕ ограничивает),
    сервер ответит 422 и конверт целиком пропадёт (4xx = drop, watermark уже
    сдвинут). Клип на агенте + reject на сервере — паттерн printer_ports.py.
    """
    from client.collectors.print_jobs import _parse_job

    raw = {
        "job_id": 1,
        "ts": "2026-06-09T10:00:00Z",
        "printer": "P" * 300,
        "pages": 4,
        "user_name": "u" * 300,
    }
    result = _parse_job(raw)
    assert result is not None
    assert len(result["printer"]) == 256
    assert len(result["user_name"]) == 256


def test_collector_parse_job_filters_virtual_printer() -> None:
    from client.collectors.print_jobs import _parse_job

    raw = {
        "job_id": 2,
        "ts": "2026-06-09T10:01:00Z",
        "printer": "Microsoft Print to PDF",
        "pages": 2,
        "size_bytes": 1000,
        "user_name": "alice",
    }
    assert _parse_job(raw) is None


def test_collector_parse_job_filters_zero_pages() -> None:
    from client.collectors.print_jobs import _parse_job

    raw = {
        "job_id": 3,
        "ts": "2026-06-09T10:02:00Z",
        "printer": "HP LaserJet",
        "pages": 0,
        "size_bytes": 0,
        "user_name": "bob",
    }
    assert _parse_job(raw) is None


def test_collector_parse_job_none_input() -> None:
    from client.collectors.print_jobs import _parse_job

    assert _parse_job(None) is None


def test_collector_safe_ts_valid() -> None:
    from client.collectors.print_jobs import _safe_ts

    assert _safe_ts("2026-06-09T10:00:00+00:00") == "2026-06-09T10:00:00+00:00"


def test_collector_safe_ts_rejects_injection() -> None:
    from client.collectors.print_jobs import _safe_ts

    assert _safe_ts("'; DROP TABLE--") == ""
    assert _safe_ts("2026-06-09 10:00:00; rm -rf") == ""


# ---------------------------------------------------------------------------
# API: days=0 (all-time) queries
# ---------------------------------------------------------------------------


def test_fleet_print_analytics_days_zero_returns_200(client: TestClient) -> None:
    r = client.get("/api/v1/fleet/print/analytics?days=0")
    assert r.status_code == 200
    body = r.json()
    assert "total_pages" in body
    assert "total_jobs" in body


def test_fleet_print_export_days_zero(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-z1", [_job(pages=3)]))
    r = client.get("/api/v1/fleet/print/export.csv?days=0")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


def test_fleet_print_days_zero_includes_all_records(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-z2", [{**_job(pages=7), "job_id": 200}]))
    body30 = client.get("/api/v1/fleet/print/analytics?days=30").json()
    body0 = client.get("/api/v1/fleet/print/analytics?days=0").json()
    assert body0["total_pages"] >= body30["total_pages"]


# ---------------------------------------------------------------------------
# API: prev period fields in analytics
# ---------------------------------------------------------------------------


def test_fleet_print_analytics_has_prev_period_fields(client: TestClient) -> None:
    r = client.get("/api/v1/fleet/print/analytics?days=30")
    assert r.status_code == 200
    body = r.json()
    assert "prev_total_pages" in body
    assert "prev_total_jobs" in body
    assert isinstance(body["prev_total_pages"], int)
    assert isinstance(body["prev_total_jobs"], int)


def test_fleet_print_analytics_prev_zero_for_days_zero(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/print/analytics?days=0").json()
    assert body["prev_total_pages"] == 0
    assert body["prev_total_jobs"] == 0


# ---------------------------------------------------------------------------
# API: department on get_device
# ---------------------------------------------------------------------------


def test_device_has_department_field(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-dept1", "inventory", {"hostname": "PC-D1"}))
    body = client.get("/api/v1/devices/dev-dept1").json()
    assert "department" in body
    assert body["department"] is None


def test_patch_meta_department_reflected_in_device(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-dept2", "inventory", {"hostname": "PC-D2"}))
    client.patch("/api/v1/devices/dev-dept2/meta", json={"department": "Finance"})
    body = client.get("/api/v1/devices/dev-dept2").json()
    assert body["department"] == "Finance"


# ---------------------------------------------------------------------------
# Analytics: departments list only contains Без отдела when no depts assigned
# ---------------------------------------------------------------------------


def test_departments_all_без_отдела_when_no_assignments(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-nodept", [_job(pages=5)]))
    body = client.get("/api/v1/fleet/print/analytics?days=30").json()
    depts = body.get("departments", [])
    dept_names = {d["dept"] for d in depts}
    assert dept_names <= {"Без отдела"}, f"unexpected dept names: {dept_names}"


# --------------------------------------------------------------------------- #
# o5 блок C: сбор событий печати в агенте
# --------------------------------------------------------------------------- #


def test_build_script_reads_job_id_from_property_zero() -> None:
    """o5-C2: живой дамп Event 307 (ru-RU): [0]=JobId, [1]=DocumentName."""
    from client.collectors.print_jobs import _build_script

    script = _build_script("")
    assert "[int]$p[0].Value" in script
    assert "$p[1].Value" not in script


def test_build_script_guards_each_event_individually() -> None:
    """o5-C11: один битый Event 307 не должен убивать весь sweep."""
    from client.collectors.print_jobs import _build_script

    script = _build_script("")
    assert "} catch { continue }" in script
    head = script.split("foreach ($e in Get-WinEvent")[0]
    assert not head.rstrip().endswith("try {"), "внешний try вокруг цикла остался"


def test_events_watermark_falls_back_to_local_clock(tmp_path, monkeypatch) -> None:
    """o5-C10 + ревью: основной штамп приходит из PowerShell (`queried_at`, снят ДО
    запроса) -- см. test_events_watermark_comes_from_before_the_query. Здесь пиним
    фолбэк: старый агент/битый ответ без `queried_at` не должен оставить проход без
    знака, знак берётся с локальных часов ПОСЛЕ запроса."""
    import json as _json

    from client.collectors import print_jobs as pj

    clock = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)]

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            return clock[0]

    def _fake_run_ps(_script, timeout=0):
        clock[0] = clock[0] + timedelta(seconds=60)  # запрос длился минуту
        from client.collectors.ps import PsResult

        return PsResult("ok", {"jobs": []})

    monkeypatch.setattr(pj, "datetime", _FakeDatetime)
    monkeypatch.setattr(pj, "run_ps", _fake_run_ps)
    monkeypatch.setattr(pj, "_detect_mode", lambda: "events")

    state_path = tmp_path / "print_state.json"
    pj.collect_print_jobs(state_path, autoenable=False)

    saved = _json.loads(state_path.read_text(encoding="utf-8"))["last_sweep_ts"]
    assert saved == clock[0].isoformat(), "фолбэк не сработал -- проход остался без знака"


def _pjob(job_id: int, ts: str) -> dict:
    return {
        "job_id": job_id,
        "ts": ts,
        "printer": "HP LaserJet",
        "pages": 3,
        "size_bytes": 1000,
        "user_name": "x",
        "source": "events",
    }


def _print_row_count(device_id: str) -> int:
    from server import db

    with db._connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM print_jobs WHERE device_id=?", (device_id,)
            ).fetchone()[0]
        )


def test_recycled_job_id_on_another_day_is_kept(client: TestClient) -> None:
    """Ревью блока C (CRITICAL-1): JobId спулера — счётчик в памяти, он начинается
    заново после перезапуска службы печати/перезагрузки. Глобально-уникальный
    индекс (device_id, job_id) молча выбрасывал ВСЕ задания следующего дня."""
    day1 = "2026-03-01T10:00:00+00:00"
    day2 = "2026-03-02T10:00:00+00:00"
    for ts in (day1, day2):
        client.post(
            "/api/v1/ingest",
            json=envelope(
                "recycle-dev",
                "print_jobs",
                {"jobs": [_pjob(1, ts)], "window_from": None},
            ),
        )
    assert _print_row_count("recycle-dev") == 2, "переиспользованный JobId другого дня потерян"


def test_same_job_id_same_day_is_deduped(client: TestClient) -> None:
    """Дедуп повтора одного и того же прохода обязан сохраниться."""
    for _ in range(2):
        client.post(
            "/api/v1/ingest",
            json=envelope(
                "dedup-dev",
                "print_jobs",
                {"jobs": [_pjob(7, "2026-03-01T10:00:00+00:00")], "window_from": None},
            ),
        )
    assert _print_row_count("dedup-dev") == 1


def test_events_sweep_payload_stays_under_transport_cap(tmp_path, monkeypatch) -> None:
    """Ревью блока C (HIGH-2): -MaxEvents 5000 * ~180 B > _MAX_PAYLOAD_BYTES --
    транспорт отбрасывает ВЕСЬ конверт, а водяной знак уже сдвинут => проход
    потерян навсегда. Проход обязан укладываться в лимит конверта."""
    import json as _json

    from client import transport as tr
    from client.collectors import print_jobs as pj
    from client.collectors.ps import PsResult

    raw = [
        {
            "job_id": i,
            "ts": f"2026-03-01T10:00:{i % 60:02d}.0000000Z",
            "printer": "HP LaserJet Pro MFP M428fdn",
            "pages": 2,
            "size_bytes": 123456,
            "user_name": "CORP-ivanov.i",
        }
        for i in range(5000)
    ]
    monkeypatch.setattr(pj, "_detect_mode", lambda: "events")
    monkeypatch.setattr(
        pj, "run_ps", lambda *a, **k: PsResult("ok", {"jobs": raw, "queried_at": raw[0]["ts"]})
    )

    res = pj.collect_print_jobs(tmp_path / "s.json", autoenable=False)
    size = len(_json.dumps(res.payload, ensure_ascii=False).encode("utf-8"))
    assert size < tr._MAX_PAYLOAD_BYTES, f"конверт прохода {size} B не влезает в лимит"


def test_truncated_sweep_watermark_allows_reading_the_rest(tmp_path, monkeypatch) -> None:
    """Ревью блока C (HIGH-2): при обрезке проход обязан оставлять водяной знак на
    самом свежем СОХРАНЁННОМ задании, иначе обрезанный хвост не дочитается никогда."""
    import json as _json

    from client.collectors import print_jobs as pj
    from client.collectors.ps import PsResult

    # Get-WinEvent отдаёт от новых к старым.
    raw = [
        {"job_id": i, "ts": f"2026-03-01T{23 - i // 250:02d}:00:00.0000000Z", "pages": 1}
        for i in range(5000)
    ]
    monkeypatch.setattr(pj, "_detect_mode", lambda: "events")
    monkeypatch.setattr(
        pj, "run_ps", lambda *a, **k: PsResult("ok", {"jobs": raw, "queried_at": raw[0]["ts"]})
    )

    state_path = tmp_path / "s.json"
    res = pj.collect_print_jobs(state_path, autoenable=False)
    sent = res.payload["jobs"]
    saved = _json.loads(state_path.read_text(encoding="utf-8"))["last_sweep_ts"]

    assert len(sent) < len(raw), "проход не обрезан"
    newest_sent = max(j["ts"] for j in sent)
    assert saved == newest_sent, "знак ушёл за пределы отправленного -- хвост потерян"


def test_events_watermark_comes_from_before_the_query(tmp_path, monkeypatch) -> None:
    """Ревью блока C (MEDIUM-3): знак, поставленный ПОСЛЕ запроса, создаёт дыру --
    задания, напечатанные во время самого запроса, не попадают ни в этот проход,
    ни в следующий. Штамп берётся из PowerShell, до Get-WinEvent."""
    import json as _json

    from client.collectors import print_jobs as pj
    from client.collectors.ps import PsResult

    assert "queried_at" in pj._build_script("")
    before = "2026-03-01T09:59:59.0000000Z"
    monkeypatch.setattr(pj, "_detect_mode", lambda: "events")
    monkeypatch.setattr(
        pj, "run_ps", lambda *a, **k: PsResult("ok", {"jobs": [], "queried_at": before})
    )

    state_path = tmp_path / "s.json"
    pj.collect_print_jobs(state_path, autoenable=False)
    assert _json.loads(state_path.read_text(encoding="utf-8"))["last_sweep_ts"] == before


def test_sweep_cap_holds_for_long_cyrillic_names(tmp_path, monkeypatch) -> None:
    """Ревью блока C (повторное): счёт заданий -- угадывание. 2000 заданий с
    длинными кириллическими именами принтера/пользователя дают ~636 КБ и пробивают
    лимит конверта. Резать надо по измеренному размеру."""
    import json as _json

    from client import transport as tr
    from client.collectors import print_jobs as pj
    from client.collectors.ps import PsResult

    raw = [
        {
            "job_id": i,
            "ts": f"2026-03-01T{i % 24:02d}:00:00.0000000Z",
            "printer": "Демо-отдел — Demo Color LaserJet Enterprise MFP X999dh (кабинет 101)",
            "pages": 2,
            "size_bytes": 1234567,
            "user_name": r"ДЕМОНСТРАЦИЯ-ДОМЕН\Иванова-Сидорова Александра Владимировна",
        }
        for i in range(5000)
    ]
    monkeypatch.setattr(pj, "_detect_mode", lambda: "events")
    monkeypatch.setattr(
        pj, "run_ps", lambda *a, **k: PsResult("ok", {"jobs": raw, "queried_at": raw[0]["ts"]})
    )

    res = pj.collect_print_jobs(tmp_path / "s.json", autoenable=False)
    size = len(_json.dumps(res.payload, ensure_ascii=False).encode("utf-8"))
    assert size < tr._MAX_PAYLOAD_BYTES, f"конверт прохода {size} B не влезает в лимит"
    assert res.payload["jobs"], "проход обрезан в ноль"


def test_print_jobs_are_age_pruned(client: TestClient) -> None:
    """o5-D3: print_jobs росла вечно — единственная таблица телеметрии без
    возрастной обрезки."""
    from server import db

    old = (datetime.now(timezone.utc) - timedelta(days=500)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with db._connect() as conn:
        conn.executemany(
            "INSERT INTO print_jobs (device_id, job_id, ts, received_at, printer, pages)"
            " VALUES (?,?,?,?,?,?)",
            [("prune-dev", 1, old, old, "HP", 1), ("prune-dev", 2, recent, recent, "HP", 1)],
        )
        conn.commit()

    db.prune_aged(heartbeat_raw_days=0, events_raw_days=0, rollup_days=0, print_jobs_raw_days=400)

    with db._connect() as conn:
        left = [
            r[0] for r in conn.execute("SELECT job_id FROM print_jobs WHERE device_id='prune-dev'")
        ]
    assert left == [2]
