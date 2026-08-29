"""Stage 2 of the tray spec (§5): print-page fallback when the event log is off.

Mode is decided EVERY sweep: PrintService/Operational enabled -> "events"
(Event 307, per-job detail); disabled -> "counter" (CIM spooler perf counter
``TotalPagesPrinted`` deltas per queue, reset-aware, no user/document detail).
Rows are tagged ``source`` ("events"|"counter") -- additive contract field.
Transitions must never double-count: entering counter mode reseeds baselines.

Class/property names verified live (2026-06-12): ``_Total`` synthetic instance
present, virtual queues include "Print to Evernote" (new filter entry).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from client.collectors import print_jobs as pj
from client.collectors.ps import PsResult
from fastapi.testclient import TestClient
from shared.schema import parse_payload

from tests.conftest import envelope

# --------------------------------------------------------------------------- #
# _counter_jobs (pure)
# --------------------------------------------------------------------------- #

_SWEEP = "2026-06-12T10:00:00+00:00"


def test_counter_delta_growth() -> None:
    jobs, base = pj._counter_jobs(
        [{"name": "HP LaserJet P1102", "pages": 110}], {"HP LaserJet P1102": 100}, _SWEEP
    )
    assert len(jobs) == 1
    job = jobs[0]
    assert job["pages"] == 10
    assert job["printer"] == "HP LaserJet P1102"
    assert job["job_id"] is None
    assert job["user_name"] is None
    assert job["source"] == "counter"
    assert job["ts"] == _SWEEP
    assert base == {"HP LaserJet P1102": 110}


def test_counter_first_seen_seeds_silently() -> None:
    # First sight of a queue: lifetime pages since spooler start must NOT be
    # emitted as "printed now" -- baseline is seeded, no job.
    jobs, base = pj._counter_jobs([{"name": "Brother HL", "pages": 500}], {}, _SWEEP)
    assert jobs == []
    assert base == {"Brother HL": 500}


def test_counter_spooler_reset_counts_since_restart() -> None:
    # Counter went backwards => spooler restarted; everything since restart is
    # real and uncounted, so the delta equals the current value.
    jobs, base = pj._counter_jobs([{"name": "HP", "pages": 4}], {"HP": 100}, _SWEEP)
    assert len(jobs) == 1
    assert jobs[0]["pages"] == 4
    assert base == {"HP": 4}


def test_counter_zero_delta_no_job() -> None:
    jobs, base = pj._counter_jobs([{"name": "HP", "pages": 100}], {"HP": 100}, _SWEEP)
    assert jobs == []
    assert base == {"HP": 100}


def test_counter_skips_total_and_virtual_queues() -> None:
    queues = [
        {"name": "_Total", "pages": 999},
        {"name": "Microsoft Print to PDF", "pages": 50},
        {"name": "Print to Evernote", "pages": 30},
        {"name": "Fax", "pages": 20},
        {"name": "HP LaserJet", "pages": 10},
    ]
    jobs, base = pj._counter_jobs(queues, {}, _SWEEP)
    assert jobs == []
    assert base == {"HP LaserJet": 10}  # only the physical queue is tracked


def test_counter_ignores_garbage_pages() -> None:
    jobs, base = pj._counter_jobs(
        [{"name": "HP", "pages": "abc"}, {"name": "OK", "pages": 5}], {}, _SWEEP
    )
    assert base == {"OK": 5}
    assert jobs == []


def test_counter_keeps_baseline_of_queue_absent_this_sweep() -> None:
    """Очередь, не попавшая в ОДИН проход, не должна терять базовую линию.

    Пропуск бывает при битой записи CIM, перезапуске спулера или временно
    отключённом принтере. Раньше карта базовых линий заменялась целиком: очередь
    пере-сеедилась молча, и все страницы, напечатанные с прошлого прохода,
    не считались никогда.
    """
    jobs, base = pj._counter_jobs([{"name": "A", "pages": 120}], {"A": 100, "B": 50}, _SWEEP)

    assert [j["pages"] for j in jobs] == [20]
    assert base["A"] == 120
    assert base["B"] == 50, "базовая линия пропавшей очереди потеряна -> страницы не сосчитаны"


def test_counter_baselines_are_capped() -> None:
    """Карта базовых линий ограничена: имена очередей на RDS плодятся без предела
    (перенаправленные принтеры), а state-файл агента не должен расти вечно."""
    old = {f"q{i}": i + 1 for i in range(pj._MAX_BASELINES + 10)}

    _jobs, base = pj._counter_jobs([{"name": "live", "pages": 5}], old, _SWEEP)

    assert len(base) <= pj._MAX_BASELINES
    assert base["live"] == 5, "очередь этого прохода вытеснять нельзя"
    assert "q0" not in base, "вытесняются самые давние, а не свежие"


def test_counter_cap_never_evicts_a_queue_seen_this_sweep() -> None:
    """Ревью (MEDIUM-3): вытеснение по потолку не должно трогать ЖИВЫЕ очереди.

    На терминальном сервере (200 сессий x 3 перенаправленных принтера) живых
    очередей бывает больше потолка. Срезая общий словарь, мы выбрасывали часть
    очередей ЭТОГО прохода: они пере-сеедились на следующем, и страницы
    алфавитно-первых очередей не считались никогда -- отказ детерминированный,
    постоянный и бесшумный.
    """
    live = [{"name": f"live{i:04d}", "pages": 10} for i in range(pj._MAX_BASELINES + 50)]

    _jobs, base = pj._counter_jobs(live, {"старая": 7}, _SWEEP)

    assert all(q["name"] in base for q in live), "живая очередь вытеснена -> пере-сеед"
    assert "старая" not in base, "потолок съеден живыми -- давние не переносим"


def test_counter_failed_enumeration_keeps_baselines() -> None:
    """Ревью (E5): полный отказ CIM отдаёт пустой список очередей -- это НЕ повод
    обнулять точки отсчёта, иначе следующий проход пере-сеедит весь парк."""
    jobs, base = pj._counter_jobs([], {"A": 100, "B": 50}, _SWEEP)

    assert jobs == []
    assert base == {"A": 100, "B": 50}


def test_counter_clips_queue_name_to_contract_cap() -> None:
    """Ревью LOW-2: counter-путь клипает имя очереди так же, как events-путь
    (`_parse_job`), иначе перф-имя >256 роняет весь конверт 422-м на сервере."""
    jobs, base = pj._counter_jobs([{"name": "Q" * 300, "pages": 120}], {"Q" * 256: 100}, _SWEEP)

    assert [j["pages"] for j in jobs] == [20], "клипнутое имя должно джойниться с базовой линией"
    assert all(len(j["printer"]) == 256 for j in jobs)
    assert set(base) == {"Q" * 256}


def test_counter_unreadable_pages_keeps_baseline() -> None:
    """Ревью (MEDIUM-1): нечитаемый счётчик очереди -- НЕ повод считать его нулём.

    Раньше PS отдавал `pages = 0` и когда счётчик прочитать не удалось, и когда он
    честно нулевой. Ноль Python принимал за перезапуск спулера и сбрасывал точку
    отсчёта в 0 -- следующий проход отчитывался за ВЕСЬ пробег принтера с завода
    как за «напечатано сейчас». Теперь такая очередь просто отсутствует в проходе,
    и её базовая линия сохраняется.
    """
    jobs, base = pj._counter_jobs([{"name": "HP", "pages": None}], {"HP": 500}, _SWEEP)

    assert jobs == []
    assert base["HP"] == 500, "нечитаемый счётчик обнулил точку отсчёта"


def test_counter_script_guards_each_queue() -> None:
    """Одна битая очередь не должна обрывать перечисление остальных (§5)."""
    head, tail = pj._COUNTER_SCRIPT.split("foreach ($q in $queues)")
    assert not head.rstrip().endswith("try {"), "внешний try вокруг цикла очередей остался"
    assert "} catch { continue }" in tail
    # Само перечисление CIM -- под своей защитой (класс отсутствует без спулера).
    assert "try { $queues = @(Get-CimInstance" in pj._COUNTER_SCRIPT


def test_counter_script_skips_queue_with_unreadable_counter() -> None:
    """Ревью (MEDIUM-1): нечитаемый счётчик уходит как пропуск, а не как ноль.

    `[long]$null` в PowerShell даёт 0 БЕЗ исключения, поэтому пустое значение
    perf-свойства попадало в тот же путь, что и честный ноль.
    """
    assert "$p = $null" in pj._COUNTER_SCRIPT, "счётчик по-прежнему инициализируется нулём"
    assert "if ($null -eq $p) { continue }" in pj._COUNTER_SCRIPT


def test_counter_script_reports_enumeration_failure() -> None:
    """Ревью (MEDIUM-2): полный отказ CIM обязан быть отличим от «никто не печатал»."""
    assert "ok = $ok" in pj._COUNTER_SCRIPT, "скрипт не сообщает об отказе перечисления"
    assert "$ok = $false" in pj._COUNTER_SCRIPT


# --------------------------------------------------------------------------- #
# Mode detection
# --------------------------------------------------------------------------- #


def test_detect_mode_enabled_log_means_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pj, "run_ps", lambda *a, **k: PsResult("ok", {"enabled": True}))
    assert pj._detect_mode() == "events"


def test_detect_mode_disabled_log_means_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pj, "run_ps", lambda *a, **k: PsResult("ok", {"enabled": False}))
    assert pj._detect_mode() == "counter"


def test_detect_mode_check_failure_falls_back_to_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PS itself broke: keep the OLD behavior (try the events sweep; its own
    # failure path reports the collector as blocked).
    monkeypatch.setattr(pj, "run_ps", lambda *a, **k: PsResult("blocked"))
    assert pj._detect_mode() == "events"


# --------------------------------------------------------------------------- #
# collect_print_jobs end-to-end (run_ps monkeypatched)
# --------------------------------------------------------------------------- #


def _ps_sequence(monkeypatch: pytest.MonkeyPatch, results: list[PsResult]) -> None:
    calls = iter(results)
    monkeypatch.setattr(pj, "run_ps", lambda *a, **k: next(calls))


def test_collect_counter_mode_seeds_then_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "print_state.json"

    # Sweep 1: log disabled, first counter sweep -> seeding, no jobs.
    _ps_sequence(
        monkeypatch,
        [
            PsResult("ok", {"enabled": False}),
            PsResult("ok", {"queues": [{"name": "HP", "pages": 100}]}),
        ],
    )
    res1 = pj.collect_print_jobs(state_path)
    assert res1.payload is not None
    assert res1.payload["jobs"] == []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["mode"] == "counter"
    assert state["baselines"] == {"HP": 100}

    # Sweep 2: counter grew -> delta job emitted and accumulated into daily.
    _ps_sequence(
        monkeypatch,
        [
            PsResult("ok", {"enabled": False}),
            PsResult("ok", {"queues": [{"name": "HP", "pages": 107}]}),
        ],
    )
    res2 = pj.collect_print_jobs(state_path)
    assert res2.payload is not None
    jobs = res2.payload["jobs"]
    assert len(jobs) == 1 and jobs[0]["pages"] == 7 and jobs[0]["source"] == "counter"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["baselines"] == {"HP": 107}
    assert sum(state["daily"].values()) == 7


def test_collect_entering_counter_mode_reseeds_stale_baselines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """events -> counter flap: stale baselines must NOT produce a retro delta.

    Pages printed during the events period were already counted via Event 307;
    a delta against a stale baseline would double-count them.
    """
    state_path = tmp_path / "print_state.json"
    state_path.write_text(
        json.dumps({"mode": "events", "baselines": {"HP": 50}, "last_sweep_ts": _SWEEP}),
        encoding="utf-8",
    )
    _ps_sequence(
        monkeypatch,
        [
            PsResult("ok", {"enabled": False}),
            PsResult("ok", {"queues": [{"name": "HP", "pages": 120}]}),
        ],
    )
    res = pj.collect_print_jobs(state_path)
    assert res.payload is not None
    assert res.payload["jobs"] == []  # reseeded silently, no 70-page ghost
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["baselines"] == {"HP": 120}


def test_collect_events_mode_tags_source_and_flips_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "print_state.json"
    state_path.write_text(json.dumps({"mode": "counter", "baselines": {"HP": 9}}), "utf-8")
    _ps_sequence(
        monkeypatch,
        [
            PsResult("ok", {"enabled": True}),
            PsResult(
                "ok",
                {
                    "jobs": [
                        {
                            "job_id": 7,
                            "ts": _SWEEP,
                            "printer": "HP LaserJet",
                            "pages": 3,
                            "size_bytes": 100,
                            "user_name": "ivanov",
                        }
                    ]
                },
            ),
        ],
    )
    res = pj.collect_print_jobs(state_path)
    assert res.payload is not None
    jobs = res.payload["jobs"]
    assert len(jobs) == 1 and jobs[0]["source"] == "events" and jobs[0]["job_id"] == 7
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["mode"] == "events"


def test_collect_counter_ps_failure_reports_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ps_sequence(
        monkeypatch,
        [PsResult("ok", {"enabled": False}), PsResult("timeout")],
    )
    res = pj.collect_print_jobs(tmp_path / "print_state.json")
    assert res.payload is None
    assert res.source_health  # collector failure surfaced, loop unharmed


def test_collect_counter_cim_failure_is_not_reported_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью (MEDIUM-2): сломанный CIM -- это UNKNOWN, а не «печати не было» (§5).

    Скрипт отрабатывает штатно и отдаёт пустой список очередей, поэтому run_ps
    возвращает "ok". Раньше сборщик безусловно ставил себе status="ok", и
    отсутствующий класс перф-счётчиков (нет службы печати, разрушённый WMI)
    выглядел на сервере ровно как исправная машина, где никто не печатал.
    """
    state_path = tmp_path / "print_state.json"
    _ps_sequence(
        monkeypatch,
        [PsResult("ok", {"enabled": False}), PsResult("ok", {"queues": [], "ok": False})],
    )

    res = pj.collect_print_jobs(state_path, autoenable=False)

    assert res.payload is None
    assert res.source_health[pj.PRINT_JOBS]["status"] == "partial"
    assert not state_path.exists(), "провал перечисления не должен переписывать состояние"


# --------------------------------------------------------------------------- #
# Contract + server storage (additive `source`)
# --------------------------------------------------------------------------- #

pytestmark_integration = pytest.mark.integration


def test_contract_accepts_jobs_with_and_without_source() -> None:
    payload = {
        "jobs": [
            {"job_id": 1, "ts": _SWEEP, "printer": "HP", "pages": 2, "source": "events"},
            {"job_id": None, "ts": _SWEEP, "printer": "HP", "pages": 5, "source": "counter"},
            {"job_id": 2, "ts": _SWEEP, "printer": "HP", "pages": 1},  # old agent
        ]
    }
    parsed = parse_payload("print_jobs", payload)
    dumped = parsed.model_dump()
    assert dumped["jobs"][0]["source"] == "events"
    assert dumped["jobs"][1]["source"] == "counter"
    assert dumped["jobs"][2]["source"] is None


@pytest.mark.integration
def test_ingest_stores_source_and_exports_it(client: TestClient) -> None:
    jobs = [
        {
            "job_id": None,
            "ts": "2026-06-12T10:00:00+00:00",
            "printer": "HP LaserJet",
            "pages": 6,
            "size_bytes": None,
            "user_name": None,
            "source": "counter",
        },
        {
            "job_id": 31,
            "ts": "2026-06-12T10:05:00+00:00",
            "printer": "HP LaserJet",
            "pages": 2,
            "size_bytes": 500,
            "user_name": "petrov",
            "source": "events",
        },
    ]
    r = client.post(
        "/api/v1/ingest",
        json=envelope("dev-fallback", "print_jobs", {"jobs": jobs, "window_from": None}),
    )
    assert r.status_code == 200

    csv_text = client.get("/api/v1/fleet/print/export.csv?days=0").text
    assert "source" in csv_text.splitlines()[0]
    assert "counter" in csv_text
    assert "events" in csv_text


@pytest.mark.integration
def test_counter_rows_with_null_job_id_are_not_deduped(client: TestClient) -> None:
    """Two counter rows (job_id NULL) must both insert -- the UNIQUE index
    guards real Windows job ids only."""
    job = {
        "job_id": None,
        "ts": "2026-06-12T11:00:00+00:00",
        "printer": "HP",
        "pages": 3,
        "source": "counter",
    }
    for _ in range(2):
        r = client.post(
            "/api/v1/ingest",
            json=envelope("dev-dedup", "print_jobs", {"jobs": [job], "window_from": None}),
        )
        assert r.status_code == 200
    stats = client.get("/api/v1/devices/dev-dedup/print?days=0").json()
    assert stats["total_pages"] == 6
    assert stats["total_jobs"] == 2


# --------------------------------------------------------------------------- #
# self-heal: counter mode attempts to enable the operational log
# --------------------------------------------------------------------------- #


def test_counter_mode_attempts_print_log_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pj, "_ENABLE_ATTEMPTED", False)
    monkeypatch.setattr(pj.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    pj._try_enable_print_log()
    pj._try_enable_print_log()  # повторный вызов -- no-op (1 попытка на процесс)
    assert calls == [["wevtutil", "sl", pj._PRINT_LOG, "/e:true"]]


def test_collect_wires_self_heal_only_in_counter_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    sentinel = pj.CollectorResult({"jobs": []}, {})
    monkeypatch.setattr(pj, "_try_enable_print_log", lambda: attempts.append("x"))
    monkeypatch.setattr(pj, "_collect_via_counter", lambda *a, **k: sentinel)
    monkeypatch.setattr(pj, "_collect_via_events", lambda *a, **k: sentinel)

    monkeypatch.setattr(pj, "_detect_mode", lambda: "counter")
    assert pj.collect_print_jobs(tmp_path / "s.json") is sentinel
    assert attempts == ["x"]

    monkeypatch.setattr(pj, "_detect_mode", lambda: "events")
    pj.collect_print_jobs(tmp_path / "s.json")
    assert attempts == ["x"]  # events-режим журнал не трогает

    monkeypatch.setattr(pj, "_detect_mode", lambda: "counter")
    pj.collect_print_jobs(tmp_path / "s.json", autoenable=False)
    assert attempts == ["x"]  # выключенное самолечение уважает решение админа
