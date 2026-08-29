"""B1 (2026-08-27): «Показатели 30 дней» + график событий на странице устройства.

Read-only показ уже собранных посуточных свёрток (heartbeat_rollup_daily /
event_rollup_daily) -- никаких новых запросов к агенту, никаких новых порогов.
``get_event_rollups`` уже существовал (server/pipeline.py:648, tests/test_rollup.py)
-- переиспользован как есть, не переписывался; здесь только пин read-side
контракта + рендер страницы устройства (RU-подписи, свёртка журнала в <details>).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from server import db
from server.analytics.errchain import CRASH

pytestmark = pytest.mark.integration


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _seed_device_with_scores(device_id: str, hostname: str) -> None:
    """Минимальный сид, чтобы s (d.scores) был truthy -- страница не падает на
    day1_factors (тот же приём, что test_device_hero.py::_seed)."""
    ts = datetime.now(timezone.utc).isoformat()
    risk = {"day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []}}
    db.touch_device(device_id, ts, "0.1.0", hostname=hostname)
    db.store_scores(device_id, ts, {"risk": risk})


def _seed_hb_rollup(device_id: str, day: str, **over) -> None:
    row = {
        "n": 1,
        "cpu_p50": None,
        "cpu_p95": None,
        "mem_avail_min": None,
        "pagefile_p95": None,
        "disk_read_ms_p95": None,
        "disk_write_ms_p95": None,
        "disk_queue_p95": None,
        "handles_max": None,
        "free_space_min": None,
        "uptime_max": None,
        "committed_pct_p95": None,
        "nic_errors_max": None,
    }
    row.update(over)
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO heartbeat_rollup_daily (device_id, day, n, cpu_p50, cpu_p95,"
            " mem_avail_min, pagefile_p95, disk_read_ms_p95, disk_write_ms_p95,"
            " disk_queue_p95, handles_max, free_space_min, uptime_max,"
            " committed_pct_p95, nic_errors_max)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                device_id,
                day,
                row["n"],
                row["cpu_p50"],
                row["cpu_p95"],
                row["mem_avail_min"],
                row["pagefile_p95"],
                row["disk_read_ms_p95"],
                row["disk_write_ms_p95"],
                row["disk_queue_p95"],
                row["handles_max"],
                row["free_space_min"],
                row["uptime_max"],
                row["committed_pct_p95"],
                row["nic_errors_max"],
            ),
        )


def _seed_historical(device_id: str, **payload) -> None:
    db.store_historical(device_id, datetime.now(timezone.utc).isoformat(), payload)


def _seed_device_with_trajectory(
    device_id: str, hostname: str, model: str, trajectory: dict
) -> None:
    """B6 6.3: a device with a model (cohort key) + one persisted trend.
    day1_factors seeded empty, same minimal shape as _seed_device_with_scores --
    _device_diagnostics.html needs it truthy or the page 500s on day1_factors."""
    ts = datetime.now(timezone.utc).isoformat()
    risk = {
        "day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []},
        "trajectory": trajectory,
    }
    db.upsert_device(device_id, ts, "0.1.0", hostname=hostname, model=model)
    db.store_scores(device_id, ts, {"risk": risk})


def _worsening_wear(slope: float) -> dict:
    # trend_to_dict() always emits every key (values may be None) -- a dict
    # missing eta_days/target_date would read as Jinja Undefined, and
    # "Undefined is not none" is True (see _disk_smart.html's own warning
    # about this exact trap), so this mirrors the real shape completely.
    return {
        "direction": "worsening",
        "slope_per_day": slope,
        "current": 40.0,
        "eta_days": None,
        "target_date": None,
    }


def _seed_ev_rollup(device_id: str, day: str, event_key: str, n: int) -> None:
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO event_rollup_daily (device_id, day, event_key, n) VALUES (?,?,?,?)",
            (device_id, day, event_key, n),
        )


# --------------------------------------------------------------------------- #
# get_event_rollups -- already existed; one lightweight test pins the
# read-side contract the device page now depends on (fixture rows + days cutoff).
# --------------------------------------------------------------------------- #
def test_get_event_rollups_reads_fixture_and_cuts_by_days(client: TestClient) -> None:
    old_day = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    _seed_ev_rollup("dev-ev", _today(), "System:1001", 2)
    _seed_ev_rollup("dev-ev", old_day, "System:41", 5)
    rows = db.get_event_rollups("dev-ev", 30)
    assert {r["day"]: r["n"] for r in rows} == {_today(): 2}  # 40-дневная строка отсечена


# --------------------------------------------------------------------------- #
# 1.1 -- «Показатели 30 дней»: полоса из 6 карточек со спарклайнами
# --------------------------------------------------------------------------- #
def test_vitals_strip_shows_title_and_six_captions(client: TestClient) -> None:
    _seed_device_with_scores("dev-vit", "PC-VIT")
    _seed_hb_rollup(
        "dev-vit",
        _today(),
        cpu_p95=42.0,
        mem_avail_min=3072.0,
        disk_queue_p95=0.8,
        handles_max=51234,
        free_space_min=15.0,
        uptime_max=48.0,
    )
    body = client.get("/device/dev-vit").text
    assert "Показатели 30 дней" in body
    for caption in (
        "ЦП, p95",
        "Память, мин. свободно",
        "Очередь диска, p95",
        "Дескрипторы, max",
        "Свободно на диске, min",
        "Аптайм, max",
    ):
        assert caption in body


def test_vitals_strip_converts_units_to_gb_and_days(client: TestClient) -> None:
    """Память показывается в ГБ (rollup хранит МБ), аптайм -- в днях (rollup
    хранит ЧАСЫ, не секунды -- проверено по коду rollup: nums('uptime_hours'))."""
    _seed_device_with_scores("dev-unit", "PC-UNIT")
    _seed_hb_rollup("dev-unit", _today(), mem_avail_min=3072.0, uptime_max=48.0)
    body = client.get("/device/dev-unit").text
    assert "3.0" in body and "ГБ" in body  # 3072 МБ / 1024 = 3.0 ГБ
    assert "2.0" in body and re.search(r"2\.0\s*дн", body)  # 48ч / 24 = 2.0 дн


def test_vitals_strip_absent_without_rollup_data(client: TestClient) -> None:
    """UNKNOWN over false confidence: нет свёрнутых дней -> нет полосы (не 0/—).
    Проверяем сам маркап карточек, а не заголовок -- он же встречается в CSS-
    комментарии extra_head, который рендерится независимо от данных."""
    _seed_device_with_scores("dev-norollup", "PC-NOROLLUP")
    body = client.get("/device/dev-norollup").text
    assert 'class="vitals-grid"' not in body


def test_srp_spark_helper_declared_once(client: TestClient) -> None:
    _seed_device_with_scores("dev-spark", "PC-SPARK")
    _seed_hb_rollup("dev-spark", _today(), cpu_p95=10.0)
    body = client.get("/device/dev-spark").text
    assert body.count("function srpSpark(") == 1


# --------------------------------------------------------------------------- #
# 1.2 -- график «События в день» + свёртка «Недавние события» в <details>
# --------------------------------------------------------------------------- #
def test_event_chart_label_present(client: TestClient) -> None:
    _seed_device_with_scores("dev-evc", "PC-EVC")
    _seed_ev_rollup("dev-evc", _today(), "System:1001", 3)
    body = client.get("/device/dev-evc").text
    assert "События в день" in body


def test_event_chart_absent_without_rollup_data(client: TestClient) -> None:
    _seed_device_with_scores("dev-noevc", "PC-NOEVC")
    body = client.get("/device/dev-noevc").text
    assert "События в день" not in body


def test_event_chart_reuses_errchain_crash_ids_not_a_new_list(client: TestClient) -> None:
    """BSOD/KP41 -- переиспользуем server.analytics.errchain.CRASH (единый источник
    правды с engine-стороной), не заводим второй список в шаблоне/JS."""
    _seed_device_with_scores("dev-crash", "PC-CRASH")
    _seed_ev_rollup("dev-crash", _today(), "System:1001", 1)
    body = client.get("/device/dev-crash").text
    m = re.search(r"CRASH_IDS\s*=\s*(\[[^\]]*\])", body)
    assert m, "CRASH_IDS literal not found in rendered page"
    assert json.loads(m.group(1)) == sorted(CRASH)


def test_recent_events_wrapped_in_details_with_count(client: TestClient) -> None:
    _seed_device_with_scores("dev-det", "PC-DET")
    body = client.get("/device/dev-det").text
    assert "<summary>Журнал событий (0)</summary>" in body
    start = body.index("Журнал событий")
    details_open = body.rindex("<details", 0, start)
    details_close = body.index("</details>", start)
    assert details_open < start < details_close


# --------------------------------------------------------------------------- #
# Ревью B1 (2026-08-27) -- регресс-тесты на подтверждённые находки.
# --------------------------------------------------------------------------- #
def test_event_chart_defines_srp_hover_label_when_only_event_rollups(client: TestClient) -> None:
    """HIGH-регресс: _plotly_hover.html (объявляет srpHoverLabel) раньше подключался
    только под heartbeat_rollups/health_series. У устройства с event_rollups, но без
    обеих (тот же сид, что test_event_chart_label_present -- ровно этот сценарий),
    draw() звал необъявленную srpHoverLabel() -> ReferenceError, график не рисовался."""
    _seed_device_with_scores("dev-evonly", "PC-EVONLY")
    _seed_ev_rollup("dev-evonly", _today(), "System:1001", 1)
    body = client.get("/device/dev-evonly").text
    assert "function srpHoverLabel(" in body


def test_vitals_sparklines_clip_to_30_days_matching_header(client: TestClient) -> None:
    """MEDIUM-регресс: #hb-rollup-data несёт 90 дней (общие с графиком латентности
    диска), а полоса подписана «Показатели 30 дней» -- без обрезки спарклайн рисовал
    все 90 точек под 30-дневным заголовком."""
    _seed_device_with_scores("dev-clip", "PC-CLIP")
    _seed_hb_rollup("dev-clip", _today(), cpu_p95=10.0)
    body = client.get("/device/dev-clip").text
    assert ".slice(0, 30).reverse()" in body


def test_srp_spark_hover_text_is_escaped(client: TestClient) -> None:
    """LOW-регресс: общий срарклайн-хелпер (задокументирован как переиспользуемый для
    будущих посуточных карточек, напр. история SMART -- план B4) собирал hover-текст
    в HTML-синк Plotly без srpEsc."""
    _seed_device_with_scores("dev-esc", "PC-ESC")
    _seed_hb_rollup("dev-esc", _today(), cpu_p95=10.0)
    body = client.get("/device/dev-esc").text
    assert 'window.srpEsc(d) + ": " + window.srpEsc(y[i])' in body


def test_event_key_parses_last_colon_segment(client: TestClient) -> None:
    """LOW-регресс: split(":")[1] брал ВТОРОЙ элемент -- если имя провайдера журнала
    (source) само содержит ":", event_id съезжал и BSOD/KP41-день мог не подсветиться."""
    _seed_device_with_scores("dev-key", "PC-KEY")
    _seed_ev_rollup("dev-key", _today(), "System:1001", 1)
    body = client.get("/device/dev-key").text
    assert 'split(":").pop()' in body


def test_event_chart_bad_fallback_matches_real_token(client: TestClient) -> None:
    """LOW-регресс: аварийный fallback --bad ("#f43f5e") не совпадал ни с одним
    реальным значением токена (base.html: #ffe000 тёмная тема по умолчанию /
    #e41e3f meta) -- недостижим на практике, но должен совпадать при срабатывании."""
    _seed_device_with_scores("dev-fb", "PC-FB")
    _seed_ev_rollup("dev-fb", _today(), "System:1001", 1)
    body = client.get("/device/dev-fb").text
    assert '"--bad").trim() || "#ffe000"' in body


# --------------------------------------------------------------------------- #
# B6 6.1 -- «Выделено памяти» (always) / «Ошибки сети» (only if >0 in window)
# --------------------------------------------------------------------------- #
def test_committed_pct_card_present(client: TestClient) -> None:
    _seed_device_with_scores("dev-committed", "PC-COMMITTED")
    _seed_hb_rollup("dev-committed", _today(), committed_pct_p95=77.0)
    body = client.get("/device/dev-committed").text
    assert "Выделено памяти" in body
    assert "77%" in body


def test_nic_errors_card_present_when_window_has_a_positive_value(client: TestClient) -> None:
    _seed_device_with_scores("dev-nic", "PC-NIC")
    _seed_hb_rollup("dev-nic", _today(), nic_errors_max=0)
    _seed_hb_rollup(
        "dev-nic",
        (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
        nic_errors_max=3,
    )
    body = client.get("/device/dev-nic").text
    assert "Ошибки сети" in body


def test_nic_errors_card_absent_when_window_is_all_zero_or_null(client: TestClient) -> None:
    """UNKNOWN over false confidence -- никаких сетевых ошибок в окне не значит
    «ноль», это значит «нечего показывать», и карточка не рендерится вовсе."""
    _seed_device_with_scores("dev-nonic", "PC-NONIC")
    _seed_hb_rollup("dev-nonic", _today(), nic_errors_max=0, cpu_p95=10.0)
    _seed_hb_rollup(
        "dev-nonic",
        (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
        nic_errors_max=None,
    )
    body = client.get("/device/dev-nonic").text
    assert "Ошибки сети" not in body


# --------------------------------------------------------------------------- #
# B6 6.2 -- WHEA-чип рядом с графиком событий, только при N>0
# --------------------------------------------------------------------------- #
def test_whea_chip_present_when_positive(client: TestClient) -> None:
    # hostname deliberately does NOT contain "WHEA" -- an earlier draft of this
    # test used "PC-WHEA" and passed vacuously (the substring came from the
    # hostname itself, not the chip) [[vacuous-test-classes]].
    _seed_device_with_scores("dev-whea", "PC-HW1")
    _seed_historical("dev-whea", whea_errors_30d=4)
    body = client.get("/device/dev-whea").text
    assert "WHEA-ошибок за 30 дн: 4" in body


def test_whea_chip_absent_when_zero(client: TestClient) -> None:
    _seed_device_with_scores("dev-whea0", "PC-WHEA0")
    _seed_historical("dev-whea0", whea_errors_30d=0)
    body = client.get("/device/dev-whea0").text
    assert "WHEA-ошибок" not in body


def test_whea_chip_absent_when_absent(client: TestClient) -> None:
    """Поле не пришло от агента вовсе -- .get() must not invent 0."""
    _seed_device_with_scores("dev-whea-none", "PC-WHEANONE")
    _seed_historical("dev-whea-none", avg_boot_ms=20000)  # historical есть, whea -- нет
    body = client.get("/device/dev-whea-none").text
    assert "WHEA-ошибок" not in body


# --------------------------------------------------------------------------- #
# B6 6.3 -- «ухудшается быстрее, чем у большинства похожих машин» в блоке
# прогноза, по перцентилю slope в когорте той же модели.
# --------------------------------------------------------------------------- #
def test_cohort_slope_note_shown_when_worse_than_most_peers(client: TestClient) -> None:
    model = "CohortModelFast"
    for i, slope in enumerate([0.1, 0.1, 0.1]):  # все 3 медленнее наблюдаемого устройства
        _seed_device_with_trajectory(
            f"peer-fast-{i}", f"PC-PEERF{i}", model, {"storage_wear": _worsening_wear(slope)}
        )
    _seed_device_with_trajectory(
        "dev-cohort-fast", "PC-COHORTFAST", model, {"storage_wear": _worsening_wear(5.0)}
    )
    body = client.get("/device/dev-cohort-fast").text
    assert "Износ SSD ухудшается быстрее, чем у большинства похожих машин" in body


def test_cohort_slope_note_absent_when_cohort_too_thin(client: TestClient) -> None:
    """Меньше _COHORT_SLOPE_MIN_N сравнимых точек в когорте -> нет сигнала,
    строка не рендерится (UNKNOWN over false confidence)."""
    _seed_device_with_trajectory(
        "dev-thin", "PC-THIN", "LonelyModel", {"storage_wear": _worsening_wear(5.0)}
    )
    body = client.get("/device/dev-thin").text
    assert "ухудшается быстрее, чем у большинства похожих машин" not in body


def test_cohort_slope_note_absent_when_not_worse_than_majority(client: TestClient) -> None:
    model = "CohortModelSlow"
    for i, slope in enumerate([5.0, 5.0, 5.0]):  # все 3 быстрее наблюдаемого устройства
        _seed_device_with_trajectory(
            f"peer-slow-{i}", f"PC-PEERS{i}", model, {"storage_wear": _worsening_wear(slope)}
        )
    _seed_device_with_trajectory(
        "dev-cohort-slow", "PC-COHORTSLOW", model, {"storage_wear": _worsening_wear(0.1)}
    )
    body = client.get("/device/dev-cohort-slow").text
    assert "ухудшается быстрее, чем у большинства похожих машин" not in body


def test_cohort_slope_note_style_present_when_first_metric_has_no_trend(
    client: TestClient,
) -> None:
    """MEDIUM (B6 review): the one-time <style> block used to be gated on
    `loop.first` set INSIDE `{% if t %}` (device.html) -- true only when the
    FIRST tmeta row (storage_wear) itself had trajectory data. Trajectory here
    deliberately omits storage_wear and worsens disk_fill (the 2nd tmeta row)
    instead, so the note renders on a non-first iteration: the bug's exact
    trigger. Before the fix .traj-cohort-note's div appeared with no CSS rule
    anywhere on the page.
    """
    model = "CohortModelStyleGap"
    for i, slope in enumerate([0.1, 0.1, 0.1]):  # все 3 медленнее наблюдаемого устройства
        _seed_device_with_trajectory(
            f"peer-style-{i}", f"PC-PEERSTYLE{i}", model, {"disk_fill": _worsening_wear(slope)}
        )
    _seed_device_with_trajectory(
        "dev-cohort-style", "PC-COHORTSTYLE", model, {"disk_fill": _worsening_wear(5.0)}
    )
    body = client.get("/device/dev-cohort-style").text
    assert "Заполнение диска ухудшается быстрее, чем у большинства похожих машин" in body
    # the class ATTRIBUTE has no leading dot -- only the CSS rule does, so this
    # substring can only match if the <style> block itself was emitted.
    assert ".traj-cohort-note" in body


# --------------------------------------------------------------------------- #
# 11b: строка «Троттлинг CPU» печатала СЫРОЙ cpu_perf_pct (доля номинала, где
# больше = лучше), а полосу рисовала недобором -- «99%» при пустой полосе.
# Число и полоса обязаны говорить одно и то же.
# --------------------------------------------------------------------------- #
def _throttle_trend(current: float) -> dict:
    return {
        "direction": "worsening",
        "slope_per_day": -0.4,
        "current": current,
        "eta_days": None,
        "target_date": None,
    }


def test_throttle_row_prints_the_deficit_not_the_raw_nominal(client: TestClient) -> None:
    _seed_device_with_trajectory(
        "dev-throttle", "PC-THROTTLE", "ThrottleModel", {"throttle": _throttle_trend(97.0)}
    )
    body = client.get("/device/dev-throttle").text
    row = body[body.index("Троттлинг CPU") :][:600]
    assert "3%" in row
    assert "97%" not in row


def test_throttle_row_clamps_turbo_boost_to_zero(client: TestClient) -> None:
    """Турбобуст даёт >100% номинала: недобор отрицательным быть не может."""
    _seed_device_with_trajectory(
        "dev-turbo", "PC-TURBO", "TurboModel", {"throttle": _throttle_trend(104.0)}
    )
    body = client.get("/device/dev-turbo").text
    row = body[body.index("Троттлинг CPU") :][:600]
    assert "0%" in row
    assert "-4%" not in row
