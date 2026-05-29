# SRP MVP — план реализации (клиент + сервер)

## Цель
Минимально работающая система раннего предупреждения отказов Windows-ПК: Python-агент собирает телеметрию → отправляет на FastAPI-сервер (IP по умолчанию 212.42.56.189, настраивается) → сервер хранит, считает 4 day-1 скора + объяснимый Bayesian-риск по классам отказов → веб-дашборд + REST API.

## Архитектура (Python везде)
```
C:\pro\srp\
  shared/        контракт сообщений (pydantic-схемы), общий для клиента и сервера
  client/        агент: config, коллекторы (PowerShell/CIM), транспорт, оффлайн-буфер
  server/        FastAPI: ingest API, SQLite-хранилище, scoring (scores+bayesian), dashboard
  tests/         pytest: схема, scoring, ingest round-trip
```
Принципы из Part 3: тонкий агент / толстый сервер; вся аналитика на сервере; объяснимость by-construction; конфигурируемость через config.json.

## Решения по стеку
- **Агент:** Python stdlib + `requests`; сбор через `powershell -NoProfile ... | ConvertTo-Json` (Get-CimInstance, Get-Counter, Get-WinEvent, powercfg, Get-StorageReliabilityCounter). Без pywin32 — меньше зависимостей при развёртывании.
- **Сервер:** FastAPI + uvicorn, хранилище SQLite (stdlib `sqlite3`), шаблоны Jinja2 для дашборда.
- **Контракт:** 3 типа сообщений — inventory, heartbeat, event-batch (+ day-1 historical как часть первого inventory).
- **Конфиг:** `client/config.json` (`server_url`, `device_id`, интервалы, вкл/выкл коллекторы), `server/config.json` (`host`, `port`, `db_path`).

## Tasks

### Общее / контракт
- [ ] T1: `shared/schema.py` — pydantic-модели Inventory, Heartbeat, EventBatch, HistoricalScan, Envelope (device_id, agent_version, msg_type, ts, payload). → Verify: `python -c "import shared.schema"` без ошибок; pydantic валидирует пример.

### Сервер
- [ ] T2: `server/config.py` + `server/config.json` — загрузка host/port/db_path с дефолтами. → Verify: импорт возвращает Config с host=0.0.0.0, port=8000.
- [ ] T3: `server/db.py` — SQLite-схема (devices, inventory, heartbeats, events, historical, scores) + функции upsert/insert/query. → Verify: создаётся srp.db, таблицы есть (`sqlite3 .tables`).
- [ ] T4: `server/scoring/scores.py` — 4 day-1 скора (Performance, Reliability, Wear, Risk-exposure) из inventory+historical+heartbeat. → Verify: unit-тест на синтетических данных даёт скоры 0–100.
- [ ] T5: `server/scoring/bayesian.py` — лог-оддс агрегация по классам (storage, battery, power_thermal, memory, stability): prior(возраст/MediaType/known-bad) + факторы-улики → posterior + объяснение по факторам. → Verify: unit-тест: больше улик → выше риск; есть explanation-список.
- [ ] T6: `server/api/ingest.py` + `server/api/query.py` — POST `/api/v1/ingest`, GET `/api/v1/devices`, GET `/api/v1/devices/{id}`. Ingest пишет в БД и пересчитывает скоры. → Verify: curl POST примера → 200; GET возвращает устройство со скорами.
- [ ] T7: `server/web/dashboard.py` + templates — `/` (флот: таблица ПК, 4 скора, топ-риск, цвет по уровню), `/device/{id}` (детали: скоры, предсказанные проблемы с объяснением, события). → Verify: открыть http://localhost:8000/ — видно тестовое устройство.
- [ ] T8: `server/main.py` — собрать FastAPI app (api + dashboard), запуск через uvicorn по config. → Verify: `python -m server.main` поднимает сервер на 0.0.0.0:8000.

### Клиент
- [ ] T9: `client/config.py` + `client/config.json` — `server_url=http://212.42.56.189:8000` по умолчанию, device_id (генерация+persist), интервалы. → Verify: импорт даёт нужный server_url; повторный запуск сохраняет тот же device_id.
- [ ] T10: `client/collectors/` — `ps.py` (обёртка вызова PowerShell→JSON), `inventory.py` (Win32_ComputerSystem/BIOS/Processor/Memory/DiskDrive+MediaType, драйверы), `historical.py` (Reliability, Kernel-Power 41 / 6008 / BugCheck 1001, StorageReliabilityCounter, powercfg battery), `heartbeat.py` (Get-Counter: CPU/mem/pagefile/disk), `events.py` (Get-WinEvent whitelist за окно). → Verify: каждый коллектор на этой машине возвращает непустой dict (или degrade-with-flag).
- [ ] T11: `client/transport.py` — POST envelope на server_url с ретраями; оффлайн-буфер (jsonl на диске) при недоступности сервера, дослать при восстановлении. → Verify: при выключенном сервере пишет в буфер; при включённом — отправляет и чистит буфер.
- [ ] T12: `client/agent.py` — главный цикл: при старте inventory+historical, далее heartbeat по интервалу, events по интервалу; self-throttle. → Verify: `python -m client.agent --once` шлёт полный пакет; сервер показывает реальные данные этой машины на дашборде.

### Интеграция и проверка (последней)
- [ ] T13: `requirements.txt` (client/server), `README` запуска, smoke-скрипт. → Verify: чистая установка по README поднимает сервер и агент.
- [ ] T14: E2E: запустить сервер локально, прогнать `agent --once`, открыть дашборд, увидеть реальные скоры этой машины; прогнать pytest. → Verify: дашборд показывает реальное устройство с 4 скорами и топ-рисками; `pytest` зелёный.

## Done When
- [ ] Агент на этой Windows-машине собирает реальную телеметрию и шлёт на сервер.
- [ ] Сервер хранит данные, считает 4 day-1 скора + объяснимый Bayesian-риск по классам.
- [ ] Дашборд показывает флот и детали устройства с объяснением рисков.
- [ ] IP сервера берётся из config.json (дефолт 212.42.56.189), меняется без правки кода.
- [ ] `pytest` зелёный (схема, scoring, ingest round-trip).

## Notes
- MVP-упрощения (осознанные, по Part 3): survival/ML и петля меток отложены — нет меток отказов; скоринг = day-1 эвристики + Bayesian лог-оддс (калибруется позже). Транспорт по HTTP без TLS/аутентификации (для прода — mTLS/токен + HTTPS). SQLite вместо TSDB+Postgres. Агент — консольный/`--once`; обёртка в Windows Service (nssm/pywin32) — следующий шаг.
- Термоданные (темпер./вольтаж/обороты) на офисных ПК обычно недоступны → используем throttle-residency (`% Processor Performance`) как прокси, как и решено в Part 2.
- Безопасность: ingest пишет в БД — параметризованные запросы; на дашборде экранирование (autoescape Jinja2); входные данные валидируются pydantic на границе.
