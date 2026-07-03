# Автообновление агентов — спека + план (2026-07-03)

Цель: агент сам замечает новую версию на СВОЁМ сервере, безопасно скачивает,
проверяет и применяет её без участия пользователя; дашборд показывает версии,
статус и даты. Максимум переиспользования: применитель = существующий
`setup.exe`, транспорт статусов = существующий конвейер конвертов.

## Архитектура (решения и почему)

**Поток:** build.bat → `dist/updates/srp-agent-update-<ver>.zip` + `manifest.json`
→ оператор кладёт оба файла в `server/updates/` → сервер отдаёт манифест/пакет
→ агент (раз в час) сравнивает версии → качает+проверяет → распаковывает в
`C:\SRP\update\staging\` → регистрирует и запускает одноразовую SYSTEM-задачу
«SRP Agent Update» = `staging\setup.exe --update` → сам выходит (exit 0) →
setup стопит задачу/процессы, robocopy payload → `C:\SRP`, icacls, пересоздаёт
задачу «SRP Agent», стартует → новый агент рапортует новую версию.

Ключевые обоснования:
- **Отдельная одноразовая schtasks-задача, не дочерний процесс**: Task Scheduler
  завершает job-object задачи вместе с детьми — дочерний апдейтер умер бы
  вместе с агентом. Своя задача = свой job. Стандартный документированный
  механизм (АВ-совместимость, требование §6 задачи).
- **Применитель = `setup.exe --update`**: боевой код стоп/копия/ACL/задача уже
  есть и протестирован; не плодим второй инсталлятор. Сегодняшний «ручной
  апгрейд тем же BAT» не стопит агента перед robocopy (запущенный exe
  заблокирован) — `--update` это чинит и для ручного пути.
- **Ожидание смерти процесса = rename-probe** (`os.rename` запущенного exe
  невозможен → успех переименования == процесс мёртв), НЕ разбор `tasklist`
  (локализованный вывод = ловушка, инвариант языконезависимости).
- **Целостность+аутентичность**: sha256 обязательно; при заданном `ingest_token`
  манифест несёт `hmac = HMAC-SHA256(token, "<version>|<sha256>")` — MITM без
  токена не подделает пакет. Агент с токеном ТРЕБУЕТ hmac (fail-closed); без
  токена — только sha256 (деградация задокументирована). Downgrade-replay
  отрезан правилом «применять только строго большую версию».
- **Идемпотентность/восстановление**: докачка = `.part` + rename; повторная
  проверка при уже актуальной версии = no-op; `state.json` (target, attempts,
  staged_at) — после рестарта агент сверяет: версия совпала → успех (чистим),
  не совпала и прошло >15 мин → рапорт `failed`; ≤3 попыток на одну целевую
  версию (потолок против бесконечной перекачки битого пакета).
- **«Последнее успешное обновление» = `devices.version_changed_at`** — сервер
  сам замечает смену `agent_version` в конверте; агенту рапортовать не нужно.

## Контракт (аддитивно, БЕЗ bump CONTRACT_VERSION — прецедент liveness)

- `MsgType` += `"update_status"`; `UpdateStatusPayload`:
  `checked_at: Optional[str]`, `state: Literal["ok","updating","failed"]`,
  `error: Optional[str]` (max_length=500), `available_version: Optional[str]`
  (max_length=32). Машинные значения — English; `error` — русская проза.
- Pipeline: ветка `update_status` = `touch_device` + `db.set_update_status`;
  тип входит в no-rescore набор `{events, print_jobs, liveness}` и НЕ проходит
  trust-оценку: гейт `env.msg_type != "liveness"` заменить на
  `env.msg_type not in _NO_TRUST_MSG_TYPES = {"liveness", "update_status"}`
  (иначе подделанный конверт протащит source_health — тот же HIGH, что нашли
  на liveness в B2 ops-fixes).
- Отправка агентом: при старте (результат reconcile), при смене состояния и
  при каждой неудаче; НЕ каждый час (не шумим).

## БД (devices, миграция через `_ADD_COLUMNS`)

Новые колонки: `update_state TEXT`, `update_error TEXT`,
`update_checked_at TEXT`, `version_changed_at TEXT`.
`version_changed_at` ведут `upsert_device`/`touch_device`:
`CASE WHEN excluded.agent_version IS NOT devices.agent_version THEN
excluded.last_seen ELSE devices.version_changed_at END`; на INSERT = recv.
`get_devices`/`get_device` возвращают новые поля.

## Сервер

- `server/updates.py`: `get_update_info(updates_dir, token) -> Optional[dict]`
  — читает `manifest.json` {version, file, sha256, size}, валидирует формат
  версии (`parse_version`), пересчитывает sha256 zip-а (кэш по mtime обоих
  файлов), несоответствие/битость → None + лог (fail-closed: битый пакет не
  предлагается). Добавляет `hmac` при непустом токене.
- `server/config.py`: `updates_dir: str = "server/updates"` +
  `resolved_updates_dir()` (относительный → корень проекта, как db_path).
- `server/main.py`: `app.state.updates_dir = cfg.resolved_updates_dir()`.
- `server/api.py`:
  `GET /api/v1/agent/update` → 404 если пакета нет; иначе
  {version, sha256, size, hmac?}. `GET /api/v1/agent/update/package` →
  FileResponse zip. Оба: токен-проверка как в ingest (hmac.compare_digest,
  401) + `check_rate_limit("endpoint:agent_update")`.
- `server/updates/.gitkeep`; `server/updates/*.zip`+`manifest.json` в .gitignore.

## Агент (`client/updater.py`, чистый stdlib)

Чистые функции (юнит-тестируемые): `_parse_version` (локальная копия, client
не импортирует shared), `select_update(manifest, current, has_token)`,
`verify_hmac(token, version, sha256, hmac)`, `safe_extract(zip_path, dest)`
(zip-slip guard: reject absolute/`..`/drive/backslash-эскейп; после распаковки
обязаны существовать `setup.exe` и `payload/srp-agent.exe`),
`schtasks_update_create_cmd/run_cmd` (argv-списки, стиль setup.py).

Класс `Updater(cfg)`: `check(...)` — fetch манифеста (urllib, X-SRP-Token,
timeout), сравнение, guard попыток, download в `<config_dir>/update/pkg.zip.part`
(поток + sha256 на лету + жёсткий cap = manifest.size и ≤200 МБ) → rename →
hmac-check → `safe_extract` в `update/staging` (staging чистится перед) →
`state.json` → schtasks create+run → вернуть RESTART_PENDING.
`reconcile_after_restart()` — логика успеха/провала по state.json (см. выше).
Гейты: `update_channel == "none"` → выкл; применение (download+staging+spawn)
только при `getattr(sys, "frozen", False)` — dev-режим только проверяет и
рапортует. `ClientConfig` += `update_check_interval_sec: int = 3600`; поле
`agent_version` из ClientConfig УДАЛИТЬ (мёртвый плейсхолдер; версия живёт в
`transport.AGENT_VERSION`, лишний ключ в старых config.json игнорируется
load_config-ом). Хук в `Agent.run_forever`: отдельный due-слот (не collector);
RESTART_PENDING → лог + выход из цикла (exit 0). `run_once` обновление НЕ
трогает (валидационный проход инсталлятора). Первая проверка — со случайной
задержкой 60–600 с (размазать парк). Статусы шлются через существующий
`Transport.send("update_status", …)` (буферизация бесплатно).

## setup.exe `--update` (client/deploy/setup.py)

`SetupOptions` += `update: bool`; `--update` подразумевает quiet, org/server не
требуются (validate пропускает, как uninstall). `run_update(dest=C:\SRP)`:
`_log("update start")` → schtasks /end «SRP Agent» → taskkill агент+трей
(rc игнорируем) → rename-probe ожидание (`srp-agent.exe`, `srp-tray.exe`,
кап 60 с; таймаут → EXIT_COPY_ACL) → robocopy payload→dest (существующий
`robocopy_cmd`, `_KEEP_FILES` уже бережёт config/логи) → `icacls_cmd` →
`_write_task_xml_utf16` + schtasks create /f + /run → уборка `.old`/zip
best-effort. Трей НЕ перезапускаем (SYSTEM не достаёт до user-сессии штатно;
Run-key уже стоит — поднимется при следующем входе; залогировать). Валидационного
прохода нет (сервер заведомо доступен — пакет только что скачан с него).

## Дашборд

- `dashboard.py`: доступная версия = `server/updates` манифест (через
  `request.app.state.updates_dir`), fallback — прежний max по парку;
  `_enrich_fleet(devices, available=…)`.
- `_fleet_body.html`: бейдж «устар.» уже есть; добавить chip `ошибка обновл.`
  (warn) при `update_state == "failed"` + title с датами проверки/обновления.
- `device.html`: строка «агент vX · доступна vY · проверка <дата> ·
  обновлён <дата> · статус <RU-label>». RU-подписи: ok=«актуален»,
  updating=«обновляется», failed=«ошибка обновления».
- `deploy.html`: блок «Обновление агентов»: версия/файл/размер пакета или
  «пакет не выложен»; путь выкладки `server/updates/`.

## Сборка и версия

- `VERSION` (корень) и `client/transport.py:AGENT_VERSION` → `0.2.0`;
  пин-тест равенства (VERSION == AGENT_VERSION) — единый источник не расползётся.
- `packaging/make_update_package.py` (build-only, stdlib): dist/share →
  `dist/updates/srp-agent-update-<ver>.zip` (setup.exe + payload/** + VERSION;
  БЕЗ config.template.json — политика живёт на шаре) + manifest.json
  (version/file/sha256/size). Вызов из `build.bat` + echo-инструкция.
- `docs/agent-install.md`: § «Обновление» переписать на автообновление.

## Задачи (порядок; TDD внутри каждой)

- **T1 контракт+БД+pipeline**: shared/schema.py, server/db.py, server/pipeline.py,
  тесты (schema/ingest/no-trust/no-rescore/version_changed_at/миграция).
- **T2 агент**: client/updater.py, client/agent.py, client/config.py, тесты.
- **T3 инсталлятор**: client/deploy/setup.py --update, тесты (argv/флоу/rename-probe).
- **T4 сервер**: server/updates.py, config.py, main.py, api.py, тесты эндпоинтов.
- **T5 дашборд**: dashboard.py + 3 шаблона, тесты рендера.
- **T6 сборка/докум.**: make_update_package.py, build.bat, VERSION+AGENT_VERSION,
  пин-тест, docs, CHANGELOG.
- Далее: полный гейт (ruff/mypy/bandit/pytest cov≥80%/smoke), security-review
  (обязателен: agent/subprocess/ingest/SQL), merge --no-ff в main, push.

Зависимости: T1 → (T2 ∥ T3 ∥ T4) → (T5 ∥ T6). Файлы задач не пересекаются.

## Не делаем (осознанно)

Каналы beta/staged rollout (update_channel «none» уважаем, остальное = один
канал), UI-загрузку пакета на дашборд (файловая запись через веб = лишняя
поверхность; копирование в каталог — штатный путь), подпись кодом (нет PKI;
HMAC на общем секрете покрывает угрозу LAN-MITM), перезапуск трея из SYSTEM
(WTSQueryUserToken-пляска ради иконки до следующего логона), автопересборку
пакета сервером (build остаётся на dev-машине).
