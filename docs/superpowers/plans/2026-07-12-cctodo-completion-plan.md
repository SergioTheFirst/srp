# CCTODO Completion Plan — завершение cctodo.md без регрессий + выгрузка srp-agent.exe

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Модель:** каждая задача = R2 (Sonnet · max effort) — это исполнение УТВЕРЖДЁННОГО плана (CLAUDE.md §2). Ревью перед merge: security-reviewer (Opus) для Task 1/2/4/5 (агент/инсталлер/ingest), code-reviewer (Sonnet) для остальных.

**Goal:** Закрыть все реально открытые пункты `cctodo.md` (аудит 2026-07-12: ~30 из 40 уже сделаны другими тредами) + два владельческих фикса: «Выход» в трее и `setup --uninstall` обязаны выгружать процесс `srp-agent.exe` из памяти.

**Architecture:** Только аддитивные изменения поверх текущего кода (trust-слой, ssd3 D/R/O, score100 — НЕ трогаем). Два фикса переиспользуют уже существующий stop-механизм пути обновления (`_stop_running_processes`). Async-rescore — коалесцирующий фоновый воркер с sync-дефолтом в коде и включением в shipped-конфиге (правило no-dormant).

**Tech Stack:** Python 3.9 floor, stdlib-only агент, FastAPI+SQLite сервер, pytest, ctypes/Win32 в трее, PyInstaller (build-only).

## Global Constraints (из CLAUDE.md §5 — нарушение = провал задачи)

- Агент `client/` = pure stdlib, ноль зависимостей (urllib/subprocess/json/winreg/ctypes).
- Agent PowerShell = Windows PowerShell 5.1 floor; языконезависимый сбор (никогда не парсить локализованный текст).
- Сервер: Jinja2 autoescape ON (без `|safe`), ВЕСЬ SQL параметризован (имена таблиц — только из закрытых литеральных множеств + проверка), pydantic v2 на границе; новые поля схемы — только additive-optional (без бампа CONTRACT_VERSION).
- Trust: `state`=gate, `weight`=modulation; UNKNOWN over false confidence. Семантику `server/trust/`, `server/analytics/health.py`, `server/scoring/score100.py` НЕ менять.
- RU для операторского текста (дашборд/факторы), EN для machine values (enums, states, bands); tech-термины латиницей.
- Python: line 100, двойные кавычки, явный `Optional` (не `|`), файлы <800 строк, функции <50, ранние возвраты. PostToolUse-хук гоняет `ruff --fix`+`format` на каждый `.py` — добавляйте import вместе с первым использованием, иначе хук его выпилит.
- Git: branch-first → гейт зелёный → subagent-ревью → `merge --no-ff` → `push origin main` — автоматически, НЕ спрашивая. Стейджить ТОЛЬКО файлы своей задачи (никогда `git add -A`); локальные `client/config.json`/`org_directory.json` не коммитить.
- No dormant features: всё OFF-by-default в коде включается в `server/config.json` в ТОЙ ЖЕ задаче.

**Полный гейт перед каждым merge (все зелёные, без исключений):**

```bash
python -m ruff check .
python -m mypy
python -m bandit -c pyproject.toml -q -r server shared client
python -m pytest --cov=server --cov=shared --cov-report=term-missing   # fail_under 80
python smoke.py
```

+ строка в `CHANGELOG.md` `## [Unreleased]` в том же коммите (для видимых изменений) + обновление `CONTINUITY.md`.

**Известные капканы среды (НЕ чинить в рамках этих задач, не приписывать себе):**
- `smoke.py`/`test_print_tracking.py` имеют «date timebomb» (фикстурная дата против скользящего 30-дневного окна) — если падает, проверь через `git stash`, что падение не от твоего диффа.
- 4 старых тестовых файла зависают на немокнутом SMART I/O (`collect_historical`) на некоторых машинах — известная проблема среды, не связана с этим планом.
- Итог по каждому пункту cctodo проверяй по карте ниже, а не по устаревшим номерам строк внутри cctodo.md.

---

## Карта статуса cctodo.md (аудит кода 2026-07-12) — ЧТО НЕ ПЕРЕДЕЛЫВАТЬ

**УЖЕ СДЕЛАНО (пере-реализация = регрессия; при сомнении — открой файл-доказательство):**

| Пункт cctodo | Доказательство в коде |
|---|---|
| W0.1 append-only `historical`/`scores` | `server/db.py:124-132,165-175,437-460` (id PK AUTOINCREMENT, plain INSERT, миграция `*__new`) |
| W0.1 heartbeats downsample | rollup-таблицы + `run_daily_rollup` (`server/db.py:1140-1180`, `server/main.py:86`), `rollup_days=730` |
| W0.2 `received_at` + клок-дрейф | `server/pipeline.py:304-384,104-125` (`_CLOCK_DRIFT_FLAG_SEC=300`) |
| W0.3 весь trust-слой | `server/trust/*`, `client/collectors/sources.py` (ok/partial/empty/timeout/blocked/absent), device.html «Покрытие источников» |
| W0.4 CONTRACT_VERSION + compat-тесты | `shared/schema.py:25`, `tests/test_contract_compat.py`, негоциация в ответе ingest |
| W0.5 confidence-gated scoring + бэнды | `server/scoring/score100.py` `_gate_axis` (UNKNOWN вместо 100), бэнды low/elevated/high/critical |
| P1 ingest auth / body-cap / rate-limit | `server/api.py:37-48`, `server/ingest_guards.py`, `server/main.py:31-48` (512KB, 30/мин) |
| P1 дефолт server_url | `client/config.py:46,230-235` (пустой + hard-error; публичного IP нет) |
| P1 SYSTEM-автостарт | schtasks SYSTEM (`client/deploy/task_template.xml`) — «Windows Service» закрыт ЭТИМ механизмом |
| P1 transport jitter + idempotency | `client/transport.py:33-36,131-134,145` + серверный дедуп `ingest_guards` |
| P1 org/dept коды + справочник | `server/org_directory.py`, `shared/schema.py:416`, чип «нет в спр.» на флоте |
| W4.1 тренд-движок slope+ETA | `server/analytics/trends.py` (Theil-Sen, storage_wear/battery/disk_fill/boot_time/throttle) |
| W4.2 все 5 доменных движков | `server/analytics/{storage,battery,disk_fill,os_degradation,software_aging,fleet_anomaly}.py` |
| W4.3 KP41 демотирован, WHEA убран | `server/scoring/bayesian.py:210-253` (KP41 только при анкоре) |
| §5 дашборд целиком | тренды/staleness/фильтры/predictive-vs-incident/«Новые эскалации» (`server/web/health_view.py:141-161`) |
| §6 тест-долг целиком | `tests/test_collectors_parsers.py`, `test_locale_fixtures.py`, `test_scoring_boundaries.py`, mypy `files=["shared","server","client"]` |
| §6 /metrics + /pipeline | `server/api.py:108-111`, `server/db.py:3793+`, `server/web/templates/pipeline.html` |

**ЗАКРЫВАЕТСЯ РЕШЕНИЕМ, НЕ КОДОМ (фиксируется в Task 9):**
- «Windows Service (nssm/sc)» → SUPERSEDED (schtasks-SYSTEM решает ту же цель, задокументировано).
- «Signed config» → ОТКЛОНЕНО: ключ жил бы в той же ACL-папке, что и конфиг — театр; реальная защита = `icacls /inheritance:r` + HMAC-подписанный update-манифест (`client/updater.py:86-89`).
- «Per-source confidence в каждый движок глубже» → достигнут потолок скоупа (CONTINUITY: «no confidence calculus»); гейтинг уже сквозной через `_gate_axis` + identity-gate.
- fleet_anomaly когорта по model (без build) → задокументированное решение (`fleet_anomaly.py:29,73-74`), сайтовый KP41-кластер сделан.
- «Структурные JSON-логи» → отложено: однобокс, логи человекочитаемые; operate-ability закрывают метрики (Task 8).
- §7 L1-intake (ITSM/CSV), L2 EWMA, L3 survival → **ГЕЙТЫ НЕ ПРОЙДЕНЫ — НЕ СТРОИТЬ.** `rulestats` (Ф8) — это самоподтверждение правил, НЕ замена L1; не расширять его в label-loop без реального потребителя.

**РЕАЛЬНО ОТКРЫТО (задачи этого плана):** два владельческих фикса (Task 1-2), иерархия рисков на карточке (Task 3), async rescore W4.0 (Task 4), клиентский cap payload (Task 5), D9 compressed raw + replay-тест (Task 6-7), метрики лок/отказы (Task 8), закрытие cctodo.md (Task 9).

## Правила не-регрессии (проверяй в каждой задаче)

1. НЕ удалять и не «чистить» legacy-скоры П/Н/И/Риск — они остаются рабочими осями (score100), тесты их пинят.
2. НЕ трогать `server/trust/`, `health.py`-семантику, веса/пороги scoring (это R4 — вне плана).
3. Схема БД: только новые таблицы/колонки; новая device-scoped таблица ОБЯЗАНА попасть в `_DEVICE_TABLES` (`server/db.py`) — иначе утечёт при удалении устройства (грабли Ф8).
4. Дефолтное поведение тестов не меняется: любой новый фоновой механизм в коде default-OFF + включение в `server/config.json` (no-dormant).
5. Каждая задача — отдельная ветка; после ВСЕХ задач — один финальный whole-branch review (память: task-scoped ревью слепо к cross-task багам).

---

### Task 1: `setup --uninstall` выгружает srp-agent.exe из памяти

**Files:**
- Modify: `client/deploy/setup.py:529-537` (`run_uninstall`)
- Modify: `client/deploy/uninstall-service.ps1` (паритет legacy-скрипта)
- Test: `tests/test_setup_update.py` (переиспользует `_fake_run`/`_tag` этого файла)
- Modify: `CHANGELOG.md` (`## [Unreleased]` → `### Fixed`)

**Interfaces:**
- Consumes: уже существующие `_stop_running_processes(dest) -> bool` (setup.py:472-483: schtasks /end → taskkill agent → taskkill tray → wait unlock 60s), `schtasks_delete_cmd()`, `reg_delete_run_cmd()`, `_run`, `_log`.
- Produces: `run_uninstall` с порядком команд `schtasks:/end → taskkill:srp-agent.exe → taskkill:srp-tray.exe → schtasks:/delete → reg` (тесты Task 2 не зависят, но security-review общий).

Контекст бага: сейчас `run_uninstall` делает `schtasks /end` + `taskkill` ТОЛЬКО трея — `srp-agent.exe` остаётся в памяти, а при `--purge` `rmtree` молча не может удалить залоченный exe. Путь обновления (`run_update`) уже делает правильно через `_stop_running_processes` — переиспользуем его. Таймаут разблокировки НЕ абортит uninstall (снять автостарт всё равно ценнее).

- [ ] **Step 1: ветка**

```bash
git checkout -b fix/uninstall-unload-agent
```

- [ ] **Step 2: два падающих теста** — добавить в конец `tests/test_setup_update.py`:

```python
# --------------------------------------------------------------------------- #
# run_uninstall: агент обязан быть выгружен из памяти (owner-fix 2026-07-12)
# --------------------------------------------------------------------------- #


def test_run_uninstall_unloads_agent_and_tray_before_deregistering(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "SRP"
    dest.mkdir()
    fake_run, calls = _fake_run({})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: True)

    rc = su.run_uninstall(su.SetupOptions(uninstall=True), dest=str(dest))

    assert rc == su.EXIT_OK
    assert [_tag(c) for c in calls] == [
        "schtasks:/end",
        "taskkill:srp-agent.exe",
        "taskkill:srp-tray.exe",
        "schtasks:/delete",
        "reg",
    ]


def test_run_uninstall_purge_proceeds_even_after_unlock_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "SRP"
    dest.mkdir()
    (dest / "config.json").write_text("{}", encoding="utf-8")
    fake_run, calls = _fake_run({})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: False)

    rc = su.run_uninstall(su.SetupOptions(uninstall=True, purge=True), dest=str(dest))

    assert rc == su.EXIT_OK
    assert "taskkill:srp-agent.exe" in [_tag(c) for c in calls]
    assert not dest.exists()
```

- [ ] **Step 3: убедиться, что падают**

Run: `python -m pytest tests/test_setup_update.py -q -k uninstall`
Expected: FAIL — первый тест видит `["schtasks:/end", "taskkill:srp-tray.exe", ...]` без `taskkill:srp-agent.exe`.

- [ ] **Step 4: минимальная правка** — заменить `run_uninstall` в `client/deploy/setup.py` целиком:

```python
def run_uninstall(opts: SetupOptions, *, dest: str = DEST) -> int:
    # Выгрузить ОБА процесса из памяти (в этом смысл uninstall), переиспользуя
    # stop-путь обновления: schtasks /end -> taskkill agent+tray -> ждать
    # разблокировки EXE. Таймаут логируем, но НЕ абортим: снять автостарт и
    # Run-ключ всё равно строго лучше, чем оставить их.
    if not _stop_running_processes(dest):
        _log(dest, "uninstall: агент/трей не освободили файлы за 60 с -- продолжаю")
    _run(schtasks_delete_cmd())
    _run(reg_delete_run_cmd())
    if opts.purge:
        shutil.rmtree(dest, ignore_errors=True)
    _log(dest, f"uninstall done (purge={opts.purge})")
    return EXIT_OK
```

- [ ] **Step 5: паритет legacy-скрипта** — в `client/deploy/uninstall-service.ps1` после строки `Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false` добавить:

```powershell
# Выгрузить процесс агента (паритет с setup.exe --uninstall): задача может быть
# уже остановлена, а srp-agent.exe -- продолжать работать (ручной запуск).
try { Stop-Process -Name "srp-agent" -Force -ErrorAction Stop } catch {}
```

(PowerShell 5.1-совместимо: `Stop-Process -Name` существует в 5.1; пустой catch — процесс может не существовать, это норма.)

- [ ] **Step 6: тесты зелёные**

Run: `python -m pytest tests/test_setup_update.py tests/test_setup_logic.py -q`
Expected: PASS (все, включая существующие — сигнатура и коды выхода не менялись).

- [ ] **Step 7: CHANGELOG** — в `## [Unreleased]` → `### Fixed`:

```markdown
- `setup --uninstall` теперь выгружает `srp-agent.exe` из памяти: останавливает задачу, снимает оба процесса и ждёт освобождения файлов (раньше агент оставался работать, а `--purge` не мог удалить залоченный exe).
```

- [ ] **Step 8: полный гейт** (блок команд из Global Constraints) — все зелёные.
- [ ] **Step 9: security-review** — subagent `security-reviewer` (Opus) на дифф ветки (инсталлер = обязательный триггер). Править CRITICAL/HIGH до merge.
- [ ] **Step 10: коммит + merge + push**

```bash
git add client/deploy/setup.py client/deploy/uninstall-service.ps1 tests/test_setup_update.py CHANGELOG.md
git commit -m "fix(setup): --uninstall выгружает srp-agent.exe (stop task + taskkill + wait unlock)"
git checkout main && git merge --no-ff fix/uninstall-unload-agent && git push origin main
```

---

### Task 2: «Выход» в трее останавливает SYSTEM-агента (через UAC)

**Files:**
- Modify: `client/tray/__main__.py` (`request_exit` :211-214, `_parse_args` :237-243, `main` :246-276, новые хелперы)
- Create: `tests/test_tray_exit.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `client.deploy.setup.schtasks_stop_cmd() -> list[str]`, `taskkill_agent_cmd() -> list[str]`, `_wait_files_unlocked(paths, timeout_sec) -> bool`, `DEST="C:\\SRP"`, `AGENT_EXE="srp-agent.exe"`; `client.winflags.NO_WINDOW`; `panel.run_password_prompt` (rc 0 = пароль верен/не задан).
- Produces: `run_stop_agent(runner=None, wait_unlocked=None, alert=_alert) -> int` (0 = агент остановлен), `_stop_agent_params(frozen: bool) -> str`, CLI-флаг `srp-tray --stop-agent`.

**Дизайн-решения (не менять при исполнении):**
1. Трей работает под обычным пользователем; агент — SYSTEM. Убить SYSTEM-процесс без прав нельзя, и это ПРАВИЛЬНО — иначе любой пользователь молча выключал бы мониторинг. Поэтому остановка идёт через `ShellExecuteW("runas", ...)` = штатный UAC-запрос. Отклонил UAC → иконка закрывается, но пользователь ЧЕСТНО предупреждается, что агент продолжает работать (About-текст трея уже обещает ровно эту модель: «пароль защищает от случайного закрытия, не от администратора»).
2. **Отвергнутая альтернатива — стоп-флаг в user-writable `C:\SRP\spool`:** это дало бы любому непривилегированному процессу канал управления SYSTEM-агентом (kill switch телеметрии) — регрессия безопасности. Не реализовывать.
3. Порядок в elevated-режиме: СНАЧАЛА `schtasks /end` (гасит инстанс задачи — иначе `RestartOnFailure 3x/1min` из task_template.xml воскресит агента после голого taskkill), ПОТОМ `taskkill /im srp-agent.exe /f` (добивает процессы вне задачи). Коды возврата обеих команд игнорируются (задача может не бежать, процесса может не быть); честный сигнал успеха — разблокировка `C:\SRP\srp-agent.exe`.
4. «Выход» выгружает агента ИЗ ПАМЯТИ, но задача остаётся зарегистрированной — после перезагрузки агент вернётся. Это осознанная семантика (полное удаление = `setup --uninstall`).
5. PyInstaller: `client/tray/__main__.py` статически импортирует `client.deploy.setup` внутри функции — modulegraph это трейсит, srp-tray.exe получит модуль автоматически (spec менять не нужно).

- [ ] **Step 1: ветка**

```bash
git checkout -b feat/tray-exit-stops-agent
```

- [ ] **Step 2: падающие тесты** — создать `tests/test_tray_exit.py`:

```python
"""Трей «Выход» -> elevated --stop-agent: чистые части, без Win32-вызовов.

ctypes-обвязка (ShellExecuteW / MessageBoxW) остаётся тонкой и нетестируемой —
та же политика, что в client/tray/icon.py; всё решающее инжектируется.
"""

from __future__ import annotations

from client.tray.__main__ import _parse_args, _stop_agent_params, run_stop_agent


def test_parse_args_stop_agent_flag() -> None:
    assert _parse_args(["--stop-agent"]).stop_agent is True
    assert _parse_args([]).stop_agent is False


def test_stop_agent_params_frozen_vs_dev() -> None:
    assert _stop_agent_params(True) == "--stop-agent"
    assert _stop_agent_params(False) == "-m client.tray --stop-agent"


def test_run_stop_agent_ends_task_before_taskkill_then_ok() -> None:
    calls: list[list[str]] = []

    def fail_alert(_msg: str) -> None:
        raise AssertionError("no alert expected on success")

    rc = run_stop_agent(
        runner=lambda cmd: calls.append(cmd) or 0,
        wait_unlocked=lambda: True,
        alert=fail_alert,
    )

    assert rc == 0
    assert [c[0] for c in calls] == ["schtasks", "taskkill"]
    assert calls[0][:2] == ["schtasks", "/end"]
    assert "srp-agent.exe" in calls[1]


def test_run_stop_agent_locked_exe_alerts_and_returns_1() -> None:
    alerts: list[str] = []

    rc = run_stop_agent(runner=lambda cmd: 0, wait_unlocked=lambda: False, alert=alerts.append)

    assert rc == 1
    assert alerts and "srp-agent.exe" in alerts[0]
```

- [ ] **Step 3: убедиться, что падают**

Run: `python -m pytest tests/test_tray_exit.py -q`
Expected: FAIL — `ImportError: cannot import name '_stop_agent_params'`.

- [ ] **Step 4: реализация** в `client/tray/__main__.py`.

4a. Расширить импорт typing (строка 30): `from typing import Callable, Optional`.

4b. После константы `_MUTEX_NAME` добавить:

```python
_AGENT_STOP_TIMEOUT_SEC = 15.0  # elevated --stop-agent: ждать разблокировки srp-agent.exe
```

4c. Перед `_acquire_single_instance` добавить модульные хелперы:

```python
# --------------------------------------------------------------------------- #
# «Выход» = остановить и SYSTEM-агента (owner-fix 2026-07-12)
# --------------------------------------------------------------------------- #


def _stop_agent_params(frozen: bool) -> str:
    """Строка параметров ShellExecuteW для elevated-ребёнка --stop-agent."""
    return "--stop-agent" if frozen else "-m client.tray --stop-agent"


def _alert(text: str) -> None:
    """Модальное предупреждение (0x30 = MB_OK | MB_ICONWARNING)."""
    ctypes.windll.user32.MessageBoxW(None, text, "SRP", 0x30)


def _launch_stop_agent_elevated() -> bool:
    """UAC-элевация этого же exe с --stop-agent; True = ребёнок запущен.

    ShellExecuteW возвращает > 32 при успехе (контракт WinAPI); <= 32 покрывает
    отказ UAC (SE_ERR_ACCESSDENIED=5) и ошибки запуска. restype пиним в
    c_void_p: дефолтный c_int обрезает 64-битный HINSTANCE (та же причина, по
    которой icon.py пинит сигнатуры).
    """
    shell = ctypes.windll.shell32
    shell.ShellExecuteW.restype = ctypes.c_void_p
    rc = shell.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        _stop_agent_params(bool(getattr(sys, "frozen", False))),
        None,
        0,  # SW_HIDE -- у ребёнка нет окна, весь его UI = MessageBox при провале
    )
    return int(rc or 0) > 32


def run_stop_agent(
    runner: Optional[Callable[[list[str]], int]] = None,
    wait_unlocked: Optional[Callable[[], bool]] = None,
    alert: Callable[[str], None] = _alert,
) -> int:
    """Elevated-ребёнок (``srp-tray --stop-agent``): выгрузить SYSTEM-агента.

    Порядок обязателен: сначала ``schtasks /end`` гасит инстанс задачи — иначе
    её RestartOnFailure (3x/1min, task_template.xml) воскресит агента после
    голого taskkill; затем ``taskkill`` добивает процессы вне задачи. Коды
    возврата игнорируются (задача может не бежать, процесса может не быть);
    честный сигнал успеха — разблокировка EXE. Задача остаётся
    зарегистрированной: после перезагрузки агент вернётся — «Выход» выгружает
    из памяти, а не удаляет (полное удаление = setup --uninstall).
    """
    from client.deploy.setup import (
        AGENT_EXE,
        DEST,
        _wait_files_unlocked,
        schtasks_stop_cmd,
        taskkill_agent_cmd,
    )

    def _default_runner(cmd: list[str]) -> int:
        # фиксированный argv, собранный строкой выше -- ни shell, ни ввода юзера
        return subprocess.run(  # nosec B603
            cmd, capture_output=True, creationflags=NO_WINDOW
        ).returncode

    run = runner or _default_runner
    run(schtasks_stop_cmd())
    run(taskkill_agent_cmd())
    wait = wait_unlocked or (
        lambda: _wait_files_unlocked(
            [Path(DEST) / AGENT_EXE], timeout_sec=_AGENT_STOP_TIMEOUT_SEC
        )
    )
    if not wait():
        alert("Не удалось остановить srp-agent.exe (файл всё ещё занят).")
        return 1
    return 0
```

4d. Заменить `request_exit` (строки 211-214):

```python
    def request_exit(self) -> None:
        proc = subprocess.run(self._child("--ask-password"), creationflags=NO_WINDOW)  # nosec B603
        if proc.returncode != 0:
            return
        # Пароль верен -> выгрузить и SYSTEM-агента. Трей бежит под обычным
        # пользователем, поэтому остановка идёт в UAC-элевированном ребёнке;
        # отказ от UAC закрывает иконку, но честно говорит, что агент жив.
        if not _launch_stop_agent_elevated():
            _alert(
                "Значок закрыт, но srp-agent.exe продолжает работать: "
                "остановка требует прав администратора."
            )
        self.icon.post_quit()
```

4e. В `_parse_args` добавить флаг (после `--ask-password`):

```python
    p.add_argument(
        "--stop-agent",
        action="store_true",
        dest="stop_agent",
        help="остановить SYSTEM-агента (запускается elevated из «Выход»)",
    )
```

4f. В `main()` добавить диспетч (после ветки `if args.ask_password:`):

```python
    if args.stop_agent:
        return run_stop_agent()
```

- [ ] **Step 5: тесты зелёные**

Run: `python -m pytest tests/test_tray_exit.py tests/test_tray_panel_logic.py tests/test_tray_state.py -q`
Expected: PASS.

- [ ] **Step 6: CHANGELOG** — `### Fixed`:

```markdown
- «Выход» в трее теперь останавливает и SYSTEM-процесс `srp-agent.exe` (после пароля — штатный UAC-запрос прав; при отказе честно предупреждает, что агент продолжает работать). До перезагрузки агент выгружен из памяти; автозапуск при загрузке сохраняется.
```

- [ ] **Step 7: полный гейт** — все зелёные.
- [ ] **Step 8: security-review** — subagent `security-reviewer` (Opus): элевация, fixed argv, отсутствие user-writable канала управления SYSTEM-процессом (проверить дизайн-решение 2 выше). Править CRITICAL/HIGH до merge.
- [ ] **Step 9: ручная проверка на живой машине (если есть установка):** «Выход» → пароль → UAC → `srp-agent.exe` исчезает из диспетчера; отказ UAC → MessageBox-предупреждение, иконка закрыта, агент жив. (Память проекта: живая верификация ловит то, что юниты не видят.)
- [ ] **Step 10: коммит + merge + push**

```bash
git add client/tray/__main__.py tests/test_tray_exit.py CHANGELOG.md
git commit -m "feat(tray): «Выход» останавливает SYSTEM-агента через UAC-элевированный --stop-agent"
git checkout main && git merge --no-ff feat/tray-exit-stops-agent && git push origin main
```

---

### Task 3: Когерентная иерархия рисков на карточке устройства (cctodo W4.3-3)

**Files:**
- Modify: `server/web/templates/device.html` (регион 155-230)
- Test: `tests/test_device_hero.py` (добавить 1 тест)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: существующий партиал `_device_hero.html` (содержит свой заголовок «Здоровье — координаты (D · R · O)» и `id="device-hero"`), контекст `s` (score100-оси), `health_color`/`risk_color`.
- Produces: порядок секций на странице: hero (вердикт D/R/O) → «Оси score100 — детализация» (бывшие 4 карточки) → «Покрытие источников» → «Прогноз». Никакой логики/весов не менять — только порядок и подпись.

Контекст: сейчас страница открывается legacy-скорами (П/Н/И/Риск), а вердикт D/R/O (hero) зарыт ниже — две конкурирующие иерархии (аудит C26/C40). Числа не убираем (оси остаются рабочей детализацией и запинены тестами) — меняем только первенство. Математику `overall=max(prob)` в bayesian.py НЕ трогать: она внутренний приоритизатор классов, оператору уже показывается бэндами.

- [ ] **Step 1: ветка** `git checkout -b feat/device-risk-hierarchy`
- [ ] **Step 2: падающий тест** — в `tests/test_device_hero.py` добавить:

```python
def test_device_page_hero_precedes_score100_axes(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    assert devices
    html = seeded_client.get(f"/device/{devices[0]['device_id']}").text
    hero = html.find('id="device-hero"')
    axes = html.find("Оси score100")
    assert hero != -1 and axes != -1, "нет hero или подписи осей"
    assert hero < axes, "вердикт D/R/O должен идти раньше score100-детализации"
```

- [ ] **Step 3: убедиться, что падает** — `python -m pytest tests/test_device_hero.py -q` → FAIL («Оси score100» нет в HTML).
- [ ] **Step 4: правка `device.html`** — два точечных edit:

4a. Удалить include hero из старого места (строки ~228-229):

```jinja
{# ── Health hero (Ф7 T7.2) — three-coordinate summary above "Прогноз" ───── #}
{% include "_device_hero.html" %}
```

→ удалить целиком (пустая замена).

4b. Перед блоком Day-1 карточек (якорь — строка `{# ── Day-1 score cards ──...`) вставить:

```jinja
{# ── Главный вердикт: координаты здоровья D·R·O (Ф7 hero) ──────────────── #}
{% include "_device_hero.html" %}

{# ── Оси score100 — детализация (вторичный уровень после координат) ────── #}
<div class="section-label" style="margin-top:24px">Оси score100 — детализация</div>
{# ── Day-1 score cards ─────────────────────────────────────────────────── #}
```

(строка `{% set c1 = health_color(s.performance) %}` и всё ниже — без изменений; «Покрытие источников» остаётся после карточек.)

- [ ] **Step 5: тесты зелёные** — `python -m pytest tests/test_device_hero.py tests/test_dashboard_api.py tests/test_health_web.py -q` → PASS.
- [ ] **Step 6: CHANGELOG** — `### Changed`:

```markdown
- Карточка устройства открывается вердиктом здоровья (координаты D·R·O, состояние, горизонт); четыре оси score100 (П/Н/И/Риск) демотированы в блок «Оси score100 — детализация» — одна иерархия риска для оператора вместо двух конкурирующих.
```

- [ ] **Step 7: полный гейт** → **Step 8: code-review (subagent code-reviewer)** → **Step 9: merge + push**

```bash
git add server/web/templates/device.html tests/test_device_hero.py CHANGELOG.md
git commit -m "feat(dashboard): карточка устройства ведёт вердиктом D/R/O, score100-оси — детализация"
git checkout main && git merge --no-ff feat/device-risk-hierarchy && git push origin main
```

---

### Task 4: W4.0 — развязать ingest и scoring (коалесцирующий async-rescore)

**Files:**
- Create: `server/rescore_queue.py`
- Modify: `server/pipeline.py` (строки 457-463 + holder), `server/config.py` (поле рядом с `stale_after_sec`, ~строка 30), `server/main.py` (lifespan: рядом со стартом maintenance/netdisco фоновых циклов), `server/config.json` (shipped-конфиг)
- Modify: `tests/conftest.py` (autouse-сброс)
- Create: `tests/test_rescore_queue.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `pipeline.recompute_scores(device_id) -> Optional[dict]` (не меняется).
- Produces: `RescoreQueue(recompute: Callable[[str], object])` c методами `start()`, `submit(device_id: str)`, `drain(timeout_sec: float = 10.0) -> bool`, `stop()`; `pipeline.set_rescore_queue(q: Optional[RescoreQueue])`; конфиг `async_rescore: bool = False` (в коде) / `true` (в `server/config.json`).

**Дизайн-решения:**
- Store остаётся синхронным (durability не трогаем); async — только recompute.
- Дефолт в КОДЕ = sync (все существующие тесты и их ожидание «скоры в ответе ingest» не меняются); shipped `server/config.json` включает async (no-dormant). Ответ ingest в async-режиме: `scores=null, scores_updated=false` — агент поле не читает (проверено: `client/` не использует ответ), дашборд поллит `/api/v1/devices`.
- Коалесценция по device_id (set, не очередь) — шторм конвертов одного устройства = один пересчёт.
- Ошибка recompute одного устройства НЕ убивает воркер (память: per-item try/catch).

- [ ] **Step 1: ветка** `git checkout -b feat/async-rescore-queue`
- [ ] **Step 2: падающие тесты** — создать `tests/test_rescore_queue.py`:

```python
"""W4.0 RescoreQueue: коалесценция, изоляция ошибок, drain, hook в pipeline."""

from __future__ import annotations

import threading
import time

from server.rescore_queue import RescoreQueue


def test_coalesces_burst_for_one_device() -> None:
    release = threading.Event()
    calls: list[str] = []

    def recompute(did: str) -> None:
        calls.append(did)
        release.wait(2.0)  # держим воркер, чтобы сабмиты легли в pending

    q = RescoreQueue(recompute)
    q.start()
    q.submit("d1")
    for _ in range(200):  # дождаться, пока воркер займёт d1
        if calls:
            break
        time.sleep(0.01)
    q.submit("d1")
    q.submit("d1")
    q.submit("d1")
    release.set()
    assert q.drain(5.0)
    q.stop()
    # первый прогон + ОДИН доп. прогон за весь шторм из трёх сабмитов
    assert calls == ["d1", "d1"]


def test_recompute_error_does_not_kill_worker() -> None:
    calls: list[str] = []

    def recompute(did: str) -> None:
        calls.append(did)
        if did == "boom":
            raise RuntimeError("scoring exploded")

    q = RescoreQueue(recompute)
    q.start()
    q.submit("boom")
    assert q.drain(5.0)
    q.submit("ok")
    assert q.drain(5.0)
    q.stop()
    assert "ok" in calls


def test_drain_true_on_idle_queue() -> None:
    q = RescoreQueue(lambda _did: None)
    q.start()
    assert q.drain(1.0)
    q.stop()


def test_pipeline_enqueues_instead_of_inline_when_queue_set(seeded_client) -> None:
    from server import pipeline

    class FakeQueue:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        def submit(self, device_id: str) -> None:
            self.submitted.append(device_id)

    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    fq = FakeQueue()
    pipeline.set_rescore_queue(fq)  # conftest-autouse вернёт None после теста
    body = {
        "device_id": did,
        "agent_version": "0.2.0",
        "msg_type": "heartbeat",
        "ts": "2026-07-12T00:00:00+00:00",
        "payload": {"cpu_load_pct": 5.0, "mem_used_pct": 40.0},
        "source_health": {},
    }
    r = seeded_client.post("/api/v1/ingest", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["scores"] is None and data["scores_updated"] is False
    assert fq.submitted == [did]
```

(если минимальный heartbeat-payload отвергается схемой — взять поля из `healthy("heartbeat")` в `tests/conftest.py:196`, суть теста не меняется.)

- [ ] **Step 3: убедиться, что падают** — `python -m pytest tests/test_rescore_queue.py -q` → FAIL (`ModuleNotFoundError: server.rescore_queue`).
- [ ] **Step 4: создать `server/rescore_queue.py`:**

```python
"""W4.0: развязка ingest и scoring -- коалесцирующий фоновый rescore-воркер.

Ingest сохраняет телеметрию синхронно (durability не меняется) и лишь ставит
устройство в очередь; один daemon-поток разгребает её, так что шторм конвертов
одного устройства стоит один пересчёт, а медленный recompute больше не сидит
внутри HTTP-запроса. Sync-режим (async_rescore=false, дефолт кода) сохраняет
сегодняшнее поведение: пересчёт инлайн, свежие скоры в ответе ingest. Shipped
server/config.json включает async (правило no-dormant): ответ тогда несёт
scores=null -- все потребители это уже переживают (агент поле не читает,
дашборд поллит /api/v1/devices).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_JOIN_TIMEOUT_SEC = 5.0


class RescoreQueue:
    """Коалесцирующий per-device rescore-воркер (in-process, один поток).

    ponytail: одного потока и set-коалесценции хватает до сотен устройств;
    выделенный воркер-процесс -- когда drain на живом флоте станет заметен.
    """

    def __init__(self, recompute: Callable[[str], object]) -> None:
        self._recompute = recompute
        self._pending: set[str] = set()
        self._cond = threading.Condition()
        self._busy = 0
        self._stopping = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="rescore-queue", daemon=True)
        self._thread.start()

    def submit(self, device_id: str) -> None:
        with self._cond:
            if self._stopping:
                return
            self._pending.add(device_id)
            self._cond.notify()

    def drain(self, timeout_sec: float = 10.0) -> bool:
        """Ждать, пока очередь пуста и воркер простаивает (тесты/шатдаун)."""
        deadline = time.monotonic() + timeout_sec
        with self._cond:
            while self._pending or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(_JOIN_TIMEOUT_SEC)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stopping:
                    self._cond.wait()
                if self._stopping and not self._pending:
                    return
                device_id = self._pending.pop()
                self._busy += 1
            try:
                self._recompute(device_id)
            except Exception:  # одно битое устройство не должно убить воркер
                log.exception("background rescore failed for %s", device_id)
            finally:
                with self._cond:
                    self._busy -= 1
                    self._cond.notify_all()
```

- [ ] **Step 5: hook в `server/pipeline.py`.**

5a. Рядом с другими модульными констанстами добавить holder + сеттер (импорт `RescoreQueue` — только под `TYPE_CHECKING`, циклов нет):

```python
if TYPE_CHECKING:
    from server.rescore_queue import RescoreQueue

_RESCORE_QUEUE: Optional["RescoreQueue"] = None


def set_rescore_queue(queue: Optional["RescoreQueue"]) -> None:
    """W4.0: включить/выключить фоновый rescore (None = синхронно, как раньше)."""
    global _RESCORE_QUEUE
    _RESCORE_QUEUE = queue
```

5b. Заменить строки 462-463 (`no_rescore = ...; scores = None if ... else recompute_scores(did)`):

```python
    no_rescore = {"events", "print_jobs", "liveness", "update_status"}
    if env.msg_type in no_rescore:
        scores = None
    elif _RESCORE_QUEUE is not None:
        # W4.0: писать быстро, пересчитывать асинхронно -- recompute уходит из
        # HTTP-запроса; свежие скоры появятся в /api/v1/devices после воркера.
        _RESCORE_QUEUE.submit(did)
        scores = None
    else:
        scores = recompute_scores(did)
```

- [ ] **Step 6: конфиг + wiring.**

6a. `server/config.py` — рядом с `stale_after_sec` добавить поле (тем же стилем, что соседние):

```python
    # W4.0: пересчитывать скоры в фоновом воркере, а не в HTTP-запросе ingest.
    async_rescore: bool = False
```

6b. `server/main.py` — в lifespan-старте приложения (рядом с существующим запуском фоновых циклов maintenance/netdisco) добавить, а в shutdown-части — остановку:

```python
    if cfg.async_rescore:
        from server.pipeline import recompute_scores, set_rescore_queue
        from server.rescore_queue import RescoreQueue

        rescore_queue = RescoreQueue(recompute_scores)
        rescore_queue.start()
        set_rescore_queue(rescore_queue)
        app.state.rescore_queue = rescore_queue
```

```python
    # shutdown: дать воркеру дожевать очередь и погаснуть
    queue = getattr(app.state, "rescore_queue", None)
    if queue is not None:
        queue.drain(5.0)
        queue.stop()
        set_rescore_queue(None)
```

6c. `server/config.json` (shipped) — добавить `"async_rescore": true` (no-dormant: фича включена в деплое, тесты используют дефолт кода = sync).

6d. `tests/conftest.py` — в существующий autouse `_reset_ingest_guards` добавить сброс (import вместе с использованием):

```python
    from server.pipeline import set_rescore_queue

    set_rescore_queue(None)
```

- [ ] **Step 7: тесты зелёные** — `python -m pytest tests/test_rescore_queue.py tests/test_ingest.py tests/test_trust_pipeline.py -q` → PASS (sync-путь нетронут).
- [ ] **Step 8: CHANGELOG** — `### Changed`:

```markdown
- Пересчёт скоров вынесен из HTTP-запроса ingest в фоновый коалесцирующий воркер (`async_rescore`, включено в поставляемом конфиге): приём телеметрии больше не ждёт scoring, шторм конвертов одного устройства = один пересчёт.
```

- [ ] **Step 9: полный гейт** → **Step 10: security-review (Opus; ingest-поверхность)** → **Step 11: merge + push**

```bash
git add server/rescore_queue.py server/pipeline.py server/config.py server/main.py server/config.json tests/test_rescore_queue.py tests/conftest.py CHANGELOG.md
git commit -m "feat(pipeline): W4.0 async coalescing rescore queue (sync по умолчанию в коде, ON в shipped-конфиге)"
git checkout main && git merge --no-ff feat/async-rescore-queue && git push origin main
```

---

### Task 5: клиентский cap размера payload (cctodo P1, последний хвост transport-hardening)

**Files:**
- Modify: `client/transport.py` (`_attempt`, :148-178)
- Test: `tests/test_transport_hardening.py` (добавить)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `Transport._attempt(envelope) -> "ok" | "drop" | "retry"` (существующий контракт).
- Produces: константа `client.transport._MAX_PAYLOAD_BYTES = 500_000`; негабарит = `"drop"` (обработан, НЕ буферится, не ретраится) — и на живой отправке, и на реплее из буфера (общая точка).

- [ ] **Step 1: ветка** `git checkout -b feat/agent-payload-cap`
- [ ] **Step 2: падающий тест** — в `tests/test_transport_hardening.py`:

```python
def test_oversized_payload_dropped_not_buffered(tmp_path) -> None:
    from types import SimpleNamespace

    from client import transport as tr

    cfg = SimpleNamespace(
        server_url="http://127.0.0.1:9",  # discard-порт: сеть не должна понадобиться
        device_id="d1",
        hostname="h",
        site_code="",
        site_name="",
        org_code="",
        dept_code="",
        comment="",
        ingest_token="",
        http_timeout_sec=1.0,
        resolved_buffer_path=lambda: tmp_path / "buffer.jsonl",
    )
    t = tr.Transport(cfg)

    big = {"blob": "x" * (tr._MAX_PAYLOAD_BYTES + 1)}
    assert t.send("historical", big) is True  # «обработан» = отброшен без ретраев
    assert t.buffer_depth() == 0  # и НЕ лёг в оффлайн-буфер
    assert "cap" in t.last_error
```

- [ ] **Step 3: убедиться, что падает** — `python -m pytest tests/test_transport_hardening.py -q -k oversized` → FAIL (`AttributeError: _MAX_PAYLOAD_BYTES`; без константы тест бы ушёл в сеть/буфер).
- [ ] **Step 4: реализация** в `client/transport.py`.

4a. После `_RETRY_JITTER_SEC` добавить:

```python
# Чуть ниже серверного лимита 512 KiB (server/main.py body-cap): негабарит
# режем ещё на агенте -- не жечь сеть ради гарантированного 413.
_MAX_PAYLOAD_BYTES = 500_000
```

4b. В `_attempt` сразу после `body = json.dumps(...)` вставить:

```python
        if len(body) > _MAX_PAYLOAD_BYTES:
            self.last_error = f"payload {len(body)} bytes > cap"
            log.warning(
                "oversized %s payload (%d bytes) -- dropping, not buffering",
                envelope.get("msg_type"),
                len(body),
            )
            return "drop"
```

(точка общая для живой отправки и `flush_buffer` — негабаритная строка из старого буфера тоже будет отброшена, очередь не клинит.)

- [ ] **Step 5: тесты зелёные** — `python -m pytest tests/test_transport.py tests/test_transport_hardening.py -q` → PASS.
- [ ] **Step 6: CHANGELOG** — `### Changed`:

```markdown
- Агент отбрасывает негабаритные конверты (>500 КБ) ещё до отправки — раньше они жгли сеть ради гарантированного серверного 413 и могли клинить оффлайн-буфер.
```

- [ ] **Step 7: полный гейт** → **Step 8: security-review (Opus; код агента)** → **Step 9: merge + push**

```bash
git add client/transport.py tests/test_transport_hardening.py CHANGELOG.md
git commit -m "feat(agent): client-side cap размера конверта (drop >500KB до отправки)"
git checkout main && git merge --no-ff feat/agent-payload-cap && git push origin main
```

---

### Task 6: D9 — compressed raw windows перед prune (cctodo W0.1-4)

**Files:**
- Modify: `server/db.py` (`prune_aged` :1152-1182, схема таблиц, `_DEVICE_TABLES`, новые `_archive_aged_rows`/`get_raw_archive`)
- Create: `tests/test_raw_archive.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `prune_aged(*, heartbeat_raw_days, events_raw_days, rollup_days)` (вызывается из maintenance-свипа `server/main.py:77-95`), `_lock`, `_connect`, `_now_iso`.
- Produces: таблица `raw_archive(device_id, day, kind, rows_n, blob, created_at, PK(device_id, day, kind))`; `get_raw_archive(device_id: str, kind: str, days: int) -> list[dict]` (распакованные строки для будущего репроцессинга); архивация встроена в `prune_aged` ДО DELETE, в той же транзакции.

**Дизайн-решения:**
- Архивируем именно то, что prune собирается удалить (та же граница `received_at < datetime('now','-N days')`, та же транзакция) — сырьё никогда не исчезает неархивированным, и повторный прогон не дублирует (удалённое не перечитать).
- День может состариться частично (граница — timestamp, не полночь) → при конфликте (device, day, kind) существующий blob распаковывается, строки дописываются, blob пересжимается. Никакой конкатенации zlib-потоков.
- Ретенция архива = `rollup_days` (та же, что у свёрток) — добавить DELETE в prune.
- `raw_archive` — device-scoped → ОБЯЗАТЕЛЬНО в `_DEVICE_TABLES` (правило не-регрессии 3).
- Имена таблиц в f-string SQL — только из закрытого литерала `_ARCHIVE_TABLES` c явной проверкой (инвариант «весь SQL параметризован» для значений; имя — из константы + `# nosec B608` с причиной).

- [ ] **Step 1: ветка** `git checkout -b feat/raw-archive-d9`
- [ ] **Step 2: падающие тесты** — создать `tests/test_raw_archive.py`:

```python
"""D9: prune_aged архивирует сырые окна (zlib) до удаления; архив читается."""

from __future__ import annotations

from server import db


def _seed_old_heartbeat(device_id: str, days_ago: int) -> None:
    with db._connect() as conn:  # тестовый сид: состарить received_at напрямую
        conn.execute(
            "UPDATE heartbeats SET received_at = datetime('now', ?) WHERE device_id = ?",
            (f"-{days_ago} days", device_id),
        )


def test_prune_archives_before_delete(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    _seed_old_heartbeat(did, days_ago=120)

    deleted = db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)

    assert deleted.get("heartbeats", 0) >= 1
    archived = db.get_raw_archive(did, "heartbeats", days=730)
    assert archived, "удалённые сырые строки обязаны лежать в raw_archive"
    assert archived[0]["device_id"] == did  # строки распаковываются в исходные dict


def test_prune_second_run_is_idempotent(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    _seed_old_heartbeat(did, days_ago=120)
    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)
    first = db.get_raw_archive(did, "heartbeats", days=730)

    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)

    assert db.get_raw_archive(did, "heartbeats", days=730) == first


def test_raw_archive_rows_die_with_device(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    _seed_old_heartbeat(did, days_ago=120)
    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)
    assert db.get_raw_archive(did, "heartbeats", days=730)

    db.delete_device(did)

    assert db.get_raw_archive(did, "heartbeats", days=730) == []
```

- [ ] **Step 3: убедиться, что падают** — `python -m pytest tests/test_raw_archive.py -q` → FAIL (`no such table: raw_archive` / `AttributeError: get_raw_archive`).
- [ ] **Step 4: реализация в `server/db.py`.**

4a. В блок создания схемы (рядом с rollup-таблицами ssd3 Ф5) добавить:

```python
        conn.execute(
            """CREATE TABLE IF NOT EXISTS raw_archive (
                 device_id TEXT NOT NULL,
                 day TEXT NOT NULL,
                 kind TEXT NOT NULL,
                 rows_n INTEGER NOT NULL,
                 blob BLOB NOT NULL,
                 created_at TEXT NOT NULL,
                 PRIMARY KEY (device_id, day, kind)
               )"""
        )
```

4b. Добавить `"raw_archive"` в `_DEVICE_TABLES`.

4c. `import zlib` в шапку (вместе с первым использованием — хук ruff).

4d. Перед `prune_aged` добавить:

```python
_ARCHIVE_TABLES = ("heartbeats", "events")  # закрытый литерал -- не внешний ввод


def _archive_aged_rows(conn: sqlite3.Connection, table: str, days: int) -> int:
    """D9: zlib-архив строк *table* старше порога -- ДО их DELETE в prune_aged.

    Группировка по (device_id, день UTC); день, состарившийся частично
    (граница -- timestamp), дописывается: существующий blob распаковывается,
    строки добавляются, blob пересжимается -- никакой конкатенации zlib-потоков.
    Той же транзакцией prune удаляет исходники, поэтому повторный прогон не
    видит уже заархивированных строк -- дублей нет by construction.
    """
    if table not in _ARCHIVE_TABLES:
        raise ValueError(f"not archivable: {table!r}")
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE received_at < datetime('now', ?)",  # nosec B608 -- имя из закрытого литерала выше
        (f"-{days} days",),
    ).fetchall()
    if not rows:
        return 0
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        d = dict(r)
        day = str(d.get("received_at") or "")[:10]
        grouped.setdefault((str(d.get("device_id")), day), []).append(d)
    now = _now_iso()
    for (device_id, day), items in grouped.items():
        lines = [json.dumps(i, ensure_ascii=False, sort_keys=True) for i in items]
        existing = conn.execute(
            "SELECT blob FROM raw_archive WHERE device_id=? AND day=? AND kind=?",
            (device_id, day, table),
        ).fetchone()
        if existing is not None:
            lines = zlib.decompress(existing["blob"]).decode("utf-8").splitlines() + lines
        conn.execute(
            "INSERT OR REPLACE INTO raw_archive "
            "(device_id, day, kind, rows_n, blob, created_at) VALUES (?,?,?,?,?,?)",
            (device_id, day, table, len(lines), zlib.compress("\n".join(lines).encode("utf-8")), now),
        )
    return len(rows)


def get_raw_archive(device_id: str, kind: str, days: int) -> list[dict[str, Any]]:
    """Распакованные сырые строки из архива (репроцессинг/feature-mining, D9)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT blob FROM raw_archive
               WHERE device_id=? AND kind=? AND day >= date('now', ?)
               ORDER BY day""",
            (device_id, kind, f"-{days} days"),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        for line in zlib.decompress(r["blob"]).decode("utf-8").splitlines():
            out.append(json.loads(line))
    return out
```

4e. В `prune_aged` вставить архивацию и ретенцию архива (внутри существующего `with _lock, _connect() as conn:`):

```python
        if heartbeat_raw_days > 0:
            deleted["raw_archived_heartbeats"] = _archive_aged_rows(
                conn, "heartbeats", heartbeat_raw_days
            )
            cur = conn.execute(  # существующий DELETE heartbeats -- без изменений
```

```python
        if events_raw_days > 0:
            deleted["raw_archived_events"] = _archive_aged_rows(conn, "events", events_raw_days)
            cur = conn.execute(  # существующий DELETE events -- без изменений
```

```python
        if rollup_days > 0:
            # ... существующие DELETE по свёрткам ...
            cur = conn.execute(
                "DELETE FROM raw_archive WHERE day < ?", (cutoff,)
            )
            deleted["raw_archive"] = cur.rowcount
```

- [ ] **Step 5: тесты зелёные** — `python -m pytest tests/test_raw_archive.py tests/test_maintenance.py tests/test_device_cleanup.py -q` → PASS.
- [ ] **Step 6: CHANGELOG** — `### Added`:

```markdown
- Сырые окна heartbeats/events перед возрастным удалением сжимаются (zlib) в архив `raw_archive` с ретенцией как у свёрток — «нашли новый precursor» больше не означает «а сырья уже нет» (D9).
```

- [ ] **Step 7: полный гейт** → **Step 8: code-review** → **Step 9: merge + push**

```bash
git add server/db.py tests/test_raw_archive.py CHANGELOG.md
git commit -m "feat(db): D9 raw_archive -- zlib-архив сырых окон перед prune, ретенция rollup_days"
git checkout main && git merge --no-ff feat/raw-archive-d9 && git push origin main
```

---

### Task 7: Replay-тест — детерминизм оффлайн-пересчёта (cctodo W0.1-5)

**Files:**
- Create: `tests/test_replay_determinism.py`

**Interfaces:**
- Consumes: `pipeline.recompute_scores(device_id)`, фикстура `seeded_client` (`tests/conftest.py:256`), `db.get_score_series(device_id, limit)`.
- Produces: только тест (код не меняется — это проверка уже существующей возможности «переиграть историю через scoring оффлайн»).

Почему сравниваются выбранные поля, а не весь dict: risk-blob содержит производные от «сейчас» величины (ETA в днях с плавающей долей, staleness-возрасты) — они детерминированы при фиксированном времени, но между двумя вызовами проходит ~секунда. Оси П/Н/И/Риск зависят только от сохранённой истории (свежепосеянной, staleness не флипается) — их равенство и есть заявленная replayability. НЕ «чинить» тест через сравнение полного dict.

- [ ] **Step 1: ветка** `git checkout -b test/replay-determinism`
- [ ] **Step 2: тест** — создать `tests/test_replay_determinism.py`:

```python
"""W0.1 replayability: сохранённая история, пересчитанная оффлайн, детерминирована.

recompute_scores читает ТОЛЬКО из БД -- два вызова подряд на неизменном
хранилище обязаны дать одинаковые оси. Заодно пинится append-only: каждый
пересчёт добавляет строку в scores, ничего не перезаписывая.
"""

from __future__ import annotations

from server import db
from server.pipeline import recompute_scores

_STABLE_AXES = ("performance", "reliability", "wear", "risk_exposure")


def test_offline_recompute_is_deterministic_and_appends(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    assert devices
    did = devices[0]["device_id"]
    rows_before = len(db.get_score_series(did, limit=1000))

    first = recompute_scores(did)
    second = recompute_scores(did)

    assert first is not None and second is not None
    assert {k: first.get(k) for k in _STABLE_AXES} == {
        k: second.get(k) for k in _STABLE_AXES
    }, "оффлайн-пересчёт по одной и той же истории обязан быть детерминирован"
    rows_after = len(db.get_score_series(did, limit=1000))
    assert rows_after == rows_before + 2, "каждый replay добавляет строку (append-only)"
```

(если аксессор истории скоров называется иначе — найти в `server/db.py` функцию, которую использует `server/web/dashboard.py:429` (`db.get_score_series`), и использовать её; ожидание теста не менять.)

- [ ] **Step 3: прогнать** — `python -m pytest tests/test_replay_determinism.py -q` → ожидается PASS сразу (это фиксация уже существующего свойства). Если FAIL по равенству осей — СТОП: найден реальный недетерминизм скоринга, завести отдельную задачу-исследование, тест не ослаблять.
- [ ] **Step 4: полный гейт** → **Step 5: code-review** → **Step 6: merge + push**

```bash
git add tests/test_replay_determinism.py
git commit -m "test(scoring): W0.1 replay -- оффлайн-пересчёт детерминирован, история append-only"
git checkout main && git merge --no-ff test/replay-determinism && git push origin main
```

---

### Task 8: /metrics — ожидание write-лока + отказы ingest (cctodo §6, хвост C32)

**Files:**
- Modify: `server/db.py` (строка 38 `_lock = threading.Lock()` → `_TimedLock`; `get_pipeline_metrics` :3880+)
- Modify: `server/ingest_guards.py` (счётчики), `server/api.py` (:37-50, инкременты), `server/main.py` (:31-48, инкремент body-cap)
- Modify: `server/web/templates/pipeline.html` (два metric-card)
- Modify: `tests/conftest.py` (`_reset_ingest_guards` — сброс счётчиков)
- Test: `tests/test_pipeline_health.py` (добавить)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: все 44 сайта `with _lock` в db.py (проверено: прямых `.acquire()` нет — только контекст-менеджер и один комментарий :1320).
- Produces: `db._lock.stats() -> {"acquisitions","wait_avg_ms","wait_max_ms"}`; `ingest_guards.REJECT_COUNTS: dict[str,int]` (`auth|rate_limit|duplicate|invalid|too_large`) + `ingest_guards.count_reject(reason)`; ключи `"lock"` и `"ingest_rejects"` в `get_pipeline_metrics()` (автоматом попадают и в `/api/v1/metrics`, и на `/pipeline`).

- [ ] **Step 1: ветка** `git checkout -b feat/pipeline-lock-reject-metrics`
- [ ] **Step 2: падающие тесты** — в `tests/test_pipeline_health.py` добавить (конверт собрать так же, как соседние тесты этого файла / `tests/test_ingest.py` — хелперы `healthy()`/`envelope()` в `tests/conftest.py:196-216`):

```python
def test_metrics_expose_lock_and_reject_counters(client) -> None:
    m = client.get("/api/v1/metrics").json()
    assert {"acquisitions", "wait_avg_ms", "wait_max_ms"} <= set(m["lock"])
    assert {"auth", "rate_limit", "duplicate", "invalid", "too_large"} <= set(m["ingest_rejects"])


def test_rate_limit_reject_is_counted(client, monkeypatch) -> None:
    from server import api as api_mod
    from server import ingest_guards

    monkeypatch.setattr(api_mod, "check_rate_limit", lambda _did: False)
    r = client.post(
        "/api/v1/ingest",
        json=envelope("dev-rej", "heartbeat", healthy("heartbeat")),
    )
    assert r.status_code == 429
    assert ingest_guards.REJECT_COUNTS["rate_limit"] == 1
```

- [ ] **Step 3: убедиться, что падают** — `python -m pytest tests/test_pipeline_health.py -q` → FAIL (`KeyError: 'lock'`).
- [ ] **Step 4: `_TimedLock` в `server/db.py`** — заменить строку 38 `_lock = threading.Lock()` на:

```python
class _TimedLock:
    """threading.Lock со счётом ожидания для /metrics (§6 «ожидание write-лока»).

    Счётчики мутируются только ПОД замком -- доп. синхронизация не нужна;
    stats() терпит рваное чтение (это мониторинг, не бухгалтерия).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquisitions = 0
        self.wait_total_sec = 0.0
        self.wait_max_sec = 0.0

    def __enter__(self) -> "_TimedLock":
        t0 = time.perf_counter()
        self._lock.acquire()
        waited = time.perf_counter() - t0
        self.acquisitions += 1
        self.wait_total_sec += waited
        if waited > self.wait_max_sec:
            self.wait_max_sec = waited
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()

    def stats(self) -> dict[str, float]:
        n = self.acquisitions
        return {
            "acquisitions": float(n),
            "wait_avg_ms": (self.wait_total_sec / n * 1000.0) if n else 0.0,
            "wait_max_ms": self.wait_max_sec * 1000.0,
        }


_lock = _TimedLock()
```

(`import time` в шапку db.py, если его там ещё нет — вместе с использованием.)

- [ ] **Step 5: счётчики отказов** — в `server/ingest_guards.py`:

```python
# §6 «отказы буфера» серверной стороной: отклонённые конверты по причинам.
# In-process счётчики (один uvicorn-воркер); тесты сбрасывают через reset-хук.
REJECT_COUNTS: dict[str, int] = {
    "auth": 0,
    "rate_limit": 0,
    "duplicate": 0,
    "invalid": 0,
    "too_large": 0,
}


def count_reject(reason: str) -> None:
    REJECT_COUNTS[reason] = REJECT_COUNTS.get(reason, 0) + 1
```

В существующую reset-функцию, которую зовёт `tests/conftest.py::_reset_ingest_guards`, добавить обнуление: `for k in REJECT_COUNTS: REJECT_COUNTS[k] = 0` (если conftest сбрасывает вручную — добавить туда).

Инкременты (по одному в каждую ветку отказа):
- `server/api.py` ingest: 401 → `count_reject("auth")`; 429 → `count_reject("rate_limit")`; duplicate-ветка → `count_reject("duplicate")`; `except ValueError` (422) → `count_reject("invalid")`.
- `server/main.py` body-cap middleware (413) → `count_reject("too_large")` (импорт из `server.ingest_guards`).

- [ ] **Step 6: `get_pipeline_metrics`** — в возвращаемый dict (db.py:3880+) добавить два ключа:

```python
        "lock": _lock.stats(),
        "ingest_rejects": _reject_counts_snapshot(),
```

где рядом с функцией:

```python
def _reject_counts_snapshot() -> dict[str, int]:
    from server.ingest_guards import REJECT_COUNTS  # локальный импорт: без циклов

    return dict(REJECT_COUNTS)
```

- [ ] **Step 7: `/pipeline` страница** — в `server/web/templates/pipeline.html`, внутрь metrics-grid блока «Хранилище» (после карточки «Обслуживание БД», перед закрывающим `</div>` грида) добавить:

```jinja
  <div class="metric-card {{ 'warn' if m.lock.wait_max_ms > 500 else 'na' }}">
    <div class="mc-label">Write-лок БД</div>
    <div class="mc-rows">
      <div class="mc-row"><span class="mc-key">захватов</span>
        <span class="mc-val accent">{{ "%.0f"|format(m.lock.acquisitions) }}</span></div>
      <div class="mc-row"><span class="mc-key">ожидание, среднее</span>
        <span class="mc-val na">{{ "%.2f"|format(m.lock.wait_avg_ms) }} мс</span></div>
      <div class="mc-row"><span class="mc-key">ожидание, максимум</span>
        <span class="mc-val {{ 'warn' if m.lock.wait_max_ms > 500 else 'good' }}">{{ "%.1f"|format(m.lock.wait_max_ms) }} мс</span></div>
    </div>
  </div>

  {% set rej = m.ingest_rejects %}
  {% set rej_total = rej.values()|sum %}
  <div class="metric-card {{ 'warn' if rej_total else 'good' }}">
    <div class="mc-label">Отказы ingest</div>
    <div class="mc-rows">
      {% for k, v in rej.items() %}
      <div class="mc-row"><span class="mc-key">{{ k }}</span>
        <span class="mc-val {{ 'warn' if v else 'na' }}">{{ v }}</span></div>
      {% endfor %}
    </div>
  </div>
```

- [ ] **Step 8: тесты зелёные** — `python -m pytest tests/test_pipeline_health.py tests/test_ingest_auth.py tests/test_ingest.py -q` → PASS.
- [ ] **Step 9: CHANGELOG** — `### Added`:

```markdown
- Страница «Пайплайн» и `/api/v1/metrics`: ожидание write-лока БД (среднее/максимум) и счётчики отказов ingest (auth / rate-limit / duplicate / invalid / too-large) — закрывает последний хвост §6 observability.
```

- [ ] **Step 10: полный гейт** → **Step 11: code-review** → **Step 12: merge + push**

```bash
git add server/db.py server/ingest_guards.py server/api.py server/main.py server/web/templates/pipeline.html tests/test_pipeline_health.py tests/conftest.py CHANGELOG.md
git commit -m "feat(observability): метрики write-лока и отказов ingest на /pipeline и /metrics"
git checkout main && git merge --no-ff feat/pipeline-lock-reject-metrics && git push origin main
```

---

### Task 9: Закрыть cctodo.md — чекбоксы, решения, финальное ревью

**Files:**
- Modify: `cctodo.md` (чекбоксы + аннотации), `README.md` или `docs/agent-install.md` (абзац TLS), `CONTINUITY.md`

**Interfaces:**
- Consumes: результаты Task 1-8 (все смержены в main), карта статуса из этого плана.
- Produces: cctodo.md, где каждый пункт либо `[x]` с доказательством, либо помечен решением, либо явно «ГЕЙТ НЕ ПРОЙДЕН».

- [ ] **Step 1: ветка** `git checkout -b docs/cctodo-closeout`
- [ ] **Step 2: правка `cctodo.md`.** Для каждого пункта — флип чекбокса + короткая пометка курсивом в конце строки:

  - §2 W0.1 (5 пунктов), W0.2 (2), W0.3 (4), W0.4 (1), W0.5 (2) → все `[x]`; пометки: *(db.py append-only + миграция)*, *(rollup Ф5)*, *(raw_archive, план 2026-07-12 T6)*, *(test_replay_determinism, T7)*, *(received_at+drift pipeline.py)*, *(server/trust/ + sources.py)*, *(test_contract_compat)*, *(score100 _gate_axis + бэнды)*.
  - §3: ingest auth `[x]` *(b49a738)*; TLS `[x]` *(операционно: reverse-proxy, см. абзац в docs — Step 3)*; дефолт-конфиг `[x]` *(config.py:46)*; Windows Service `[x]` *(SUPERSEDED: schtasks-SYSTEM, task_template.xml)*; Signed config `[x]` *(РЕШЕНИЕ: отклонено — ключ жил бы под той же ACL, реальная защита icacls+HMAC-манифест обновлений)*; transport `[x]` *(jitter+idempotency ранее; cap — T5)*; input limits `[x]` *(ingest_guards+main.py)*; org/dept `[x]` *(org_directory.py)*.
  - §4 W4.0 `[x]` *(RescoreQueue, T4)*; W4.1 `[x]` *(trends.py Theil-Sen+ETA)*; W4.2 все `[x]` *(analytics/\*; когорта=model — задокументированное решение, сайтовый KP41 сделан)*; W4.3 `[x]`,`[x]`,`[x]` *(bayesian.py:210-253; иерархия: D/R/O первичны на карточке — T3, max(prob) остаётся внутренним приоритизатором)*.
  - §5 все `[x]` *(health_view/fleet.html/device.html — аудит 2026-07-12)*.
  - §6 observability `[x]` *(метрики+/pipeline; лок/отказы — T8; структурные JSON-логи — отложены решением: однобокс)*; тест-долг 4×`[x]` *(test_collectors_parsers / test_locale_fixtures / mypy client / test_scoring_boundaries)*.
  - §7 оставить `[ ]`, добавить над списком строку: `> **СТАТУС 2026-07-12: гейты не пройдены — не строить.** rulestats (ssd3 Ф8) — самоподтверждение правил, НЕ замена L1-меток.`
  - §9 дописать ответы: retention-формат → zlib-блоб на (device, day) в raw_archive; граница confidence → реализована в score100/_gate_axis + health.observability<40; когортирование → model-only (решение в fleet_anomaly.py); async-масштаб → in-process set-очереди хватает до сотен устройств, воркер-процесс — при видимом drain.
- [ ] **Step 3: абзац про TLS** — в `docs/agent-install.md` (раздел про сервер) добавить:

```markdown
## Продакшн: TLS

Сервер SRP слушает HTTP; в проде ставьте перед ним reverse-proxy с TLS
(caddy — две строки конфига, или nginx+certbot) и указывайте агентам
`https://…` в `server_url`. Собственный PKI не заводим сознательно
(cctodo §1, анти-цели): токен ingest + TLS прокси закрывают модель угроз
«внешний злоумышленник в сети».
```

- [ ] **Step 4: финальное whole-branch ревью** — subagent `code-reviewer` (Sonnet) на СОВОКУПНЫЙ дифф всех задач плана (`git diff <коммит-до-Task1>..HEAD`), с явной инструкцией: «проверь cross-task стыки: очередь+метрики+архив вместе; grep-ни симптомы багов, названных в ревью отдельных задач, по всему диффу» (память: task-scoped ревью слепо к межзадачным багам).
- [ ] **Step 5: гейт** (docs-правки тоже гоняем — ruff/mypy не затронуты, но полный прогон обязателен) → **Step 6: CONTINUITY.md** — тред CCTODO-COMPLETION пометить ✅ ЗАВЕРШЁННЫМ, перечислить 9 задач и коммиты.
- [ ] **Step 7: merge + push**

```bash
git add cctodo.md docs/agent-install.md CONTINUITY.md
git commit -m "docs(cctodo): закрытие roadmap -- чекбоксы с доказательствами, решения, гейты §7 зафиксированы"
git checkout main && git merge --no-ff docs/cctodo-closeout && git push origin main
```

---

## Порядок исполнения и независимость

```
Task 1 (uninstall)  ─┐  владельческие фиксы — первыми
Task 2 (tray exit)  ─┘  (независимы друг от друга)
Task 3 (иерархия UI)      — независима
Task 4 (async rescore)    — независима
Task 5 (payload cap)      — независима
Task 6 (raw archive)  → Task 7 (replay-тест)   — 7 логически после 6 (оба — W0.1)
Task 8 (метрики)          — независима
Task 9 (закрытие cctodo)  — СТРОГО последняя (ссылается на T1-T8)
```

Вне плана (осознанно НЕ делаем): §7 L1/L2/L3 (гейты), RBAC/PKI/multi-tenant, auto-remediation (анти-цели §1), структурные JSON-логи, подпись config.json, углубление per-domain weight в движки (потолок скоупа). Возможный следующий шаг по желанию владельца: колонка «Здоровье» (state D/R/O) на флоте — потребует расширения `db.get_devices()`, в этот план не включена.
