# Периодическая переоценка «протухания» источников trust — дизайн + план

- **Дата:** 2026-07-22
- **Статус:** ДИЗАЙН — одно решение принято по каждой развилке, ждёт вычитки перед реализацией.
- **Класс задачи:** R4 (`server/trust/` + схема `device_source_trust` + trust-write-path). Security-review обязателен на Чанк-1 и Чанк-2.
- **Прозовая часть — RU; идентификаторы/поля/enum-значения/коды — EN** (конвенция §5: operator prose RU, machine values EN).
- **Первопричина:** stoperrors.md P2-2. `derive_state` содержит ветку STALE (gate.py:47), но единственный вызов (`evaluate_trust`, pipeline.py:256-261) всегда передаёт `age_sec=None, stale_after_sec=None` → ветка мертва. Источник, который перестал отчитываться (сломанный коллектор, вынутый диск), навсегда замораживает своё последнее trust-состояние («trusted»/OK); `resolve_domain_trust` бесконечно считает его актуальной уликой. Trust переоценивается ТОЛЬКО реактивно, на приход нового конверта; замолчавший источник этот путь больше не запускает.

---

## 1. Цель и критерий успеха

Добавить **фоновую периодическую переоценку** каждой строки `device_source_trust`, независимую от ingest: если сервер давно не получал реального отчёта по (device, source), состояние источника деградирует до `STALE` (вес → 0), и домен, который на нём стоит, перестаёт читаться как «trusted».

**Критерий успеха:**
- Замолчавший **доменный** источник в пределах `порог + интервал` переходит в `STALE`; домен на следующем ingest устройства становится `UNKNOWN` (K-инвариант: UNKNOWN важнее ложной уверенности).
- Повторные прогоны джоба **не сбрасывают** часы протухания (ловушка P1-4 структурно невозможна).
- Восстановившийся источник (снова шлёт данные) автоматически оживает штатным ingest-путём.
- Часы протухания считаются по **серверному** времени приёма, а не по клиентскому `ts` (инвариант W0.2: клиентским часам для staleness не верим).
- Событийные/не-доменные источники (print_jobs, events) НЕ протухают ложно из-за «нечего было отправлять».

---

## 2. Ключевые решения

- **D1 (единица порога = источник, применяется к доменным источникам).** `derive_state` работает ПО ИСТОЧНИКУ, не по домену, поэтому «per domain» из находки механически неточно. Но все источники шлёт один агент по одному расписанию (client/config.py: inventory/historical/heartbeat на общем 14400 с / 4 ч цикле) — реальной разницы кадансов между источниками нет. Значит: **один глобальный порог, применяемый к каждой строке**, без per-source/per-domain override (YAGNI; override добавим, только если появится источник с заведомо иным кадансом). Порог применяется ТОЛЬКО к источникам, входящим в `DOMAIN_SOURCES` (union required+optional = `storage_reliability, disk_latency, smart, free_space, reliability, boot_time, throttle, network`). Именно их замерзание порождает вред из находки (`resolve_domain_trust` читает только доменные источники). `identity, events, certificates, print_jobs` — событийные / не гейтят домен → **явно исключены** (иначе устройство, которое просто ничего не печатало 12 ч, ложно получило бы `STALE`). Множество вычисляется из `DOMAIN_SOURCES` (единый источник правды), тем же способом, что `_KNOWN_TRUST_SOURCES` в pipeline.py.

- **D2 (часы протухания = новая серверная колонка `evidence_seen_at`).** Нужна метка «когда сервер в последний раз получил РЕАЛЬНЫЙ отчёт по этому источнику». Существующий `device_source_trust.ts` для этого не годится: (а) это КЛИЕНТСКИЙ `ts` (env.ts) — подделываемый будущим временем заморозит staleness навсегда, прямое нарушение W0.2; (б) семантически перегружать его рискованно. Существующего серверного per-source времени нет (`source_last_good.ts` тоже клиентский и пишется лишь на OK+reading). Поэтому: **аддитивная колонка `evidence_seen_at TEXT`**, серверный `received_at`, пишется ТОЛЬКО на реальном ingest. `ts` (клиентский) оставляем как есть — он сейчас downstream никем не читается (`_build_source_trust_map` его игнорирует), не трогаем.

- **D3 (анти-ловушка сброса = отдельная write-функция без параметра `evidence_seen_at`).** Джоб пишет результат через НОВУЮ `apply_source_staleness(...)`, которая обновляет ТОЛЬКО `state, weight, reason`. У неё физически нет аргумента `evidence_seen_at` (и она не трогает `collector_status/semantic_status/ts`) → повторный прогон не может сдвинуть часы. Это тот же приём, что P1-4 (reconcile.py:189-197: «omit the field, let COALESCE preserve»), только доведённый до структурной невозможности. Плюс оптимистичный guard: `WHERE evidence_seen_at IS ?` (значение, прочитанное джобом) — если ingest успел обновить улику между read и write, staleness-запись безопасно отбрасывается (свежий ingest побеждает). Guard — defense-in-depth: даже без него следующий цикл самоисправится (STALE — безопасное направление).

- **D4 (интервал = 3600 с / 1 ч + джиттер).** Staleness — сигнал масштаба часов, не секунд. 3600 с — ровно тот регистр, где уже живут соседи (`classify/topology/passive` = 3600). Ловит переход через порог за ≤1 ч после факта, ценой одного скана крошечной таблицы. Пол `max(60, ...)` как у всех петель; джиттер де-фазирует от других петель (анти-thundering-herd).

- **D5 (порог = 43200 с / 12 ч ≈ 3 пропущенных цикла).** Агент шлёт каждые 4 ч; 3×4 ч = «источник пропустил ~3 отчёта подряд = реально мёртв, а не просто опоздал» → не флапает между штатными отчётами. Пол на пороге (напр. 60 с), чтобы кривой конфиг `0` не пометил всё staleness мгновенно.

- **D6 (джоб только помечает строки-источники; домены/скоры оживают на следующем ingest).** Джоб НЕ переагрегирует домены и НЕ вызывает `recompute_scores`. В сценарии находки (устройство шлёт ДРУГИЕ источники, молчит один) каждый следующий ingest устройства вызывает `evaluate_trust` → `_build_source_trust_map` читает ВСЕ строки `device_source_trust` (включая нашу `STALE`) → `resolve_domain_trust` агрегирует → `store_trust` → `recompute_scores`. Т.е. `STALE` доезжает до домена/скора на ближайшем ingest (~4 ч), БЕЗ втягивания джоба в тяжёлый scoring-путь. Полностью молчащее устройство (все источники встали) — вне зоны этой фичи (его закрывают существующий device-offline флаг + 30-дневный ghost-purge).

- **D7 (переход только OK/DEGRADED → STALE; никогда не оживляет).** Джоб на каждой in-scope строке зовёт `derive_state(stored_collector, stored_semantic, age_sec, порог)` и пишет, лишь если результат ОТЛИЧАЕТСЯ от хранимого. Лестница (`SUSPECT > UNAVAILABLE > STALE > DEGRADED > OK`) сама даёт: строка уже `UNAVAILABLE/SUSPECT` (коллектор/семантика падали на последнем отчёте) → результат тот же → записи нет; строка `OK/DEGRADED` + возраст > порога → `STALE`, вес `0`. Свежая (`age < порог`) → `OK` == хранимого → записи нет (нет churn). Вес через `compute_weight(STALE) = 0.0`.

---

## 3. Не-цели (YAGNI)

- Переагрегация доменов / `recompute_scores` внутри джоба (D6: оживает на ближайшем ingest).
- Обработка полностью молчащего устройства (существующий device-offline `stale_after_sec=600` + ghost-purge).
- Per-source / per-domain override порога (D1: единый каданс агента).
- Оживление источника джобом (штатный ingest-путь, D3/D7).
- Отдельная подсистема-конфиг (dataclass) как у printers/netdisco — фича ближе к retention-петле (топ-левел поля ServerConfig).

---

## 4. Архитектура и компоненты

| Компонент | Где | Роль |
|---|---|---|
| колонка `evidence_seen_at` | `server/db.py` схема + `_ADD_COLUMNS`/`_BACKFILL` | серверные часы «последнего реального отчёта» по (device, source) |
| `upsert_source_trust(..., evidence_seen_at)` | `server/db.py` | ingest-путь пишет улику (received_at) |
| `evaluate_trust(..., received_at)` | `server/pipeline.py` | прокидывает received_at в upsert (единственный call-site ingest_envelope) |
| `get_source_trust_rows()` | `server/db.py` | 1 SELECT всех строк (device_id, source, state, collector_status, semantic_status, evidence_seen_at) |
| `apply_source_staleness(updates)` | `server/db.py` | пишет ТОЛЬКО state/weight/reason, guard `WHERE evidence_seen_at IS ?` (D3) |
| `reevaluate_staleness(rows, now, stale_after_sec)` | `server/trust/staleness.py` (новый) | ЧИСТАЯ функция: фильтр по доменным источникам, возраст из evidence_seen_at, `derive_state`, отдаёт только изменившиеся → `list[StaleUpdate]` |
| `run_staleness_cycle(cfg, *, get_rows, write, now=None)` | `server/trust/staleness.py` | тонкий оркестратор (инъектируемые deps, стиль `run_topology_cycle`); read → pure fn → write |
| `_run_source_staleness` / `_source_staleness_loop` | `server/main.py` | self-guarded обёртка + asyncio-петля (идиома `_printer_poll_loop`) |
| `source_stale_after_sec`, `source_stale_reeval_interval_sec` | `server/config.py` | топ-левел поля ServerConfig |

**Разделение:** вся trust-логика (что становится STALE) — чистая, в `staleness.py`, юнит-тестируется без DB/часов; `db.py` остаётся без логики; `main.py` — только петля. `derive_state`/`compute_weight` НЕ меняются (мёртвая ветка просто наконец получает не-None аргументы).

---

## 5. Data flow

```
[ingest конверта с source_health] (без изменений семантики, +1 метка)
  ingest_envelope: received_at = _now_iso()  (уже есть)
    └─ evaluate_trust(did, payload, raw_health, ts, received_at)   ← +received_at
         для каждого источника в source_health:
           upsert_source_trust(..., ts=env.ts, evidence_seen_at=received_at)  ← +улика (серверные часы)
  (замолчавший источник в source_health отсутствует → его строку не трогаем → evidence_seen_at замерзает — ЭТО И ЕСТЬ часы)

[фоновая петля, каждые 3600 с + джиттер]
  _source_staleness_loop → to_thread(_run_source_staleness) → run_staleness_cycle(cfg)
    rows = get_source_trust_rows()                              # 1 SELECT
    updates = reevaluate_staleness(rows, now, cfg.source_stale_after_sec)
        для строки, source ∈ DOMAIN_SOURCES-flat, evidence_seen_at != NULL:
          age = now - evidence_seen_at
          new = derive_state(collector, semantic, age, порог)   # мёртвая ветка ожила
          if new != state: yield StaleUpdate(device, source, new.value, compute_weight(new), reason_ru)
    apply_source_staleness(updates)                             # state/weight/reason, guard evidence_seen_at

[ближайший ingest устройства (любого ДРУГОГО источника)]
  evaluate_trust → _build_source_trust_map (читает STALE-строку) → resolve_domain_trust → домен UNKNOWN → store_trust → recompute_scores (скор withholds)
```

`reason` (operator-facing → RU): напр. `"источник молчит {age_h} ч (порог {thr_h} ч)"`. Значения-числа/`state="stale"` — EN.

---

## 6. Схема и миграция

```sql
-- device_source_trust: +1 колонка (аддитивно)
evidence_seen_at TEXT   -- серверный received_at последнего реального ingest этого источника
```
- Новые БД: колонка в `CREATE TABLE device_source_trust` (db.py:245).
- Существующие БД: запись в `_ADD_COLUMNS["device_source_trust"] = (("evidence_seen_at", "TEXT"),)`; `_migrate_add_columns` доальтерит идемпотентно.
- **Backfill:** `_BACKFILL["device_source_trust"] = "UPDATE device_source_trust SET evidence_seen_at = ts WHERE evidence_seen_at IS NULL"` — тот же приём и формулировка, что существующие W0.2-бэкфиллы (db.py:595-604: серверные колонки best-effort заполняются из клиентского `ts` для legacy-строк). Живые часы дальше серверные (W0.2-корректная, load-bearing часть); одноразовый backfill legacy-строк из клиентского `ts` — явный best-effort, строка перештампуется серверным received_at на первом же реальном отчёте. Следствие: genuinely-мёртвый до миграции источник корректно флагится `STALE` на первом цикле (не ложное срабатывание — он и правда молчит), свежая строка — нет.
- `evidence_seen_at IS NULL` (защитно, после backfill не должно быть) → `age = None` → ветка STALE не срабатывает (нельзя состарить то, чему нет времени).
- `device_source_trust` уже в `_DEVICE_TABLES` → чистка устройства сносит и новую колонку строки; NOT в `_METRIC_TABLES`-специфике — ничего не ломает.

---

## 7. Конфиг (server/config.py, топ-левел ServerConfig)

```python
# Per-source trust staleness re-eval (stoperrors P2-2). ВНИМАНИЕ: это НЕ то же,
# что device-level stale_after_sec=600 (дашбордный offline-флаг, set_stale_threshold);
# те поля не связаны — не путать и не переиспользовать.
source_stale_after_sec: int = 43200          # 12 ч ≈ 3 пропущенных 4-ч цикла агента
source_stale_reeval_interval_sec: int = 3600 # 1 ч; 0 = выключить петлю
```
- **`server/config.json` менять НЕ нужно.** Фича включена дефолтом (`interval=3600 > 0`), не гейтит сеть/приватность/скан — чистый внутренний DB-sweep. Соответствует правилу no-disabled-by-default: не off-by-default в коде → отдельного `enabled`-флага и записи в config.json не требует (off-switch = `interval=0`, как retention-петля гейтится на `purge_interval_hours>0`).
- Петля: `interval = max(60, cfg.source_stale_reeval_interval_sec)`; порог клампится полом (напр. `max(60, cfg.source_stale_after_sec)`) в оркестраторе.

---

## 8. Error handling

- `_run_source_staleness` — self-guard `try/except Exception → log.exception → return` (дословно идиома `_run_printer_poll`, main.py:132-142): транзиентная DB-ошибка не роняет старт и не убивает петлю.
- `reevaluate_staleness` — чистая, без I/O: битый/NULL `evidence_seen_at` → строка пропускается (не флагится), не исключение. Неизвестный `state`/`collector_status` в строке (форс-мажор) → `derive_state` работает на enum-ах; парс enum обёрнут, кривое значение → пропуск строки.
- Гонка reeval↔ingest: guard D3 + самоисправление за ≤1 цикл (STALE — безопасное направление).

---

## 9. Стратегия тестирования (TDD RED→GREEN)

**RED, доказывающий баг сегодня** (юнит, `tests/test_trust_staleness.py` новый): строка `{source:"storage_reliability", state:"ok", collector:"ok", semantic:"plausible", evidence_seen_at:T0}`, `now = T0 + 2*порог` → `reevaluate_staleness` обязан вернуть update в `"stale"`. Сегодня функции нет → RED; после — GREEN.

**Обязательные пины (юнит, чистая функция):**
- свежая строка (`age < порог`) → нет update; доменный OK → STALE при `age > порог`.
- **не-доменный источник** (`print_jobs`, `events`) со старым evidence → НИКОГДА не STALE (D1).
- уже-`UNAVAILABLE`/`SUSPECT` строка → без изменения (D7, лестница).
- `evidence_seen_at = NULL` → не STALE (защитно).
- **анти-сброс (ловушка P1-4):** прогнать цикл дважды с растущим `now`; `evidence_seen_at` не изменился, `state` остался `stale` — часы не сброшены. Обязателен.

**Интеграция (`tests/test_trust_staleness.py` или рядом с test_trust_pipeline.py, fixture `client` + throwaway DB):**
- ingest `storage_reliability` OK → домен storage `trusted`; посадить старую улику (`upsert_source_trust(..., received_at="2020-..")` или `now=` далеко в будущее в `run_staleness_cycle`); прогнать цикл → строка `stale`; ingest ДРУГОГО источника (heartbeat) → домен storage `unknown` (D6 propagation). Сегодня цикла нет → RED.
- миграция: pre-existing БД без колонки → `init_db` доальтерит, backfill не NULL.
- db round-trip: `apply_source_staleness` меняет только state/weight/reason, `evidence_seen_at`/`collector_status`/`ts` нетронуты; guard `WHERE evidence_seen_at IS ?` отбрасывает запись, если улика уехала.

**Петля** — smoke-уровнем через `create_app` (как соседние петли; отдельный тест на саму `async`-петлю не пишем — оркестратор `run_staleness_cycle` покрыт напрямую). `cov ≥80%`, целимся в текущие ~94%. Локале-независимость: числа/ISO-даты, enum-значения EN.

---

## 10. План по чанкам (1 ветка = 1 TDD-задача = 1 commit; всего 3, в рамках ≤3-4)

| Чанк | Содержание | Файлы | Класс · Ревью |
|---|---|---|---|
| **Ч1 — серверные часы улики** | колонка `evidence_seen_at` (схема + `_ADD_COLUMNS` + `_BACKFILL`); `upsert_source_trust(+evidence_seen_at)`; `evaluate_trust(+received_at)` + call-site в `ingest_envelope`. RED→GREEN: после ingest `evidence_seen_at` — серверная метка; ре-ingest её двигает; миграция добавляет колонку. | `server/db.py`, `server/pipeline.py`, `tests/test_trust_pipeline.py`(+) | R4 · security-reviewer |
| **Ч2 — чистая логика + read/write** | `server/trust/staleness.py`: `reevaluate_staleness` (+`StaleUpdate`); `db.get_source_trust_rows`, `db.apply_source_staleness` (guard D3). RED = баг-доказывающий тест из §9 + все юнит-пины (вкл. анти-сброс). | `server/trust/staleness.py`(new), `server/trust/__init__.py`(export), `server/db.py`, `tests/test_trust_staleness.py`(new) | R4 · security-reviewer |
| **Ч3 — конфиг + живая петля** | `config.py`: 2 поля; `staleness.run_staleness_cycle` (оркестратор); `main.py`: `_run_source_staleness` + `_source_staleness_loop` + wiring в `create_app` (гейт `interval>0`). Интеграционный end-to-end + config-defaults тест; петля smoke через create_app. | `server/config.py`, `server/trust/staleness.py`, `server/main.py`, тесты | R3 · code-reviewer |

Ч1 — предпосылка (улика должна существовать раньше, чем её читает reeval). Ч2 — ядро логики. Ч3 — оживление вживую. Каждый чанк — самостоятельный RED→GREEN + gate (ruff · mypy[server+shared+client] · bandit · pytest cov≥80% · smoke.py · CHANGELOG-строка · CONTINUITY).

---

## 11. Открытые вопросы (для вычитки)

1. Порог `43200` (12 ч ≈ 3 цикла) — ок, или строже/мягче (напр. 8 ч / 24 ч)?
2. Интервал `3600` (1 ч) — ок, или 4 ч достаточно (детект медленнее, но каданс-сигнал всё равно часовой)?
3. Оставить `identity` вне staleness (сейчас — да, D1)? Протухший identity гейтит device_trust целиком — более тяжёлое следствие; сознательно отложено.
4. Джиттер для этой петли — переиспользовать `netdisco.jitter_sec` или свой малый (напр. 0..60 с)? Сейчас предполагается малый локальный литерал.
