# Тихое авто-обновление агента — дизайн + план создания

- **Дата:** 2026-06-14
- **Статус:** ДИЗАЙН УТВЕРЖДЁН (3 ключевые развилки выбраны пользователем) → ждёт вычитки спеки перед написанием implementation-плана.
- **Класс задачи:** R4 (новый канал доставки кода на SYSTEM-процесс + агент-PowerShell/привилегии + ingest/auth-поверхность). Security-review обязателен на Ф1–Ф3.
- **Прозовая часть — RU; идентификаторы/файлы/enum-значения/коды выхода — EN** (конвенция §5: operator prose RU, machine values EN).

---

## 1. Цель и критерий успеха

Дать серверу возможность **незаметно для пользователя** обновлять версию агента (и трея) на всём флоте, не ломая инвариант «high-trust / UNKNOWN-over-false-confidence» и **не превращая канал обновления в дыру RCE на весь флот**.

**Критерий успеха:**
- Новая версия доезжает до машины, проверяется и встаёт **без перезагрузки** (перезапуск в тихое окно), пользователь ничего не замечает.
- Плохая версия **не способна окирпичить флот**: авто-откат на машине + поэтапная раскатка + глобальный «стоп» на сервере.
- Непроверенное (нет манифеста / не сошёлся SHA-256 / канал не HTTPS) **никогда не ставится** — агент просто остаётся на текущей версии.
- Инвариант агента жив: `client/` остаётся чистым stdlib; PyInstaller — build-only.
- Контракт расширяется **аддитивно-опционально** → без bump `CONTRACT_VERSION`.

---

## 2. Модель угроз и центральное решение по безопасности

Тихое авто-обновление SYSTEM-агента — **самый опасный канал во всей системе**. Если злоумышленник подменит «новую версию», он получает весь флот с правами SYSTEM.

**Выбор пользователя:** проверка только по **отпечатку SHA-256**, без отдельной подписи (ни HMAC, ни Authenticode).

**Прямое следствие (жёсткий инвариант дизайна):** раз отдельной подписи нет — всю *подлинность* несёт **канал связи**. Поэтому:

- **D-SEC-1 (HTTPS-only gate):** авто-обновление включается, **только** если `server_url` начинается с `https://` и сертификат сервера проверяется (`ssl.create_default_context()`, без отключения проверки). На простом `http://` авто-обновление **полностью выключено** — обновление только вручную через `setup.exe`. Причина: на HTTP сетевой MITM в той же сети отдаёт и фальшивый бинарь, и «правильный» к нему отпечаток.
- **D-SEC-2:** манифест (`target_version` + `sha256` + `size`) и сам файл агент берёт **с того же сервера**, которому уже доверяет по `X-SRP-Token`. Отпечаток приходит по аутентифицированному HTTPS-каналу → он load-bearing.

**Граница (что в зоне действия):** внешний/сетевой злоумышленник (задокументированная external-only модель проекта). Token + проверенный HTTPS-сертификат = граница подлинности; SHA-256 = целостность.

**Принятый остаточный риск (документируется явно):** полностью скомпрометированный SRP-сервер может опубликовать вредоносную версию. Это верно и для HMAC (сервер держал бы ключ); устранил бы только Authenticode с офлайн-ключом, которого у нас нет. **Принято.**

**Будущее дешёвое усиление (не строим сейчас, заложено в §17):** HMAC-SHA256 поверх манифеста секретным `update_key` (чистый stdlib `hmac`) снимает зависимость от HTTPS-канала. Одна-две строки кода. Отложено по решению пользователя.

---

## 3. Не-цели (YAGNI — намеренно не строим)

- Дельта/бинарный-diff апдейты — качаем полный zip версии (~3–4 МБ, дёшево).
- P2P/одноранговое кеширование между агентами.
- SMB-канал доставки (выбрали HTTP с сервера — переносимо для не-доменных сетей).
- Авто-обновление **самого лаунчера** (он стабилен, обновляется только `setup.exe` — см. D4).
- Idle-CPU-детект «простоя» (ненадёжен из session 0); «тихо» = ночное окно ИЛИ нет активной консольной сессии (D9).
- Admin-UI раскатки — настройка файлом (как `org_directory.json`).
- Подпись (HMAC/Authenticode) — отложена (см. §2, §17).

---

## 4. Ключевые решения

- **D1 (механизм замены = лаунчер-надзиратель).** Задача SYSTEM запускает не агента, а крошечный стабильный `srp-launch.exe`. Он читает указатель активной версии, запускает агента дочерним процессом, надзирает. Это единственный устойчивый способ менять залоченный onedir-exe + естественное место логики авто-отката.
- **D2 (версия — в своей папке).** `C:\SRP\versions\<ver>\` — каждая версия отдельным onedir. Новая качается рядом, не трогая запущенную. Переключение = смена указателя `current.txt` + перезапуск.
- **D3 (указатель — файл, не junction).** `current.txt` / `previous.txt` с номером версии, атомарная замена (write-temp + `os.replace`). Проще и тестируемее, чем junction; не требует привилегированных операций. Оба лаунчер-режима читают файл.
- **D4 (лаунчер вне авто-обновления).** Авто-обновляются только `versions\<ver>\` (агент+трей). `srp-launch.exe` + его `_internal\` обновляет только `setup.exe` (ручной доверенный путь ИТ). «То, что выполняет обновления, само меняется только доверенно».
- **D5 (один лаунчер, два режима).** `srp-launch.exe agent` (надзор + откат) и `srp-launch.exe tray` (exec-and-forget). Задача → `agent`; HKLM Run → `tray`.
- **D6 (подлинность = SHA-256 + HTTPS-only).** См. §2.
- **D7 (узнавание = отдельный редкий эндпоинт).** `GET /api/v1/update/check` отдельно от `ingest` (горячий путь и контракт не трогаем). Опрос раз в несколько часов + случайный джиттер (анти-thundering-herd).
- **D8 (тихое окно — без перезагрузки).** Агент применяет готовую проверенную версию только в тихое окно: выходит спец-кодом → лаунчер промоутит и запускает новую. Трей подхватывает новую версию при следующем входе пользователя (без мелькания).
- **D9 (определение «тихо»).** Ночное окно (`update_quiet_hours`, по умолчанию `02-05` локального времени) **ИЛИ** нет активной консольной сессии (`WTSGetActiveConsoleSessionId` через ctypes — stdlib). Покрывает и «офис выключают на ночь», и «ночью включён».
- **D10 (поэтапная раскатка + канарейка).** Сервер решает по `agent_rollout.json`, положена ли версия машине: `enabled` (глобальный стоп), `canary.org_codes`/`dept_codes` (явные когорты), `canary.percent` (детерминированно по `hash(device_id) % 100`, без флаппинга), `min_agent_version` (слишком старые — только вручную).
- **D11 (двойная защита от кирпича).** На машине: (а) crash-loop (новая версия упала ≥3 раз за 5 мин → откат на `previous.txt`); (б) startup-health (новая версия обязана за ≤3 мин записать маркер `started_ok:<ver>` → иначе откат). Маркер = «бинарь стартанул и сам себя инициализировал», НЕ «достучался до сервера» — чтобы недоступность сервера не вызывала ложный откат.
- **D12 (репорт состояния — аддитивно).** Агент в heartbeat шлёт опциональный блок `update_status` (`active_version`/`previous_version`/`last_update_ts`/`last_result`). `last_result: none|ok|rolled_back|failed`. Без bump `CONTRACT_VERSION`.
- **D13 (валидация версии-строки).** Везде, где версия попадает в путь/имя файла: строгий `^\d+\.\d+\.\d+$`. Защита от path-traversal в `versions\<ver>` и download-эндпоинте.
- **D14 (zip-slip-safe распаковка).** Каждую запись zip проверяем: нет `..`, нет абсолютных путей/диска, итог внутри `staging\<ver>\`. Иначе отказ всей версии.
- **D15 (ACL наследуется).** `versions\`, `staging\` наследуют `C:\SRP` (SYSTEM:F, Users:RX) → обычный юзер не может подсунуть/подменить staged-обновление.
- **D16 (миграция плоской установки).** `setup.py` распознаёт текущую плоскую раскладку (`C:\SRP\srp-agent.exe` без `versions\`) и конвертирует в версионную (идемпотентно).

---

## 5. Архитектура — раскладка на диске

```
C:\SRP\
  srp-launch.exe                  ← стабильный лаунчер (обновляет ТОЛЬКО setup.exe; D4)
  _internal\                      ← PyInstaller-зависимости лаунчера (стабильны)
  current.txt                     ← активная версия, напр. "0.2.0" (атомарная замена; D3)
  previous.txt                    ← последняя рабочая (для отката; D11)
  versions\
    0.1.0\  { srp-agent.exe, srp-tray.exe, _internal\, VERSION }
    0.2.0\  { ... }               ← новая, скачана рядом — НЕ трогает запущенную 0.1.0
  staging\<ver>\                  ← скачали zip → распаковали → проверили → промоут в versions\
  update_state.json               ← бухгалтерия лаунчера+агента (см. §7)
  config.json, status.json, print_state.json, spool\, *.log   (как сейчас)
```

ACL `C:\SRP` = SYSTEM:Full, Admins:Full, Users:RX (наследуется на `versions\`/`staging\`; D15).

### Компоненты
| Компонент | Где | Чистота | Роль |
|---|---|---|---|
| `srp-launch.exe` | `client/launcher.py` → build | stdlib | надзор за агентом, промоут, откат, режим tray |
| update-модуль агента | `client/update.py` | stdlib (urllib/hashlib/zipfile/ssl/ctypes) | check → download → verify → stage → решение «применить» |
| server check/download | `server/api.py` (+ `server/updates.py`) | — | манифест + отдача zip, gating по раскатке |
| rollout-настройка | `server/agent_rollout.json` (+ загрузчик, mtime-reload) | — | target/enabled/canary/min_version |
| дашборд раскатки | `server/web/rollout.html` + route | — | живой прогресс: кто на target / на старой / откатился |

---

## 6. Жизненный цикл обновления (data flow)

```
[агент, раз в N часов + джиттер]
  └─ HTTPS-only gate (D-SEC-1): server_url не https → СТОП, ничего не делаем
  └─ GET /api/v1/update/check?device&current   (X-SRP-Token)
       server: rollout-gating (D10) → 204 «нет» | манифест {target_version,url,sha256,size}
  └─ есть target и target>current:
       GET /api/v1/update/download/<version>  → zip в staging\<ver>\
       verify: SHA-256(zip) == manifest.sha256  (иначе drop staging, last_result=failed)
       unzip zip-slip-safe (D14) в staging\<ver>\
       промоут: staging\<ver>\ → versions\<ver>\   (atomic-ish move/rename)
       пометить pending_version=<ver> в update_state.json
  └─ ждать тихого окна (D9)
[тихое окно настало]
  └─ агент: дослать буфер → записать «promote pending» → exit(EXIT_UPDATE_APPLY=90)
[лаунчер видит exit 90]
  └─ previous.txt ← current.txt;  current.txt ← pending_version
  └─ запустить versions\<new>\srp-agent.exe; старт health-таймера (D11)
[новая версия]
  └─ стартанула+инициализировалась → записать started_ok:<new> (≤3 мин)
  └─ первый успешный ingest → update_status.last_result=ok (репорт серверу; D12)
[если плохо]
  └─ crash ≥3/5мин ИЛИ нет started_ok за 3 мин → лаунчер: current.txt ← previous.txt → запустить старую
  └─ агент при первом контакте: last_result=rolled_back → версия краснеет в /rollout, дальше не катится
[трей]
  └─ при следующем входе пользователя HKLM Run перечитает current.txt → новая версия трея
```

---

## 7. Лаунчер — детали

**Коды выхода агента (контракт лаунчер↔агент):**
- `0` / сигнал завершения — система гасится → лаунчер выходит.
- `90` `EXIT_UPDATE_APPLY` — готов промоут; лаунчер переключает версию и перезапускает.
- иное ненулевое — падение; учитывается в crash-loop.

**`update_state.json` (под SYSTEM-ACL):**
```json
{ "pending_version": "0.2.0",
  "started_ok": "0.2.0",
  "healthy": "0.2.0",
  "crashes": [<epoch>, ...],
  "last_result": "ok|rolled_back|failed|none",
  "last_update_ts": "<iso>" }
```

**Цикл `agent`-режима (чистая логика решений вынесена и юнит-тестируется):**
1. прочитать `current.txt` → `V` (валидация D13; пусто/битый → наибольшая валидная в `versions\` как fail-safe).
2. запустить `versions\V\srp-agent.exe --log-file C:\SRP\srp-agent.log` дочерним.
3. дождаться выхода; ветвление по коду (выше).
4. **откат** (D11): `_should_rollback(crashes, started_ok, now, just_updated)` — чистая функция; True → `current ← previous`, `last_result=rolled_back`.
5. устойчивость: при не-апдейтном падении — небольшой backoff и повтор (задача всё равно RestartOnFailure 3×).

**Режим `tray`:** прочитать `current.txt` → exec `versions\V\srp-tray.exe`; без надзора (перезапуск трея = вход в систему).

---

## 8. Сервер — детали

**`GET /api/v1/update/check?device=<id>&current=<ver>`** (заголовок `X-SRP-Token`)
- грузит `agent_rollout.json` (mtime-reload, битый JSON → лог + старая копия, как `org_directory.py`).
- gating (любой → `204 No Content`): `enabled=false`; `current >= target_version`; устройство не в когорте канарейки (D10); `agent_version < min_agent_version`.
- иначе `200` манифест: `{ "target_version", "url":"/api/v1/update/download/<ver>", "sha256", "size" }`. `sha256`/`size` сервер считает из zip на диске (кеш по mtime).

**`GET /api/v1/update/download/<version>`** (`X-SRP-Token`)
- валидация `<version>` строго `^\d+\.\d+\.\d+$` (D13) → нет traversal.
- отдаёт `server/updates/<version>.zip` (StreamingResponse). Файлы кладёт ИТ из `dist\share` (build).
- 404, если версии нет.

**`agent_rollout.json`:**
```json
{ "target_version": "0.2.0",
  "enabled": true,
  "canary": { "org_codes": [7], "dept_codes": [], "percent": 10 },
  "min_agent_version": "0.1.0" }
```
- Канарейка детерминированно: устройство допущено, если `org_code∈org_codes` ИЛИ `dept_code∈dept_codes` ИЛИ `int(sha256(device_id)[:8],16) % 100 < percent`. Стабильно (та же машина — тот же бакет), при росте `percent`→100/`enabled` все сходятся к target.

**Дашборд `/rollout`** (read-only, SSR, autoescape): распределение `active_version` по флоту, сколько `rolled_back`, текущий `target_version` + настройки канарейки. XSS-safe by-construction (как `/deploy`).

---

## 9. Контракт (аддитивно, без bump)

`shared/schema.py`: новая опциональная модель `AgentUpdateStatus`:
```
active_version: str
previous_version: Optional[str]
last_update_ts: Optional[str]
last_result: str   # none|ok|rolled_back|failed  (валидируется множеством)
```
встраивается опциональным полем `update_status` в `HeartbeatPayload`. Старый агент не шлёт → `None`. Сервер: аддитивные колонки `devices.update_last_result/update_previous_version/update_last_ts` (миграция `_migrate_add_columns`, паттерн W0.2). Без bump `CONTRACT_VERSION` (правило §5).

---

## 10. Тихое окно (D9)

Конфиг (аддитивный, дефолты безопасные):
- `update_enabled: bool = true`
- `update_check_interval_sec: int = 14400` (4 ч) + джиттер 0..600 с
- `update_quiet_hours: str = "02-05"` (локальное; пусто = только «нет консольной сессии»)
- применять, если `now ∈ quiet_hours` **ИЛИ** нет активной консольной сессии.

---

## 11. Миграция уже стоящих установок (D16)

Сейчас живьём — **плоская** 0.1.0 (`C:\SRP\srp-agent.exe`). Новый `setup.py`:
1. если есть `C:\SRP\srp-agent.exe` и нет `versions\` → переместить текущие бинарники в `versions\<VERSION>\`, записать `current.txt`.
2. положить `srp-launch.exe` + `_internal\` в корень.
3. перенаправить задачу: `Command=C:\SRP\srp-launch.exe`, `Arguments=agent --log-file C:\SRP\srp-agent.log`; HKLM Run → `srp-launch.exe tray`.
Идемпотентно. Машин пока ~одна (дев) → одноразовый ре-установ безопасен.

---

## 12. Сборка/упаковка

- `packaging/srp.spec`: третий entry `srp-launch.exe` (onedir).
- `build.bat`: кладёт `versions\<VERSION>\` (агент+трей) + лаунчер в `dist\share`; **собирает `versions\<VERSION>.zip`** для отдачи сервером.
- Агент/трей/лаунчер — чистый stdlib; PyInstaller build-only (инвариант жив).

---

## 13. Режимы отказа (failure modes)

| Ситуация | Поведение |
|---|---|
| `server_url` = http:// | авто-обновление выключено целиком (D-SEC-1); версия не меняется |
| нет манифеста / 204 | ничего не делаем |
| SHA-256 не сошёлся | drop `staging\<ver>`, `last_result=failed`, остаёмся на текущей |
| zip-slip в архиве | отказ всей версии (D14) |
| версия-строка кривая | отказ (D13) |
| новая версия падает по кругу | откат на previous (D11a) |
| новая версия «молчун» | откат по таймеру startup-health (D11b) |
| сервер недоступен в момент старта новой | НЕ откат (health = старт бинаря, не сеть; D11) |
| плохая версия уже на канарейке | `enabled=false` (стоп) + версии краснеют в /rollout |
| PC выключен ночами | применит при «нет консольной сессии» или в следующее окно |

---

## 14. Инварианты безопасности (проверит security-reviewer на Ф1–Ф3)

- HTTPS-only gate (D-SEC-1) — единственная не-обходимая точка подлинности при выборе «без подписи».
- Строгая валидация версии-строки в путях + download (D13); zip-slip-safe (D14).
- `staging\`/`versions\` под SYSTEM-ACL (D15) — локальный юзер не подменит.
- Токен/SHA не светятся в логах (агент уже редактирует URL — переиспользуем).
- Лаунчер вне авто-обновления (D4).
- «UNKNOWN не катим»: любое сомнение (нет/битый манифест, не-HTTPS, не сошёлся хеш) → **не обновляемся**, не ставим непроверенное.
- Канарейка ограничивает blast-radius; kill-switch (`enabled=false`) мгновенно останавливает.

---

## 15. План создания по фазам (каждая: ветка → TDD RED→GREEN → gate → subagent-review → merge --no-ff)

| Фаза | Содержание | Модель/effort | Ревью |
|---|---|---|---|
| **Ф1** | Лаунчер `srp-launch.exe` + версионная раскладка + `current/previous.txt` + crash-loop/startup-health откат. Чистая логика решений (`_pick_version`, `_should_rollback`, промоут) юнит-тестируется; тонкий Windows-shell (exec/move). Миграция плоской раскладки в `setup.py` (D16). **Сети нет.** | Opus · max (R4: меняет способ запуска SYSTEM-агента) | security-reviewer |
| **Ф2** | `client/update.py`: `/check`-клиент, download, SHA-256 verify, zip-slip-safe extract в staging, HTTPS-only gate, валидация версии. Сервер `/check` + `/download` + `agent_rollout.json` (базово: enabled+target, без канарейки). Чистые verify/parse функции. | Opus · max (R4: download/ingest/auth-поверхность) | security-reviewer |
| **Ф3** | Тихое окно (D9) + применение (sentinel exit → промоут) + startup-health маркер + репорт `update_status` (контракт аддитивный + db-колонки + миграция). | Opus · high | security-reviewer |
| **Ф4** | Раскатка: канарейка/когорты (чистая функция членства) + дашборд `/rollout`. | Opus · high | code-reviewer |
| **Ф5** | Сборка: `srp-launch` в spec, zip-артефакт в build.bat; docs (`docs/agent-install.md` — раздел авто-обновления). | Opus · low → R2 | code-reviewer |

**Гейт «done» каждой фазы (§6):** ruff · mypy[server+shared+client] · bandit · pytest cov ≥80% · `smoke.py` OK · CHANGELOG-строка на видимое изменение · CONTINUITY обновлён.

---

## 16. Стратегия тестирования

- **Чистые функции (приоритет):** `_pick_version`/`_should_rollback`/промоут-решение; парс+валидация манифеста; SHA-256 verify; zip-entry safety (zip-slip фикстуры); сравнение/формат версий; членство в канарейке; HTTPS-only gate; «тихо ли сейчас» по часам.
- **Сервер:** FastAPI TestClient — auth (нет токена → 401/403), `204` vs манифест, отклонение traversal в `<version>`, gating раскатки (enabled/percent/min_version), download существующей/отсутствующей версии.
- **Тонкий Windows-shell:** exec/move/replace — минимален, smoke-проверка живьём (как этапы трея/setup).
- **Локале-независимость:** числовые коды выхода, English-only парсинг; даты epoch.
- cov ≥80%, целимся в текущие ~94%.

---

## 17. Будущее усиление (отложено, НЕ строим сейчас)

- **HMAC-SHA256 манифеста** секретным `update_key` (stdlib `hmac`) — снимает зависимость от HTTPS-канала; ~1–2 строки. Заложено в форму манифеста (можно добавить поле `sig` позже без ломки).
- **Authenticode** — если организация заведёт сертификат подписи кода: агент проверяет подпись средствами Windows + пин издателя/отпечатка (локале-независимый enum статуса).
- Дельта-апдейты, idle-CPU-детект, авто-обновление лаунчера — по реальной необходимости.

---

## 18. Открытые вопросы (для вычитки)

1. `update_quiet_hours` дефолт `02-05` — подходит, или шире/уже?
2. `update_check_interval_sec` дефолт 4 ч — ок для «незаметно, но не слишком медленно»?
3. Канарейка: достаточно `org/dept + percent`, или нужен ещё явный список `device_id`?
4. Хранить ли больше одной `previous` версии (сейчас — ровно одна, для отката)?
