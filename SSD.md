# SSD.md — Мастер-план: диагностика состояния и прогноз деградации парка ПК

> Исполнитель: Claude Code (Sonnet). Все исследовательские и архитектурные решения УЖЕ приняты
> (ревизия кода `server/` + `client/` + `shared/` + `cctodo.md` + `glmdiag_degrdation.md`, 2026-07-02).
> Документ = последовательность конкретных технических задач. Не пересматривать архитектуру,
> не расширять scope. При конфликте с CLAUDE.md / constitution.md — они главнее.

---

## 0. Как исполнять (обязательно для каждой фазы)

1. **Ветка на фазу**: `feat/ssd-f<N>-<slug>` от свежего `origin/main`.
2. **TDD**: тест RED → минимальная реализация GREEN → рефакторинг. Пока итерируешь — только затронутые тесты (`pytest tests/test_x.py -q`).
3. **Гейт перед merge** (ВСЕ зелёные, без исключений): `ruff check .` + `ruff format --check .` · `mypy` (server+shared+client по pyproject) · `bandit` · `pytest` cov ≥80% · `python smoke.py`.
4. **Ревью субагентом до merge**: фазы Ф1, Ф3 (агент/PowerShell/ctypes/privacy/ingest) → `security-reviewer` (Opus) ОБЯЗАТЕЛЬНО; остальные → `code-reviewer` (Sonnet). Исправить все CRITICAL/HIGH.
5. **Merge**: `git merge --no-ff` → `git push origin main` — автоматически, не спрашивая (правило владельца `[[auto-merge-push-no-ask]]`). Стейджить ТОЛЬКО файлы фазы.
6. **CHANGELOG.md** (`## [Unreleased]`) — строка на каждое видимое пользователю изменение, в том же коммите. **CONTINUITY.md** — отметка о фазе.
7. **Инварианты (нарушать нельзя)**: агент = чистый stdlib (ctypes — это stdlib, можно); PowerShell 5.1 floor; языконезависимость (только числа + бинарные структуры, никакого локализованного текста); приватность (сырой серийник диска НИКОГДА не покидает агента — только SHA-256); контракт additive-optional (без bump `CONTRACT_VERSION`); SQL параметризован; Jinja2 autoescape, JS-síнки через `srpEsc`; **UNKNOWN over false confidence** — отсутствие данных никогда не читается как «здоров»; операторский текст — русский, machine-значения — английские; новое поведение включено в shipped `server/config.json` (правило `[[no-disabled-by-default]]`).
8. Каждая фаза самодостаточна и выпускаема отдельно. Порядок фаз менять нельзя (зависимости по данным).

---

## 1. Принятые решения (карта, не обсуждается)

**Научная основа (дистилляция glmdiag_degrdation.md, отфильтрованная по реализуемости на этом стеке):**

- Отказ — не событие, а **накопление**: интегральные и трендовые признаки важнее мгновенных порогов. Тренд-движок (Theil-Sen + ETA) уже есть — расширяем его входы, а не переписываем.
- **Форма распределения ломается раньше среднего**: p95/p50 (tail ratio) дисковой латентности — ранний сигнал. Собираем перцентили на агенте (микро-серия замеров внутри одного heartbeat), НЕ сырые потоки.
- **Повторяемость важнее факта**: одиночная ошибка — шум; кластер (burstiness B=σ/μ>2, повтор в разные недели) — предвестник (Facebook SSD: 99.8% рецидив). Считаем по таблице `events` и по серии SMART-показаний.
- **Цепочки событий**: `153/129 (retry/reset) → 55/7 (corruption/bad block) → 41 (падение)` за 30 дней = деградация storage в процессе, даже при чистом SMART.
- **SMART: высокая specificity, низкая sensitivity** (Google: 56% отказов без SMART-сигнала). Поэтому SMART — ведущий, но не единственный канал; «SMART чистый» ≠ «риск низкий», отсутствие SMART = UNKNOWN.
- **Software aging**: наклон утечки (handles, RAM) относительно uptime-сессии — предиктор зависаний, лечится перезагрузкой; отдельный механизм со своей рекомендацией.
- **Когорта**: устройство сравнивается с одномодельными (уже есть `fleet_anomaly`); добавляем перцентиль по метрике при когорте ≥5.
- **Результат для оператора** — не «сломается/нет», а: доминирующий механизм + горизонт (бэнды 7/30/90 дней, НЕ проценты — вероятности некалиброваны) + цепочка улик + рекомендация + уверенность.

**Отвергнуто / отложено (НЕ реализовывать, не «дорисовывать»):**

| Идея из glmdiag | Вердикт | Причина |
|---|---|---|
| Клиентский risk-engine + локальный SQLite на агенте | ОТВЕРГНУТО | Архитектура SRP: агент тупой, вся аналитика на сервере (LAN, парк ≤1000) |
| Postgres | ОТВЕРГНУТО | SQLite + WAL достаточен на порядки; см. §2 |
| Cox/Weibull survival, labels, Brier-калибровка | ОТЛОЖЕНО (гейт cctodo §7 L1–L3) | Нет меток; детерминированный ETA-baseline ещё не исчерпан |
| Shannon-энтропия латентности | ОТВЕРГНУТО | Tail ratio p95/p50 покрывает ту же физику проще |
| Correlation-drift (Frobenius) | ОТЛОЖЕНО (гейт L2) | При каденсе 4ч на устройство сигнал слаб |
| Активное зондирование (stress-ЭКГ) | ОТВЕРГНУТО | Инвазивно для офисного ПК |
| Arrhenius-интеграл по 1-мин температуре | ОТВЕРГНУТО | Нет надёжного датчика (MSAcpi — мусор на половине OEM) и нет 1-мин каденса; вместо него — тренд температуры диска (реальный сенсор) |
| Email/Teams-алёртинг | ОТВЕРГНУТО (сейчас) | Вместо push — вид «новые эскалации» на дашборде |
| Анти-шумовые окна (AV/WU/Search) | ОТЛОЖЕНО | Вернуться, если tail-ratio даст ложные тревоги на проде |

**Что переиспользуем (уже реализовано — НЕ дублировать):**

| Механизм | Где | Роль в плане |
|---|---|---|
| Trust-слой: source_health → состояния/веса/домены, semantic-валидаторы, last_good, regressed | `server/trust/*`, `pipeline.evaluate_trust` | Новый источник `smart` встраивается сюда |
| Score100-конверт (value/band/confidence/factors/missing_evidence/lineage) | `server/scoring/score100.py` | ВСЕ новые оси и композит — только в нём |
| Тренд-движок Theil-Sen + ETA + anchor-resets | `server/analytics/trends.py` | Новые метрики = новые extractors + вызовы `build_trend` |
| Доменные движки (шаблон) | `server/analytics/storage.py` и соседи | Storage расширяем; `software_aging.py` пишем по этому шаблону |
| Тонкий Bayesian-приоритизатор (D5/D6) | `server/scoring/bayesian.py` | Новые входы — через `domain_values`, KP41/WHEA не реанимировать |
| Append-only история + received_at + clock drift | `db.py` (historical/heartbeats/scores/events) | Источник серий; расширяем retention/rollup |
| Когортные агрегаты | `db.get_fleet_cohort_stats` | Расширяем одним-двумя полями |
| Дашборд-паттерны: Plotly + `|tojson`-остров + `srpEsc` + DOMContentLoaded-гейт + 3 темы + section-label predictive/incident | `web/templates/*` | Вкладка «Здоровье» строится только из них |
| Производные серверные таблицы из payload-хинтов (прецедент) | `printer_ip_map`, `printer_readings` | Образец для `disk_readings` |

---

## 2. Решение по хранению (принято, исполнять как написано)

**Одна база `srp.db` (SQLite, WAL).** Разделение — по СЛОЯМ ТАБЛИЦ, не по файлам (ATTACH-мультифайл отвергнут: усложняет JOIN/бэкап без выигрыша при нашем объёме ~6 конвертов/устройство/сутки):

1. **Оперативный слой** (есть): `devices`, `trust`, `source_last_good`, latest-читатели.
2. **Сырая история** (есть): `heartbeats`, `historical`, `events`, `scores` — append-only, cap на устройство. Добавляем возрастную политику.
3. **Показания приборов** (новое): `disk_readings` — SMART-серия с ключом «конкретный диск», переживает переустановку ОС.
4. **Агрегаты** (новое): `heartbeat_rollup_daily`, `event_rollup_daily` — вечная (2 года) посуточная свёртка для долгих трендов и сравнения периодов; сырьё можно резать смело.
5. **Результаты анализа** (есть): блоб `scores.risk` — расширяется полем `health`; история скорингов = история прогнозов бесплатно.

**Обслуживание** — ежедневный maintenance-цикл (Ф5): rollup → prune по возрасту → `PRAGMA optimize` → редкий guarded `VACUUM`. Рост базы ограничен по построению.

---

## 3. Фазы

### Ф1 — Глубокий SMART на агенте (ядро плана) `[security-review ОБЯЗАТЕЛЕН]`

**Цель:** собрать высокоспецифичные предикторы отказа накопителей, которых сейчас нет: ATA-атрибуты 5/187/188/197/198…, NVMe health-log (media errors, unsafe shutdowns, available spare, percentage used), PredictFailure-флаг, uncorrected-ошибки.

**T1.1 — контракт (additive, БЕЗ bump):** в `shared/schema.py::StorageReliability` добавить Optional-поля:

```python
serial_hash: Optional[str] = None          # SHA-256 серийника, как в DiskInfo — ключ диска
bus_type: Optional[int] = None             # числовой enum MSFT_PhysicalDisk.BusType (17=NVMe, 11=SATA)
read_errors_uncorrected: Optional[int] = None
write_errors_uncorrected: Optional[int] = None
start_stop_cycles: Optional[int] = None
load_unload_cycles: Optional[int] = None
flush_latency_max_ms: Optional[int] = None
smart_predict_fail: Optional[bool] = None  # MSStorageDriver_FailurePredictStatus
smart_attrs: dict[str, int] = Field(default_factory=dict)  # {"5": raw, "197": raw, ...} raw = 48-bit LE int
nvme_critical_warning: Optional[int] = None
nvme_spare_pct: Optional[int] = None
nvme_spare_threshold_pct: Optional[int] = None
nvme_percentage_used: Optional[int] = None
nvme_media_errors: Optional[int] = None
nvme_unsafe_shutdowns: Optional[int] = None
nvme_error_log_entries: Optional[int] = None
nvme_data_units_written: Optional[int] = None
nvme_power_cycles: Optional[int] = None
```

**T1.2 — новый модуль `client/collectors/smart.py`** (подключается из `collect_historical`, отдельный error-domain как `_CERT_SCRIPT`):

- **Tier A (PS 5.1, SATA/ATA):** один скрипт возвращает JSON: (a) карта дисков `Get-CimInstance Win32_DiskDrive` → `{PNPDeviceID, SerialNumber, Index, Model}`; (b) `Get-CimInstance -Namespace root\wmi MSStorageDriver_FailurePredictStatus` → `{InstanceName, PredictFailure}`; (c) `MSStorageDriver_FailurePredictData` → `{InstanceName, VendorSpecific}` как **base64** (`[Convert]::ToBase64String($_.VendorSpecific)`), парсинг блоба — в Python, НЕ в PS. Сопоставление: `InstanceName` без суффикса `_0` case-insensitive = `PNPDeviceID`. Все значения числовые/бинарные — языконезависимо.
- **Парсер блоба (Python, чистая функция `parse_ata_smart(blob: bytes) -> dict[str, int]`):** атрибуты с offset 2, 30 записей × 12 байт: `[0]`=id (0 = пусто), `[5:11]`=raw (6 байт little-endian). Белый список id: `{1, 5, 9, 10, 184, 187, 188, 194, 196, 197, 198, 199, 241, 242}`. Мусорный размер блоба (<362 байт) → `{}`.
- **Tier B (ctypes, NVMe):** функция `read_nvme_health(disk_index: int) -> Optional[dict]`: `CreateFileW(r"\\.\PhysicalDrive{N}", GENERIC_READ, FILE_SHARE_READ|FILE_SHARE_WRITE, ...)` → `DeviceIoControl(IOCTL_STORAGE_QUERY_PROPERTY=0x2D1400)` c буфером: `STORAGE_PROPERTY_QUERY{PropertyId=StorageDeviceProtocolSpecificProperty(50), QueryType=0}` + `STORAGE_PROTOCOL_SPECIFIC_DATA{ProtocolType=ProtocolTypeNvme(3), DataType=NVMeDataTypeLogPage(2), ProtocolDataRequestValue=0x02, ProtocolDataRequestSubValue=0, ProtocolDataOffset=sizeof(STORAGE_PROTOCOL_SPECIFIC_DATA), ProtocolDataLength=512}`. **Сверить размеры структур с winioctl.h** (в новых SDK у STORAGE_PROTOCOL_SPECIFIC_DATA есть SubValue2-4 → 40 байт). Разбор 512-байтового NVMe SMART/Health log (все поля LE): byte 0 critical_warning; 1:3 composite temperature (Kelvin → °C = K−273); 3 available spare %; 4 spare threshold %; 5 percentage used; 32:48 data units read; 48:64 data units written; 112:128 power cycles; 128:144 power-on hours; 144:160 unsafe shutdowns; 160:176 media errors; 176:192 error log entries. 128-битные счётчики клампить в int64.
- **Слияние:** базовая строка каждого диска — из существующего `Get-StorageReliabilityCounter`-блока `historical.py` (расширить его полями `ReadErrorsUncorrected`, `WriteErrorsUncorrected`, `StartStopCycleCount`, `LoadUnloadCycleCount`, `FlushLatencyMax` — они в том же объекте); поверх — Tier A/B по совпадению серийника/индекса. `serial_hash` = **тот же самый** хэш-хелпер, что в `client/collectors/inventory.py` для `DiskInfo.serial_hash` (найти и переиспользовать функцию/нормализацию, при необходимости вынести в общий модуль — иначе JOIN на сервере не сойдётся). Сырой серийник за пределы функции не выходит.
- **Отказоустойчивость:** каждый tier в своём try/except; USB/RAID/виртуалки без поддержки → поля None, никогда не исключение. Таймаут PS ≤ 60с; ctypes-вызов на диск ≤ 1с.
- **source_health:** новый источник `smart` в `client/collectors/sources.py` (константа + owned в historical): `ok` (есть хоть один tier хоть на одном диске) / `partial` / `absent`.

**T1.3 — trust:** в `server/trust/domains.py` добавить `smart` в домен `storage`; в `server/trust/validators.py` — semantic-валидатор `smart`: монотонные счётчики (`nvme_media_errors`, `nvme_unsafe_shutdowns`, `smart_attrs[5|197|198]`, poh) не убывают против `last_good` (падение > допуска = replaced-диск → это НЕ suspect, это reset — сравнивать только при том же `serial_hash`); проценты 0..100; температура −10..100. В `pipeline._extract_reading` — срез для `smart` (первый диск, decision-material поля).

**T1.4 — тесты** (`tests/test_smart_collector.py`): golden-фикстуры JSON PS-вывода (в т.ч. записанный русско-локальный вывод — проверка языконезависимости фактом); `parse_ata_smart` на синтетическом блобе (известные значения, обрезанный, нулевой); разбор NVMe-лога на синтетических 512 байтах; слияние по serial_hash; отказ каждого tier по отдельности → статусы; **приватность: в payload нет сырого серийника** (grep-тест по сериализованному payload). ctypes-путь тестировать через инъекцию транспорта (функция принимает `ioctl_fn`), НЕ через реальные устройства.

**Гейт Ф1:** полный §0-гейт + security-review (фокус: ctypes-буферы/переполнение, приватность серийников, PS 5.1-совместимость, «блокированный источник ≠ здоров»).

---

### Ф2 — Сервер: серия «на диск» + storage-движок v2

**Цель:** SMART-история с ключом «физический диск», жёсткие правила и рецидив-факторы в storage-риске.

**T2.1 — таблица `disk_readings`** (образец — `printer_readings`/`printer_ip_map`):

```sql
CREATE TABLE IF NOT EXISTS disk_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  disk_key  TEXT NOT NULL,   -- serial_hash | fallback sha256(model|size_gb|порядковый)
  ts TEXT, received_at TEXT NOT NULL,
  media_type TEXT, payload TEXT NOT NULL   -- JSON одной StorageReliability-строки
);
CREATE INDEX IF NOT EXISTS idx_diskread ON disk_readings(device_id, disk_key, id);
```

`db.store_disk_readings(device_id, storage_list, ts, received_at)` — вызывается из `pipeline.ingest_envelope` в ветке `historical` (рядом с `store_printer_ip_hints`); prune cap `_retain_disk=2000` на (device, disk); `disk_readings` → в `_DEVICE_TABLES` (удаление устройства чистит). Читатель `db.get_disk_series(device_id, disk_key, limit)` + `db.list_device_disks(device_id)`.

**T2.2 — storage-движок v2** (`server/analytics/storage.py`, сигнатура `compute_storage_risk` расширяется optional-параметром `disk_series: Optional[dict[str, list[dict]]] = None`, обратная совместимость обязательна):

- **Жёсткие правила** (band=bad немедленно, факторы по-русски): `smart_predict_fail` (+70 «прошивка диска сама предсказывает отказ»); `nvme_critical_warning` бит 0 spare (+70) / бит 3 read-only (+80); attr 197>0 (+45; >10 → +60 «сектора в ожидании переназначения»); attr 198>0 (+60); `nvme_media_errors`>0 (+45); `nvme_spare_pct < nvme_spare_threshold_pct` (+70).
- **Сильные:** attr 5>0 (+30; >100 → +50); attr 187>0 (+35); attr 188>0 (+20); `read/write_errors_uncorrected`>0 (+40).
- **Старение:** `nvme_percentage_used`/`wear_pct` >85 (+25) / >95 (+40); высокие unsafe_shutdowns добавляют +10 ТОЛЬКО при ненулевых media/pending (условный усилитель, D6-стиль).
- **Рецидив (нужна серия):** по `disk_series` — если `nvme_media_errors` или attr 197/5 ВЫРОСЛИ между показаниями, разнесёнными ≥7 дней → множитель ×1.3 + фактор «ошибки повторяются — кластерный предвестник». Сброс (замена диска) определяется сменой `disk_key`, не эвристикой.
- Латентность остаётся только подтверждением (не трогать инвариант). Нет НИКАКИХ SMART-полей → UNKNOWN (как сейчас).

**T2.3 — тренды на диск:** в `trends.py` добавить extractors по `disk_series` худшего диска: `smart_pending` (attr 197, direction-only), `smart_media_errors` (direction-only), уточнить `storage_wear` — источник `max(wear_pct, nvme_percentage_used)`. Подключить в `pipeline.recompute_scores`: выбрать worst-диск по последнему показанию, передать серию в `compute_trends` (новый optional-параметр `disk_series`).

**T2.4 — тесты:** `tests/test_disk_readings.py` (запись/чтение/cap/cleanup/fallback-ключ); `tests/test_storage_engine_v2.py` (каждое правило, рецидив, UNKNOWN-гейты, обратная совместимость без новых полей — старые payload'ы дают прежний результат: регресс-пин на двух старых фикстурах).

---

### Ф3 — Цепочки событий и кластеризация ошибок `[security-review: агентная часть]`

**Цель:** реализовать «153→129→55→41», burstiness и рецидив недель — самые ранние storage/стабильность-сигналы вне SMART.

**T3.1 — агент, whitelist событий** (`client/collectors/events.py::_SCRIPT`, добавить запросы):
`@{LogName='System'; ProviderName='storahci'; Id=129}`, `@{LogName='System'; ProviderName='stornvme'; Id=129}`, `@{LogName='System'; ProviderName='disk'; Id=157}`, `@{LogName='Application'; ProviderName='Application Hang'; Id=1002}`. Кап батча не менять.

**T3.2 — новый чистый модуль `server/analytics/errchain.py`:**

```python
STORAGE_EARLY = {153, 129}; STORAGE_DAMAGE = {55, 7, 51}; CRASH = {41, 1001, 6008}
@dataclass(frozen=True) class ErrChain:
    stage: int            # 0 нет, 1 retries, 2 retries+damage, 3 полная цепочка (damage→crash ≤ 7д)
    burstiness: Optional[float]   # σ/μ интервалов storage-ошибок, None при n<4
    recurrent_weeks: int  # число ISO-недель с ≥1 storage-ошибкой за 30д
    counts: dict[str, int]; factors: list[dict]  # русские labels
def analyze_events(events: list[dict], *, now: datetime) -> ErrChain
```

Вход — строки `db.get_recent_events` (там есть `event_id`, `source`, `ts`, `received_at`); время — `received_at`. Всё детерминированно, без ML.

**T3.3 — wiring:** в `recompute_scores` вызвать `analyze_events` (events уже читаются для disk_fill — переиспользовать ту же выборку); передать в storage-движок (`chain: Optional[ErrChain]`): stage 2 → ×1.25 + фактор; stage 3 → +25 (может поднять риск сам — это событие повреждения, не латентность); burstiness>2 → +10; НО при полностью пустом SMART цепочка даёт максимум band=watch (цепочка без SMART = подозрение, не приговор). В `bayesian._stability` — усилитель роста 1002 (app hang) с весом ≤0.6. KP41/WHEA-инварианты (D6) не трогать.
Результат `errchain` положить в `risk_block["errchain"]` (для дашборда и health-композита Ф6).

**T3.4 — тесты:** `tests/test_errchain.py` — стадии, burstiness (регулярные B≈1 vs кластер B>2), недели-рецидив, пустота; `tests/test_storage_chain_wiring.py` — цепочка без SMART капится watch; фикстура агентского PS-вывода с новыми ID.

---

### Ф4 — Форма распределения + software-aging движок

**Цель:** ранние сигналы «до сдвига среднего»: p95/p50 латентности диска; наклоны утечек по uptime-сессиям.

**T4.1 — агент, микро-серия в heartbeat** (`client/collectors/heartbeat.py`): в СУЩЕСТВУЮЩИЙ скрипт добавить цикл из 8 замеров × ~2с по `Win32_PerfRawData_PerfDisk_PhysicalDisk` (`Name='_Total'`): латентность как PERF_AVERAGE_TIMER: `((N2−N1)/Frequency_PerfTime)/(B2−B1)` секунд, где N=`AvgDisksecPerRead|Write`, B=`AvgDisksecPerRead_Base|Write_Base`; замеры с `ΔB=0` (нет операций) пропускать. Отдать в JSON: `disk_read_ms_p50/p95`, `disk_write_ms_p50/p95`, `disk_lat_max_ms`, `disk_lat_samples`. Общий бюджет скрипта ≤ 30с (таймаут run_ps поднять до 75). Это заодно чинит известное ограничение «целочисленная латентность = 0». Контракт: шесть новых Optional-полей в `HeartbeatPayload`. Старые поля не убирать.

**T4.2 — тренд tail-ratio:** extractor `_disk_tail_ratio(row) = p95/p50` (None при p50==0/отсутствии); `build_trend(..., worsening_sign=1)` direction-only; добавить в `compute_trends` и в `trajectory`-блоб (в `_DEPLETION_DOMAINS` НЕ включать — нет физической границы).

**T4.3 — движок `server/analytics/software_aging.py`** (по шаблону storage.py, Score100):

- Вход: `hb_series` (уже читается в pipeline, limit 200).
- Разбить на uptime-сессии: разрыв, где `uptime_hours` упал против предыдущего → новая сессия.
- В последней сессии с ≥4 точками: Theil-Sen (переиспользовать `trends.theil_sen_slope` на точках `(uptime_hours*3600, value)` → пересчитать в «в час») для `handle_count_total` и `mem_avail_mb`.
- Факторы: handles slope > 100/час (+25; >300 +45 «утечка дескрипторов»); mem_avail slope < −50 МБ/час (+20); `uptime_hours` > 336 (2 недели, +10 «давно без перезагрузки»); подтверждение: рост `pagefile_pct` p95 против первой половины сессии (+10).
- Сравнение сессий: если предыдущая сессия имела slope, а после перезагрузки метрика вернулась → фактор-примечание «перезагрузка возвращает ресурс — программная утечка» (риск не добавляет, попадает в рекомендацию).
- Гейты: <4 точек в сессии → UNKNOWN; untrusted → withhold (копировать шаблон).
- Wiring: в `recompute_scores` → `score100["software_aging_risk"]`; в `bayesian._stability` через `domain_values["software_aging_risk"]` (scale 0.8); в `diagnostics.compute_diagnostics` добавить поле.

**T4.4 — тесты:** `tests/test_heartbeat_shape.py` (фикстура PS-вывода с перцентилями, отсутствие полей у старого агента); `tests/test_software_aging.py` (утечка GREEN-кейс, сессии/сброс uptime, «перезагрузка лечит», UNKNOWN-гейты); обновить golden-пины scoring-блоба, если есть.

---

### Ф5 — Rollup, retention, обслуживание хранилища

**Цель:** годы истории для сравнения периодов при ограниченном размере базы; события живут достаточно долго для 30-дневных цепочек.

**T5.1 — таблицы:**

```sql
CREATE TABLE IF NOT EXISTS heartbeat_rollup_daily (
  device_id TEXT NOT NULL, day TEXT NOT NULL,          -- 'YYYY-MM-DD' по UTC received_at
  n INTEGER NOT NULL,
  cpu_p50 REAL, cpu_p95 REAL, mem_avail_min REAL, pagefile_p95 REAL,
  disk_read_ms_p95 REAL, disk_write_ms_p95 REAL, disk_queue_p95 REAL,
  handles_max INTEGER, free_space_min REAL, uptime_max REAL,
  PRIMARY KEY (device_id, day));
CREATE TABLE IF NOT EXISTS event_rollup_daily (
  device_id TEXT NOT NULL, day TEXT NOT NULL, event_key TEXT NOT NULL,  -- "source:event_id"
  n INTEGER NOT NULL, PRIMARY KEY (device_id, day, event_key));
```

Писатели `db.rollup_heartbeats_daily(day)` / `db.rollup_events_daily(day)` — идемпотентный upsert (перцентили в Python: выборка дня, объёмы малы). Читатели `db.get_heartbeat_rollups(device_id, days)`, `db.get_event_rollups(device_id, days)`.

**T5.2 — конфиг (additive в `server/config.py` + shipped `server/config.json`, всё включено):**

```json
"retention": { "heartbeat_raw_days": 30, "events_days": 90, "rollup_days": 730,
               "disk_readings_per_disk": 2000, "maintenance_interval_sec": 86400 }
```

**T5.3 — maintenance-цикл** в `server/main.py` (образец — netdisco-циклы: поток, джиттер, self-guard try/except, гейт не нужен — всегда on): досчитать rollup за вчера+сегодня → prune: `heartbeats` старше `heartbeat_raw_days` (сверх cap-политики, cap остаётся страховкой), `events` старше `events_days` (возрастной prune ВАЖЕН: сейчас cap=1000 строк — на шумной машине это <2 суток либо, наоборот, годы мусора), rollups старше `rollup_days` → `PRAGMA optimize`; `VACUUM` только если `freelist_count/page_count > 0.2` и не чаще раза в 7 дней (отметка в новой служебной таблице `maintenance_log(ts, action, detail)`). Проверить `db._connect`: если WAL/busy_timeout не включены — включить (`PRAGMA journal_mode=WAL`, `busy_timeout=5000`) идемпотентно в `init_db`.

**T5.4 — surface:** на `/pipeline` — блок «Хранилище»: размер базы, строки по слоям, последний maintenance, последний rollup-день. В `db.get_pipeline_metrics` добавить эти числа.

**T5.5 — тесты:** `tests/test_rollup.py` (перцентили, идемпотентность, границы дня UTC); `tests/test_maintenance.py` (prune по возрасту не трогает свежее, rollup переживает prune сырья, maintenance_log, VACUUM-guard мокается).

---

### Ф6 — Композитный «Индекс здоровья» + горизонты + когорта

**Цель:** один взгляд оператора: индекс 0-100, механизм, горизонт-бэнды, рекомендация, динамика. Детерминированно, поверх существующих осей.

**T6.1 — модуль `server/analytics/health.py`:**

```python
@dataclass(frozen=True) class HealthVerdict:
    index: Optional[float]        # 0..100, выше = здоровее; None = UNKNOWN
    band: str; confidence: str    # реиспользовать шкалы score100
    dominant: Optional[str]       # machine-key механизма (storage|aging|battery|disk_fill|os|power|fleet|network)
    dominant_label: str           # русский
    horizon: dict[str, str]      # {"d7": band, "d30": band, "d90": band} — БЭНДЫ, не проценты
    action: str                   # русская рекомендация из словаря механизмов
    factors: list[dict]; missing_evidence: list[str]
def compute_health(score100_axes: dict, bayes: dict, trends: dict, errchain: Optional[dict],
                   cohort: Optional[dict]) -> HealthVerdict
```

- `index = 100 − (0.7·max_i(w_i·r_i) + 0.3·Σ(w_i·r_i)/Σw_i)` по известным (не-None) риск-осям; веса: storage .30, software_aging .15, os_degradation .15, trajectory .10, battery .10, disk_fill .10, network .05, throttle-тренд .05 (fleet_anomaly — НЕ в индексе, это контекст сценария «когорта, не железо»). Все None-оси исключаются из суммы И из нормировки; если материальных осей <2 или storage=None → confidence ≤ medium + missing_evidence; все None → index=None (UNKNOWN).
- Горизонты (маппинг, не вероятность): любое жёсткое SMART-правило или chain stage 3 → `d7=critical`; ETA≤30д или chain stage 2 или media-рецидив → `d30=high`; ETA≤90д или aging-slope high или tail-ratio worsening → `d90=elevated`; иначе по бэнду индекса. Монотонность: d7 ≥ d30 ≥ d90 по серьёзности не требуется — но ближний горизонт не может быть ХУЖЕ дальнего без жёсткого правила (тест).
- `dominant` = ось с max w_i·r_i, сверенная с `bayes["top"]`; словарь action: storage→«снять образ данных, планировать замену накопителя», aging→«перезагрузить; если повторится — искать утечку в ПО», disk_fill→«освободить место — под угрозой обновления Windows», battery→«заменить батарею», power→«проверить питание/БП/розетку», os→«переустановка/восстановление ОС при повторении», fleet→«массовый случай — проверять обновление/партию, не железо», network→«проверить линк/кабель/точку доступа».
- Дельта: `delta_7d` считается в pipeline из `db.get_score_series` (первая строка старше 7 дней; None при нехватке).
- Когорта: расширить `db.get_fleet_cohort_stats` полем `cohort_boot_ms` (список значений когорты через json_extract, cap 200) → health-фактор «загрузка дольше, чем у 90% таких же машин» при когорте ≥5 (перцентиль в Python).

**T6.2 — wiring:** в `recompute_scores` после всех осей: `risk_block["health"] = asdict(verdict) + {"delta_7d": ...}`. API: `GET /api/v1/devices/{id}/health` (читает из сохранённого блоба, НЕ пересчитывает — паттерн diagnostics.py); в `db.get_devices` пробросить `health.index/band/dominant/delta_7d` в строку флота.

**T6.3 — тесты:** `tests/test_health.py` — веса/None-исключение/UNKNOWN, горизонт-маппинг (жёсткое правило бьёт всё), монотонность горизонтов, словарь действий покрыт для каждого механизма, дельта, когортный фактор (≥5, перцентиль), RU/EN-разделение (dominant — английский ключ, label — русский; пин-тест).

---

### Ф7 — Дашборд «Здоровье» (визуальный, минимум текста)

**Цель:** оператор за 10 секунд видит: у кого плохо, у кого станет плохо, что делать. Максимум графики, ноль таблиц с сырыми числами на первом экране. Перед вёрсткой инвокнуть скилл `frontend-design`; XSS-инварианты дашборда обязательны (`srpEsc` для ЛЮБОЙ агентской строки в JS-síнках, `|tojson`-острова, init строго после `DOMContentLoaded` — закреплённая гочя Plotly).

**T7.1 — маршрут `/health` + `web/templates/health.html`** (nav в `base.html`: пункт «здоровье» после «флот»):

- **Ряд KPI-плиток**: распределение флота по бэндам (donut Plotly) · число «критичных ≤7д» · число ухудшившихся за неделю · число UNKNOWN (наблюдаемость — честно показать слепые зоны).
- **Тепловая карта** (Plotly heatmap): строки=устройства (сортировка по index asc), колонки=оси (storage/aging/os/battery/disk_fill/network/trajectory), z=band (0..3), hover=топ-факторы; клик по строке → `/device/{id}`.
- **«Ухудшаются»**: топ-10 по `delta_7d` — горизонтальный bar с дельтой + спарклайн индекса (мини-Plotly, данные из score-серии, limit 30).
- **«Новые эскалации»**: устройства, чей band стал хуже за 7д (данные — новый `db.get_fleet_health_deltas(days=7)`: по каждому устройству последний score-row старше cutoff одним запросом с window-функцией, образец — `get_net_device_status_series`).
- Данные страницы — один route-контекст + JSON-острова; никаких fetch-каскадов.

**T7.2 — hero-блок на `device.html`** (над существующей секцией «Прогноз — траектории и ресурс», её НЕ удалять — она становится «деталями»):
gauge индекса (Plotly indicator) · три chips-горизонта (7/30/90 дней, цвет=band) · «доминирующий механизм» + рекомендация одной строкой · стрелка Δ7д · спарклайн индекса из score-серии · кнопки «30д/90д» — переключение окна спарклайна (сравнение периодов). Цвета — только из CSS-токенов темы (3 темы), никаких хардкодов.

**T7.3 — тесты:** `tests/test_health_web.py` — 200 на `/health`, JSON-остров с полями, XSS-payload `<img onerror>` в hostname инертен (пин по экранированному телу), структурный пин DOMContentLoaded-гейта (урок printers.html), пункт nav есть; `tests/test_device_hero.py` — hero рендерится, UNKNOWN-устройство показывает «данных недостаточно», а не 100/100.

---

## 4. Порядок, роутинг, критерий готовности

```
Ф1 (агент SMART, security-review) → Ф2 (disk_readings + движок v2)
  → Ф3 (цепочки/burstiness, security-review агентной части)
  → Ф4 (форма распределения + aging) → Ф5 (rollup/retention)
  → Ф6 (health-композит) → Ф7 (дашборд)
```

- Модель/effort на исполнение задач: R2 (Opus·low) для одиночных задач по этому плану; R3 для Ф1 и Ф6 (много файлов); любые правки `shared/schema.py`, `server/trust/`, агентского PS — режим R4-триггеров: ≥R3 + security-review (CLAUDE.md §2).
- **Definition of Done всего плана:** все 7 фаз в `origin/main`; полный гейт зелёный; на демо-стенде: устройство с изношенным SSD показывает механизм «storage» с горизонтом и рекомендацией; устройство с утечкой — «aging» с «перезагрузить»; флот-страница `/health` отвечает <500мс на 50 устройствах; база не растёт бесконтрольно (maintenance_log пишется); ни один тест не ослаблен.
- После Ф7 — отдельной задачей (вне плана): наблюдение 2-4 недели на реальном парке, калибровка порогов Ф2-Ф4 по ложным тревогам; только после этого рассматривать гейтованные фазы cctodo §7 (labels → survival).
