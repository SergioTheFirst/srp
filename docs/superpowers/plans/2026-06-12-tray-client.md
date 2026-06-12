# Трей-клиент SRP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline,
> по решению пользователя «выполняй по стадиям» — исполнитель = эта же сессия с полным
> контекстом спеки). Шаги — чекбоксы. Гранулярность = задачи этапов; детальный дизайн,
> контракты и тексты — в спеке `docs/superpowers/specs/2026-06-12-tray-client-design.md`
> (v2 + правка: окно предупреждения сертификата = 14 дн).

**Goal:** трей-клиент (сертификаты 4 ч / 1×день×7, IP, пароль), fallback учёта печати,
серверный справочник орг/отделов, установка одной командой из сетевой папки.

**Architecture:** две плоскости — SYSTEM-агент (существующий) пишет односторонний
`status.json`; `srp-tray.exe` в сессии пользователя читает его и сам проверяет
`Cert:\CurrentUser\My`. Сервер декодирует коды орг/отдела render-time из
`org_directory.json`. Деплой: PyInstaller onedir + setup одной командой.

**Tech Stack:** Python 3.9 stdlib (client), ctypes+tkinter (tray), FastAPI+SQLite
(server, существующие), PyInstaller (build-only), PowerShell 5.1 floor.

**Процесс каждого этапа:** ветка `feat/tray-stageN-…` → тесты RED → код GREEN →
targeted pytest → полный gate (`ruff check .` · `mypy` · `bandit -c pyproject.toml -q -r server shared client` ·
`pytest --cov=server --cov=shared` ≥80% · `python smoke.py`) → CHANGELOG (видимое) →
subagent-review (sec для 2/4/5/6, code для 1/3) → fix → `merge --no-ff` в main →
`push origin main` → CONTINUITY + память.

---

## Этап 1 — status.json + счётчики печати (R2; ветка feat/tray-stage1-status-json)

**Files:** Create `client/status_writer.py`, `tests/test_status_writer.py`;
Modify `client/transport.py` (счётчики исходов), `client/agent.py` (вызов в цикле),
`client/collectors/print_jobs.py` (daily-аккумулятор в print_state.json).

- [ ] Тесты RED: `build_status()` собирает все поля спеки §1; пин-тест «нет секретов»
  (сериализованный JSON не содержит ingest_token/password_hash/server creds);
  `write_status()` атомарен (tmp+os.replace) и переживает OSError молча (лог);
  `_lan_ips()` фильтрует не-RFC1918/loopback; print_state: страницы суммируются по
  локальной дате, prune > 62 дней, today/month считаются верно на границе месяца.
- [ ] Transport: атрибуты `last_ok_ts: Optional[float]`, `last_error: str`,
  метод `buffer_depth() -> int`; обновление в `_deliver`/`_attempt` исходах.
- [ ] print_jobs: `_accumulate_daily(state, jobs, today)` чистая; вызов в сборщике.
- [ ] status_writer: `build_status(cfg, transport, print_counters, now)` чистая +
  `write_status(path, doc)`; диск C: через `shutil.disk_usage(env SystemDrive)`,
  аптайм `GetTickCount64` через ctypes (None вне Windows); status-путь =
  `cfg.resolved_buffer_path().with_name("status.json")`.
- [ ] agent.py: писать status.json в конце `run_once` и каждой итерации `run_forever`.
- [ ] Gate → CHANGELOG («агент публикует status.json для трея») → review → merge → push.

## Этап 2 — print fallback counter (R3+sec; ветка feat/tray-stage2-print-fallback)

**Files:** Modify `client/collectors/print_jobs.py` (авторежим + counter-сборщик),
`shared/schema.py` (PrintJob.source: Optional[str]), `server/db.py` (колонка source
additive + store), `server/pipeline.py` (прокинуть), `tests/test_print_fallback.py`,
существующие тесты печати.

- [ ] ЖИВАЯ ПРОВЕРКА на этой машине: `Get-CimInstance Win32_PerfFormattedData_Spooler_PrintQueue`
  — имена свойств/поведение счётчика; результат зафиксировать в плане (verify-at-impl спеки §12).
- [ ] Тесты RED: выбор режима (IsEnabled true/false/ошибка→counter? нет: ошибка
  проверки → events-попытка как раньше, мягко); дельты (рост, ноль, reset спулера
  cur<base → дельта=cur); фильтр `_VIRTUAL` по Name; переходы events→counter
  (baseline=current, без ретро) и counter→events (last_sweep_ts=now) без двойного
  счёта; строки counter: job_id=None, user_name=None, source="counter";
  events-строки получают source="events"; контракт: payload без source валиден
  (старый агент), с source — хранится.
- [ ] PS: `(Get-WinEvent -ListLog '…PrintService/Operational').IsEnabled` → bool JSON;
  counter-скрипт CIM → [{name, pages}] числа.
- [ ] print_state.json v2: {last_sweep_ts, mode, baselines{}, daily{}} (+миграция от v1 формата).
- [ ] Server: `_migrate_add_columns` print_jobs.source TEXT; store_print_jobs пишет;
  CSV-экспорт + колонка source.
- [ ] Gate → CHANGELOG → security-review (агент PS) → merge → push.

## Этап 3 — справочник организаций (R3; ветка feat/tray-stage3-org-directory)

**Files:** Create `server/org_directory.py`, `org_directory.json` (пример-шаблон),
`tests/test_org_directory.py`; Modify `server/config.py` (path+env), `server/db.py`
(print-аналитика COALESCE на dept_code), `server/web/dashboard.py` + шаблоны
`fleet.html`/`device.html`/`print.html` (имена, чип «нет в справочнике», версия
агента + «новых за 7 дн»), `server/api.py` (CSV +org_name/dept_name).

- [ ] Тесты RED: load+decode (org/dept), unknown → None (рендер покажет код+чип),
  mtime-reload (правка файла подхватывается без рестарта), битый JSON → старая
  копия + лог, отсутствующий файл → пустой справочник; /print группирует по
  COALESCE(имя по dept_code, devices.department, 'Без отдела'); CSV содержит
  org_name/dept_name; fleet: группы орг→отдел, чип unknown-кода, колонка версии.
- [ ] device.html: edit-поле department → comment (PATCH meta расширить полем comment,
  department принимается но deprecated в доке API).
- [ ] Gate → CHANGELOG → review → merge → push.

## Этап 4 — tray core (R3+sec(пароль); ветка feat/tray-stage4-core)

**Files:** Create `client/tray/__init__.py`, `__main__.py`, `icon.py` (ctypes
Shell_NotifyIcon + hidden window + menu + TaskbarCreated), `panel.py` (tkinter,
режим --panel/--ask-password дочерним процессом), `state.py` (status.json чтение/
свежесть, icon_state, tray_state, lan_ips live, клипборд-строка), `assets/srp_{ok,warn,alert}.ico`;
`tests/test_tray_state.py`, `tests/test_tray_panel_logic.py`.

- [ ] Тесты RED (чистые части): `icon_state` worst-of матрица; freshness 15 мин;
  «Скопировать для поддержки» строка; парольный гейт (verify + lockout 3×/5 мин,
  персистентность в tray_state.json); single-instance решение по коду ошибки мьютекса.
- [ ] icon.py: только адаптер win32 (без логики) — NIM_ADD/MODIFY/DELETE, балун
  NIF_INFO, меню (Открыть/Обновить/О программе/Выход), WM_LBUTTONUP → панель.
- [ ] panel.py: ttk-страница по спеке §2 (8 строк + 2 кнопки), пароль-диалог;
  дочерний запуск `sys.executable --panel` (frozen) / `-m client.tray --panel`.
- [ ] Ручной чек-лист (зафиксировать результат в PR/ledger): иконка появляется,
  балун показывается, перезапуск explorer возвращает иконку, выход требует пароль.
- [ ] Gate (cov не падает — client вне cov-скоупа, логика всё равно покрыта) →
  CHANGELOG → security-review (пароль/ACL-чтение) → merge → push.

## Этап 5 — cert-движок v2 (R4; ветка feat/tray-stage5-certs)

**Files:** Create `client/tray/certs.py`, `tests/test_tray_certs.py`;
Modify `client/tray/__main__.py` (цикл 30 мин), `client/config.py`
(tray_cert_warn_days=14, tray_notify_hours=4, tray_require_cert=False, helpdesk_contact="").

- [ ] Тесты RED: парсер PS-JSON (фикстуры: RU-subject CN, несколько сертов, без
  private key, истёкший); subject-группировка (преемник того же CN гасит старый
  до и после истечения; разные CN независимы); `should_nag`: >14 тишина; 8–14
  каждые 4 ч (граница ровно 4 ч, анти-спам через перезапуск state); ≤7 каждые
  4 ч red; истёк ≤7 дн → ровно 1/календарный день (вторая проверка того же дня
  молчит); истёк >7 дн → тишина, состояние red остаётся; инфо-балун преемника
  однократен (new_cert_announced); require_cert=true + пусто → 1/день.
- [ ] PS-запрос: Cert:\CurrentUser\My, HasPrivateKey, epoch-даты (DateTimeOffset),
  subject/issuer/thumbprint; таймаут/ошибка PS → состояние «неизвестно» (не red).
- [ ] Интеграция в трей-цикл; тексты RU из спеки §3.
- [ ] Gate → CHANGELOG → security-review (cert/privacy/PS) → merge → push.

## Этап 6 — build + setup одной командой (R4; ветка feat/tray-stage6-setup)

**Files:** Create `build.bat`, `packaging/srp.spec` (PyInstaller: agent console
onedir + tray windowed onedir + setup onefile uac-admin), `client/deploy/setup.py`
(логика setup.exe), `client/deploy/task_template.xml`, `requirements-build.txt`,
`docs/deploy-share-README.md`; Modify `README.md` (раздел деплоя);
`tests/test_setup_logic.py` (чистые части).

- [ ] Тесты RED (чистая логика setup): разбор параметров таблицы спеки §6 +
  авто-quiet (server+org заданы), валидация org/dept `^[A-Za-z0-9_-]{1,16}$`,
  exit codes 0/2/3/4/5, merge config (device_id сохраняется, пароль → hash,
  template+параметры, UTF-8 без BOM), построение командных строк robocopy/icacls/
  schtasks/reg/wevtutil (строки сравниваются в тестах, не исполняются).
- [ ] setup.py: шаги спеки §6 (UAC-манифест на exe; robocopy payload; icacls;
  config; wevtutil best-effort; валидация `srp-agent.exe --once` (--allow-offline
  смягчает); schtasks /xml; HKLM Run (кроме --no-tray); старт; отчёт RU;
  install.log append; --uninstall/--purge).
- [ ] build.bat + spec: три артефакта в dist\share\ (+ config.template.json, VERSION).
- [ ] Gate → CHANGELOG → security-review (installer/ACL/секреты) → merge → push.

## Этапы 7–8 (опц., по команде пользователя)
7. Генератор BAT в дашборде (плейсхолдеры секретов). 8. user-cert спул в fleet.

## Self-review (выполнен)
Покрытие спеки: §1→Э1, §5→Э2, §7→Э3, §2/§4→Э4, §3→Э5, §6→Э6, §8 размазан по
sec-review этапов, §9 → тесты этапов. Типы/имена сквозные: `build_status`/`icon_state`/
`should_nag`/`source`/`tray_cert_warn_days=14` согласованы со спекой.
