"""B4 (2026-08-27): «Диски (SMART)» -- показ уже собранных hist.storage данных
и их истории (disk_readings/get_disk_series) на странице устройства.

Read-only: никаких новых сборщиков/полей контракта, только чтение уже
хранимого StorageReliability (shared/schema.py:114-147) через hist.storage
(server/db.py::get_device -> _latest_historical) и disk_readings
(server/db.py::get_disk_series, до этой ветки не читался ни одним роутом).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from server import db
from server.web import dashboard

pytestmark = pytest.mark.integration


def _seed_device_with_scores(device_id: str, hostname: str) -> None:
    """Минимальный сид, чтобы s (d.scores) был truthy -- иначе весь блок
    диагностики (в т.ч. «Диски (SMART)») не рендерится (тот же приём, что
    test_device_vitals.py::_seed_device_with_scores)."""
    ts = datetime.now(timezone.utc).isoformat()
    risk = {"day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []}}
    db.touch_device(device_id, ts, "0.1.0", hostname=hostname)
    db.store_scores(device_id, ts, {"risk": risk})


def _seed_storage(device_id: str, disks: list[dict]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    db.store_historical(device_id, ts, {"storage": disks})


_ONE_DISK = {
    "disk": "Samsung SSD 970 EVO 1TB",
    "media_type": "SSD",
    "bus_type": "NVMe",
    "serial_hash": "a1b2c3d4e5f6a1b2",
    "temperature_c": 45,
    "wear_pct": 12.0,
    "power_on_hours": 9000,
}


# --------------------------------------------------------------------------- #
# 4.1 -- карточка «Диски (SMART)»
# --------------------------------------------------------------------------- #
def test_disk_smart_section_shows_model_and_current_values(client: TestClient) -> None:
    _seed_device_with_scores("dev-disk", "PC-DISK")
    _seed_storage("dev-disk", [_ONE_DISK])
    body = client.get("/device/dev-disk").text
    assert "Диски (SMART)" in body
    assert "Samsung SSD 970 EVO 1TB" in body
    assert "45°C" in body
    assert "12%" in body


def test_disk_smart_section_absent_without_storage_data(client: TestClient) -> None:
    """UNKNOWN over false confidence: без hist.storage -- блок отсутствует
    целиком, не «0°C»/фиктивная карточка."""
    _seed_device_with_scores("dev-nodisk", "PC-NODISK")
    body = client.get("/device/dev-nodisk").text
    assert "Диски (SMART)" not in body
    assert "0°C" not in body


def test_disk_smart_non_numeric_fields_do_not_crash_page(client: TestClient) -> None:
    """HIGH (review B1-B5 final): historical is the RAW envelope dict
    (pipeline.py passes env.payload straight to db.store_historical, no
    pydantic coercion) -- a legit numeric STRING (e.g. "temperature_c": "75")
    used to reach disk_temp_color()'s raw Python `>` comparison and Jinja's
    own `pending > 0` checks, crashing the WHOLE device page with an unhandled
    500 (recurrence of the link_mbps bug class fixed for _device_net.html in
    review B5, commit 1f74401 -- there is no global FastAPI handler for
    template errors)."""
    device_id = "dev-strnum"
    _seed_device_with_scores(device_id, "PC-STRNUM")
    _seed_storage(
        device_id,
        [
            {
                **_ONE_DISK,
                "temperature_c": "75",
                "wear_pct": "12.0",
                "power_on_hours": "9000",
                "reallocated_sectors": "3",
                "smart_attrs": {"197": "5", "198": "0"},
                "read_errors_uncorrected": "1",
                "write_errors_uncorrected": "0",
            }
        ],
    )
    resp = client.get(f"/device/{device_id}")
    assert resp.status_code == 200
    assert "75°C" in resp.text
    assert "12%" in resp.text
    assert "ожидают переназначения: 5" in resp.text
    assert "переназначено: 3" in resp.text


def test_disk_smart_section_notes_overlap_with_attention_factors(client: TestClient) -> None:
    """MEDIUM (review B1-B5 final): the same current values (temp/wear/POH/
    pending/realloc/predict_fail) can already appear above under «Требует
    внимания» once they cross a risk threshold -- a short note explains the
    overlap instead of it reading as an unexplained repeat."""
    _seed_device_with_scores("dev-notecap", "PC-NOTECAP")
    _seed_storage("dev-notecap", [_ONE_DISK])
    body = client.get("/device/dev-notecap").text
    assert "уже могла войти в «Требует внимания»" in body


def test_disk_smart_predict_fail_shows_ru_badge(client: TestClient) -> None:
    _seed_device_with_scores("dev-predict", "PC-PREDICT")
    _seed_storage("dev-predict", [{**_ONE_DISK, "smart_predict_fail": True}])
    body = client.get("/device/dev-predict").text
    assert "SMART: предсказан отказ" in body


def test_disk_smart_no_predict_badge_when_false(client: TestClient) -> None:
    _seed_device_with_scores("dev-nopredict", "PC-NOPREDICT")
    _seed_storage("dev-nopredict", [{**_ONE_DISK, "smart_predict_fail": False}])
    body = client.get("/device/dev-nopredict").text
    assert "SMART: предсказан отказ" not in body


def test_disk_smart_no_errors_claim_absent_when_no_error_fields_reported(
    client: TestClient,
) -> None:
    """UNKNOWN over false confidence: pending/realloc/uncorrected вообще не
    пришли (не 0, а именно отсутствуют) -- «ошибок нет» заявлять нельзя."""
    _seed_device_with_scores("dev-noerrdata", "PC-NOERRDATA")
    _seed_storage("dev-noerrdata", [_ONE_DISK])
    body = client.get("/device/dev-noerrdata").text
    assert "Диски (SMART)" in body
    assert "ошибок нет" not in body


def test_disk_smart_errors_absent_shows_no_errors_label(client: TestClient) -> None:
    """«ошибок нет» требует, чтобы ВСЕ ЧЕТЫРЕ счётчика реально пришли и были
    нулевыми -- сид ниже называет их все явно (B4-review MEDIUM fix; раньше
    для этого чипа хватало одного известного поля, см. следующий тест)."""
    _seed_device_with_scores("dev-noerr", "PC-NOERR")
    _seed_storage(
        "dev-noerr",
        [
            {
                **_ONE_DISK,
                "reallocated_sectors": 0,
                "read_errors_uncorrected": 0,
                "write_errors_uncorrected": 0,
                "smart_attrs": {"197": 0, "198": 0},
            }
        ],
    )
    body = client.get("/device/dev-noerr").text
    assert "ошибок нет" in body


def test_disk_smart_no_errors_claim_absent_when_only_some_counters_known(
    client: TestClient,
) -> None:
    """B4-review MEDIUM: один известный счётчик (reallocated_sectors=0) не
    даёт права заявлять «ошибок нет» -- pending/uncorrectable(attr 198)/
    uncorr остаются НЕИЗВЕСТНЫ (UNKNOWN over false confidence). Раньше
    ЛЮБОГО ОДНОГО известного поля хватало, и этот же сид ошибочно закреплял
    зелёный чип."""
    _seed_device_with_scores("dev-partialerr", "PC-PARTIALERR")
    _seed_storage("dev-partialerr", [{**_ONE_DISK, "reallocated_sectors": 0}])
    body = client.get("/device/dev-partialerr").text
    assert "ошибок нет" not in body


def test_disk_smart_uncorrectable_198_shown_on_par_with_uncorrected(
    client: TestClient,
) -> None:
    """B4-review MEDIUM: attr 198 (неисправимые секторы -- storage.py
    начисляет за него 60 очков «D») раньше в блоке ошибок не выводился
    вовсе, поэтому диск с attr198>0 и realloc=0 показывал зелёное «ошибок
    нет». Теперь у него свой чип, наравне с read/write_errors_uncorrected."""
    _seed_device_with_scores("dev-attr198", "PC-ATTR198")
    _seed_storage(
        "dev-attr198", [{**_ONE_DISK, "reallocated_sectors": 0, "smart_attrs": {"198": 3}}]
    )
    body = client.get("/device/dev-attr198").text
    assert "неисправимых секторов: 3" in body
    assert "ошибок нет" not in body


def test_disk_smart_realloc_error_shown_with_count(client: TestClient) -> None:
    _seed_device_with_scores("dev-realloc", "PC-REALLOC")
    _seed_storage("dev-realloc", [{**_ONE_DISK, "reallocated_sectors": 5}])
    body = client.get("/device/dev-realloc").text
    assert "переназначено: 5" in body
    assert "ошибок нет" not in body


def test_disk_smart_pending_sectors_read_from_smart_attrs_197(client: TestClient) -> None:
    _seed_device_with_scores("dev-pending", "PC-PENDING")
    _seed_storage("dev-pending", [{**_ONE_DISK, "smart_attrs": {"197": 15}}])
    body = client.get("/device/dev-pending").text
    assert "ожидают переназначения: 15" in body


def test_disk_smart_uncorrected_sums_read_and_write(client: TestClient) -> None:
    _seed_device_with_scores("dev-uncorr", "PC-UNCORR")
    _seed_storage(
        "dev-uncorr",
        [{**_ONE_DISK, "read_errors_uncorrected": 2, "write_errors_uncorrected": 3}],
    )
    body = client.get("/device/dev-uncorr").text
    assert "неисправлено чтение/запись: 5" in body


def test_disk_smart_all_smart_attrs_in_details(client: TestClient) -> None:
    _seed_device_with_scores("dev-attrs", "PC-ATTRS")
    _seed_storage("dev-attrs", [{**_ONE_DISK, "smart_attrs": {"5": 0, "197": 0, "198": 0}}])
    body = client.get("/device/dev-attrs").text
    assert "<summary>Все атрибуты SMART (3)</summary>" in body


def test_disk_smart_bus_type_visible_slow_disk_is_usb(client: TestClient) -> None:
    # хостнейм НЕ содержит "USB" -- иначе строка совпадает с ним, а не с bus_type
    # (найдено живым прогоном: PC-USB давал ложный «зелёный» до реализации).
    _seed_device_with_scores("dev-busext", "PC-EXTDRIVE")
    _seed_storage("dev-busext", [{**_ONE_DISK, "bus_type": "USB"}])
    body = client.get("/device/dev-busext").text
    assert "USB" in body


def test_disk_smart_missing_wear_shows_dash_not_invented_zero(client: TestClient) -> None:
    """NVMe-диск без wear_pct (не все накопители его отдают) -- поле «—», не 0%."""
    _seed_device_with_scores("dev-nowear", "PC-NOWEAR")
    disk = {k: v for k, v in _ONE_DISK.items() if k != "wear_pct"}
    _seed_storage("dev-nowear", [disk])
    body = client.get("/device/dev-nowear").text
    assert "Диски (SMART)" in body
    # без данных метрика -- нейтральный «—», а не выдуманный "0%" (подстрока
    # "0%" сама по себе ловит несвязанное "100%" из CSS/тултипов на странице,
    # поэтому целимся в конкретную отрисовку пустого значения износа)
    assert '<div class="disk-m-v na">—</div>' in body


def test_disk_wear_color_nvme_percentage_used_only_stays_good_below_85(
    client: TestClient,
) -> None:
    """B4-review MEDIUM: storage.py's ssd3 rule only starts scoring
    nvme_percentage_used at >85 (storage.py:381-387) -- a value of 75 with NO
    wear_pct scores ZERO risk points there. Painting it .warn (the legacy
    wear_pct cutoff of >70) invents a threshold the engine doesn't have."""
    device_id = "dev-nvmewear75"
    _seed_device_with_scores(device_id, "PC-NVMEWEAR75")
    disk = {k: v for k, v in _ONE_DISK.items() if k != "wear_pct"}
    _seed_storage(device_id, [{**disk, "nvme_percentage_used": 75}])
    body = client.get(f"/device/{device_id}").text
    assert '<div class="disk-m-v good">75%</div>' in body


def test_disk_wear_color_nvme_percentage_used_warns_past_85(client: TestClient) -> None:
    """The ssd3 rule DOES fire past 85 -- the UI must still warn there."""
    device_id = "dev-nvmewear90"
    _seed_device_with_scores(device_id, "PC-NVMEWEAR90")
    disk = {k: v for k, v in _ONE_DISK.items() if k != "wear_pct"}
    _seed_storage(device_id, [{**disk, "nvme_percentage_used": 90}])
    body = client.get(f"/device/{device_id}").text
    assert '<div class="disk-m-v warn">90%</div>' in body


def test_disk_wear_color_legacy_wear_pct_still_warns_at_70(client: TestClient) -> None:
    """Regression pin: the legacy wear_pct-only path keeps its OWN >70 warn
    cutoff (storage.py:260-267 scores 12pts there) -- only the
    nvme_percentage_used-only path moved to 85."""
    device_id = "dev-wearpct75"
    _seed_device_with_scores(device_id, "PC-WEARPCT75")
    _seed_storage(device_id, [{**_ONE_DISK, "wear_pct": 75}])
    body = client.get(f"/device/{device_id}").text
    assert '<div class="disk-m-v warn">75%</div>' in body


def test_disk_wear_sparkline_formula_takes_max_like_header(client: TestClient) -> None:
    """HIGH (review B1-B5 final): the header's «Износ» value takes
    max(wear_pct, nvme_percentage_used) (wear_vals|max, mirrored by
    disk_wear_color's own docstring) -- the sparkline JS instead preferred
    wear_pct whenever present and IGNORED a higher nvme_percentage_used, so a
    disk reporting both fields (normal co-existence for Ф1 NVMe, per
    storage.py:378-380) could show a header number its own sparkline line
    directly contradicted."""
    device_id = "dev-wearmismatch"
    _seed_device_with_scores(device_id, "PC-WEARMISMATCH")
    disk = {**_ONE_DISK, "wear_pct": 10.0, "nvme_percentage_used": 90.0}
    _seed_storage(device_id, [disk])
    now = datetime.now(timezone.utc).isoformat()
    db.store_disk_readings(device_id, [disk], now, now)
    body = client.get(f"/device/{device_id}").text
    # header: max(10, 90) = 90 -> nvme rule's >85 warn tier
    assert '<div class="disk-m-v warn">90%</div>' in body
    # sparkline JS formula must take the max too, not prefer wear_pct outright
    assert "return Math.max(w, n);" in body
    assert "r.wear_pct != null ? r.wear_pct : r.nvme_percentage_used" not in body


def test_disk_smart_poh_shown_in_years_and_hours(client: TestClient) -> None:
    _seed_device_with_scores("dev-poh", "PC-POH")
    _seed_storage("dev-poh", [{**_ONE_DISK, "power_on_hours": 9860}])  # 8760+1100
    body = client.get("/device/dev-poh").text
    assert "1 г 1100 ч" in body


def test_disk_smart_poh_under_one_year_shows_hours_only(client: TestClient) -> None:
    _seed_device_with_scores("dev-pohsmall", "PC-POHSMALL")
    _seed_storage("dev-pohsmall", [{**_ONE_DISK, "power_on_hours": 500}])
    body = client.get("/device/dev-pohsmall").text
    assert "500 ч" in body


def test_disk_smart_serial_hash_not_leaked_to_html(client: TestClient) -> None:
    device_id = "dev-priv"
    _seed_device_with_scores(device_id, "PC-PRIV")
    _seed_storage(device_id, [_ONE_DISK])
    now = datetime.now(timezone.utc).isoformat()
    db.store_disk_readings(device_id, [_ONE_DISK], now, now)
    body = client.get(f"/device/{device_id}").text
    assert _ONE_DISK["serial_hash"] not in body


# --------------------------------------------------------------------------- #
# 4.2 -- disk_series (спарклайны истории) прокинут в шаблон
# --------------------------------------------------------------------------- #
def test_disk_series_embedded_as_json_for_sparklines(client: TestClient) -> None:
    device_id = "dev-series"
    _seed_device_with_scores(device_id, "PC-SERIES")
    _seed_storage(device_id, [_ONE_DISK])
    now = datetime.now(timezone.utc).isoformat()
    db.store_disk_readings(device_id, [{**_ONE_DISK, "temperature_c": 40}], now, now)
    db.store_disk_readings(device_id, [{**_ONE_DISK, "temperature_c": 44}], now, now)
    body = client.get(f"/device/{device_id}").text
    m = re.search(r'<script id="disk-series-data"[^>]*>(.*?)</script>', body, re.S)
    assert m, "disk-series-data script tag not found"
    payload = json.loads(m.group(1))
    assert "0" in payload
    temps = [row["temperature_c"] for row in payload["0"]]
    assert 40 in temps and 44 in temps


def test_disk_series_points_capped(client: TestClient) -> None:
    """B4-review MEDIUM: get_disk_series must be called with an explicit
    small limit -- a 36px sparkline never needed the DB's own default of 200
    points. Seed well past the cap and confirm the embedded JSON stays
    capped at it."""
    device_id = "dev-manyreadings"
    _seed_device_with_scores(device_id, "PC-MANYREADINGS")
    _seed_storage(device_id, [_ONE_DISK])
    now = datetime.now(timezone.utc).isoformat()
    for _ in range(dashboard._DISK_HISTORY_POINTS_MAX + 20):
        db.store_disk_readings(device_id, [_ONE_DISK], now, now)
    body = client.get(f"/device/{device_id}").text
    m = re.search(r'<script id="disk-series-data"[^>]*>(.*?)</script>', body, re.S)
    assert m
    payload = json.loads(m.group(1))
    assert len(payload["0"]) == dashboard._DISK_HISTORY_POINTS_MAX


def test_disk_series_disk_count_capped(client: TestClient) -> None:
    """B4-review MEDIUM: a single ingest envelope can carry up to
    STORAGE_DISKS_MAX (64) disks -- capping how many get a per-disk history
    query keeps one render from opening dozens of DB connections."""
    device_id = "dev-manydisks"
    _seed_device_with_scores(device_id, "PC-MANYDISKS")
    n = dashboard._DISK_HISTORY_DISKS_MAX + 5
    disks = [{**_ONE_DISK, "serial_hash": f"serial-{i:02d}", "disk": f"Disk {i}"} for i in range(n)]
    _seed_storage(device_id, disks)
    body = client.get(f"/device/{device_id}").text
    m = re.search(r'<script id="disk-series-data"[^>]*>(.*?)</script>', body, re.S)
    assert m
    payload = json.loads(m.group(1))
    assert len(payload) == dashboard._DISK_HISTORY_DISKS_MAX


def test_disk_spark_slots_loop_over_all_disks_not_just_series_keys(
    client: TestClient,
) -> None:
    """B4-review LOW: a disk beyond the history cap (no disk_series entry)
    must still get its sparkline JS call -- srpSpark's own "мало данных"
    placeholder only fires when it actually RUNS on that slot. The old
    Object.keys(bySlot) loop skipped slots with no series entry outright,
    leaving a bare empty 36px block under the "Темп., история" label."""
    device_id = "dev-sparkloop"
    _seed_device_with_scores(device_id, "PC-SPARKLOOP")
    n = dashboard._DISK_HISTORY_DISKS_MAX + 2
    disks = [{**_ONE_DISK, "serial_hash": f"serial-{i:02d}", "disk": f"Disk {i}"} for i in range(n)]
    _seed_storage(device_id, disks)
    body = client.get(f"/device/{device_id}").text
    # the disk past the history cap still gets a spark container in the DOM...
    assert f'id="disk-spark-temp-{n - 1}"' in body
    # ...and the JS driving it iterates by disk COUNT, not by the (capped,
    # therefore shorter) set of keys the server actually queried history for.
    assert f"var diskCount = {n};" in body


def test_disk_series_reuses_existing_srp_spark_not_a_second_copy(client: TestClient) -> None:
    """B1 задокументировал srpSpark как общий переиспользуемый хелпер --
    подтверждаем, что B4 не завёл вторую копию (тот же счётчик, что и в
    test_device_vitals.py::test_srp_spark_helper_declared_once)."""
    _seed_device_with_scores("dev-onespark", "PC-ONESPARK")
    _seed_storage("dev-onespark", [_ONE_DISK])
    body = client.get("/device/dev-onespark").text
    assert body.count("function srpSpark(") == 1


def test_disk_series_present_even_without_heartbeat_rollup(client: TestClient) -> None:
    """Регресс на область видимости srpSpark: она объявлена в _device_vitals.html
    -- у свежего устройства со SMART, но без единой посуточной свёртки, вызов
    srpSpark для дисков падал бы (ReferenceError), если функция объявлена
    только внутри {% if heartbeat_rollups %}."""
    _seed_device_with_scores("dev-nohb", "PC-NOHB")
    _seed_storage("dev-nohb", [_ONE_DISK])
    body = client.get("/device/dev-nohb").text
    assert 'class="vitals-grid"' not in body  # нет rollup -- полосы «Показатели 30 дней» нет
    assert "function srpSpark(" in body  # но хелпер всё равно объявлен для дисков
    assert "Диски (SMART)" in body


# --------------------------------------------------------------------------- #
# 4.3 -- инвентарь: bus_type/interface/firmware дисков, part_number памяти
# --------------------------------------------------------------------------- #
def test_specs_disk_list_shows_interface_bus_and_firmware(client: TestClient) -> None:
    device_id = "dev-specs"
    _seed_device_with_scores(device_id, "PC-SPECS")
    db.store_inventory(
        device_id,
        datetime.now(timezone.utc).isoformat(),
        {
            "disks": [
                {
                    "model": "WD Blue",
                    "media_type": "HDD",
                    "size_gb": 500.0,
                    "interface": "SATA",
                    "bus_type": "SATA",
                    "firmware": "1.0A",
                }
            ]
        },
    )
    body = client.get(f"/device/{device_id}").text
    assert "SATA" in body
    assert "1.0A" in body


def test_specs_memory_modules_show_part_number(client: TestClient) -> None:
    device_id = "dev-mem"
    _seed_device_with_scores(device_id, "PC-MEM")
    db.store_inventory(
        device_id,
        datetime.now(timezone.utc).isoformat(),
        {
            "memory_modules": [
                {"capacity_gb": 16.0, "speed_mhz": 3200, "part_number": "M378A2K43EB1"}
            ]
        },
    )
    body = client.get(f"/device/{device_id}").text
    assert "M378A2K43EB1" in body
