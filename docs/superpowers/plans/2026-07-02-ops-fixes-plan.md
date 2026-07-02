# План «ops-fixes»: offline ≤10 мин · имена устройств · единая идентичность · netmap-фиксы · печать/принтеры

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 7 веток-фиксов: (1) починить учёт печати (однобуквенный баг `\e:true` в инсталляторе + самолечение агентом + баннер), (2) offline-детект ≤10 мин через новый liveness-конверт, (3) glyph/medium-фиксы карты сети (принтер-фильтр + wireless-пунктир), (4) единый резолвер имени устройства + identity-карточка, (5) контрастный Plotly-hover, (6) полная невидимость агента (CREATE_NO_WINDOW везде), (7) IPP Get-Jobs как дополняющий серверный сборщик.

**Architecture:** Минимально-инвазивно: каждая правка садится на существующий паттерн (TASKS-loop агента, pipeline-dispatch, unified-ассемблер, PrinterConfig/scheduler-инъекции, structural-pin тесты). Ни одной новой зависимости, ни одного нового процесса.

**Tech Stack:** Python 3.9 (stdlib-only в `client/`), FastAPI+SQLite, pydantic v2, Jinja2 (autoescape ON), Plotly, canvas-JS.

## Global Constraints (копия инвариантов — действуют в КАЖДОЙ задаче)

- `client/` = чистый stdlib, ZERO внешних зависимостей. `[[agent-stdlib-only]]`
- Агент-PowerShell = **Windows PowerShell 5.1 floor**; language-independent (числа/английские enum). `[[agent-powershell-51-floor]]`
- Privacy: наружу только RFC1918; серийники — SHA-256; сертификаты без приватных ключей.
- Сервер: autoescape ON (никакого `|safe`), ВСЕ SQL параметризованы, pydantic v2 на границе; новые поля контракта аддитивно-опциональны → **CONTRACT_VERSION не бампить**.
- Prose оператора — русский, техтермины латиницей; machine values — английский.
- Python 3.9: explicit `Optional`, line 100, двойные кавычки; `# nosec <код>` только с причиной.
- Файлы <800 строк, функции <50, early returns, immutable-паттерны.
- PostToolUse-хук гоняет `ruff --fix`+`format` на каждом `.py` → импорт добавлять ВМЕСТЕ с первым использованием.
- OFF-by-default в коде ⇒ ON в shipped `server/config.json` в ТОМ ЖЕ изменении. `[[no-disabled-by-default]]`
- Гейт «done» (нет `make` — команды напрямую):
  `python -m ruff check .` · `python -m mypy` · `python -m bandit -c pyproject.toml -q -r server shared client` · `python -m pytest --cov=server --cov=shared --cov-report=term-missing` (fail_under 80) · `python smoke.py` — ВСЁ green.
- Git: branch-first → gate green → subagent-review (**security-reviewer** для веток 1, 2, 6, 7 — agent/ingest/SQL/subprocess; **code-reviewer** для 3, 4, 5) → `merge --no-ff` → `push origin main` — автоматически, не спрашивая. Conventional commits, без атрибуции. Стейджить ТОЛЬКО тронутые файлы (`git add <файл>…`, НИКОГДА `-A`); `client/config.json`/`org_directory.json` не коммитить.
- Видимое изменение → строка в `CHANGELOG.md` (`## [Unreleased]`, RU) в том же коммите; `CONTINUITY.md` обновить после каждой ветки.

## Порядок веток (зависимости)

| # | Ветка | Задания промпта | Зависит от |
|---|-------|-----------------|------------|
| 1 | `feat/print-log-repair` | З.10-P0, З.10-P3, часть З.7 (экспорт `NO_WINDOW`) | — |
| 2 | `feat/liveness-offline` | З.1 | — |
| 3 | `feat/netmap-medium-glyph` | З.4 + З.5 + принтеры-на-карте из З.3 | — |
| 4 | `feat/device-identity` | З.2 + З.3 (резолвер + карточка) | ветка 1 (баннер использует резолвер — правится здесь) |
| 5 | `feat/plotly-hover` | З.6 | — |
| 6 | `feat/agent-invisible` | З.7 (остаток) | ветка 1 (`NO_WINDOW` уже экспортирован) |
| 7 | `feat/ipp-get-jobs` | З.10-P1 | — |

З.8 (переиспользование) — раздел «Переиспользование» в каждой задаче. З.9 — вшито: нейтральная подпись-константа (в.4), memo-защита от мигания трея (в.6), общий hover-сниппет (в.5), `Hidden=true` в task XML (в.6).

**Решение по З.10-P2 (vendor-OID цвет/моно): НЕ заполнять.** Живой опрос НЕ подтвердил конкретные OID (HP «модельно-зависимо», Xerox — учёт выключен). Заполнение по догадке нарушает инвариант UNKNOWN-over-false-confidence и явный комментарий `oids.py:102-105` («fill per model once confirmed»). Механизм оверлея уже готов (`drivers/vendor.py:44-53` читает `oids.VENDOR`) — когда OID будут сняты с живого железа, заполнение = данные, не код. Команда для верификации на месте (ops-заметка, не код):
`python -c "from server.printers.snmp import snmp_get; print(snmp_get('192.168.9.8', ['<кандидат-OID>'], community='public', version=1, timeout=1.5))"`.

---

## Ветка 1: `feat/print-log-repair` — учёт «кто печатал» чинится у корня

### Цель
Сейчас: журнал `Microsoft-Windows-PrintService/Operational` на живых хостах бывает выключен (подтверждено хостом 192.168.9.100: GPO / деплой до появления инсталлятора / ручное отключение) → агент падает в counter-режим → `user_name=None` у всех заданий. Станет: SYSTEM-агент самолечит выключенный журнал, провал включения журнала инсталлятором виден в install.log, дашборд показывает баннер по ПК в counter-режиме, Kyocera 192.168.9.163 добавлена в static_ips.

### Диагноз (file:line)
- ~~`setup.py:251` опечатка `\e:true`~~ — **ложная тревога** (артефакт отображения grep-вывода): в файле `"/e:true"`, пин `tests/test_setup_logic.py:286-289` зелёный. Команда инсталлятора корректна. Шаги 1.1-1.2 сокращаются до правки `:366`.
- `client/deploy/setup.py:366` — `_run(wevtutil_enable_cmd())` вызывается БЕЗ `dest=` → по `setup.py:280` ненулевой rc никогда не логируется: если wevtutil на хосте падает (политики/локализация), это не видно.
- `client/collectors/print_jobs.py:227-237` — `_detect_mode()` честно видит выключенный журнал и уходит в counter (`user_name=None`, `print_jobs.py:275`), но не пытается его включить, хотя агент работает под SYSTEM.
- Дашборд нигде не показывает, что часть флота потеряла атрибуцию печати (`source='counter'` лежит в `print_jobs.source`, `server/db.py:195`).

### Переиспользование
- `client/collectors/ps.py:31` `_NO_WINDOW` — станет публичным `NO_WINDOW` (нужен здесь и в ветке 6).
- `_detect_mode()`/`run_ps` — уже есть; самолечение вставляется в существующий sweep.
- `print_jobs.source` — колонка уже есть; баннер = один read-запрос.
- `server/printers/config.py:53` — static_ips уже RFC1918-фильтруются на загрузке.

### Шаги

- [ ] **1.1** ~~RED-тест на `/e:true`~~ — уже существует и зелёный (`tests/test_setup_logic.py:286`). Пропущен.

- [ ] **1.2** `client/deploy/setup.py:366` → `_run(wevtutil_enable_cmd(), dest=dest, label="wevtutil print-log")` (провал включения журнала теперь виден в install.log).

- [ ] **1.3** `client/collectors/ps.py`: сразу после строки 31 добавить публичный алиас (одной правкой с первым использованием в 1.4):

```python
NO_WINDOW = _NO_WINDOW  # public: единый флаг «без окна» для ВСЕХ subprocess в client/
```

- [ ] **1.4 RED:** в `tests/test_print_fallback.py` добавить тест самолечения (monkeypatch `subprocess.run` и `run_ps`):

```python
def test_counter_mode_attempts_print_log_enable(monkeypatch):
    from client.collectors import print_jobs as pj
    calls = []
    monkeypatch.setattr(pj, "_ENABLE_ATTEMPTED", False)
    monkeypatch.setattr(pj.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    pj._try_enable_print_log()
    pj._try_enable_print_log()  # повторный вызов — no-op (1 попытка на процесс)
    assert calls == [["wevtutil", "sl", pj._PRINT_LOG, "/e:true"]]
```

- [ ] **1.5 GREEN:** в `client/collectors/print_jobs.py` (рядом с `_detect_mode`, импорты `subprocess` и `NO_WINDOW` из `client.collectors.ps` добавить вместе с использованием):

```python
_PRINT_LOG = "Microsoft-Windows-PrintService/Operational"
_ENABLE_ATTEMPTED = False  # 1 попытка на процесс агента: не бодаться с GPO каждый sweep


def _try_enable_print_log() -> None:
    """SYSTEM-агент включает операционный журнал печати, если он выключен.

    То же действие выполняет инсталлятор; здесь — самолечение уже развёрнутого
    парка. Провал глотается: следующий sweep честно останется в counter-режиме.
    """
    global _ENABLE_ATTEMPTED
    if _ENABLE_ATTEMPTED:
        return
    _ENABLE_ATTEMPTED = True
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # nosec B603 B607 -- фиксированный argv, системная утилита
            ["wevtutil", "sl", _PRINT_LOG, "/e:true"],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )
```

В месте, где `collect_print_jobs` выбирает режим (`mode = _detect_mode()`): если `mode == "counter"` → вызвать `_try_enable_print_log()` (текущий sweep остаётся counter — журнал пуст; следующий переключится сам). Тест green.

- [ ] **1.6 RED:** в `tests/test_print_page.py` (паттерн клиент-фикстуры там уже есть): устройство с job'ами `source="counter"` за 7 дней и без `source="events"` → в HTML `/print` есть текст `Журнал печати` и hostname устройства; устройство с events-job'ами в баннер не попадает.

- [ ] **1.7 GREEN:** в `server/db.py` (рядом с print-query функциями ~:2850):

```python
def get_print_counter_mode_devices(days: int = 7) -> list[dict[str, Any]]:
    """ПК, чья печать за окно пришла ТОЛЬКО counter-фолбэком (журнал выключен).

    На таких ПК user_name пуст -- оператор должен включить PrintService/Operational.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT p.device_id, d.hostname AS hostname, MAX(p.ts) AS last_ts
            FROM print_jobs p LEFT JOIN devices d ON d.device_id = p.device_id
            WHERE p.ts >= datetime('now', ?) AND p.source = 'counter'
              AND NOT EXISTS (
                SELECT 1 FROM print_jobs e
                WHERE e.device_id = p.device_id AND e.source = 'events'
                  AND e.ts >= datetime('now', ?)
              )
            GROUP BY p.device_id ORDER BY hostname
            """,
            (f"-{days} days", f"-{days} days"),
        ).fetchall()
    return [dict(r) for r in rows]
```

Роут страницы `/print` (найти: `grep -n "\"/print\"" server/web/dashboard.py`) передаёт `counter_devices=db.get_print_counter_mode_devices()` в контекст; в `server/web/templates/print.html` сверху:

```html
{% if counter_devices %}
<div class="banner warn">
  Журнал печати (PrintService/Operational) выключен на {{ counter_devices|length }} ПК —
  «кто печатал» не учитывается:
  {% for c in counter_devices %}{{ c.hostname or "Без названия" }}{% if not loop.last %}, {% endif %}{% endfor %}.
  Агент попробует включить журнал сам; для GPO-парка включите централизованно.
</div>
{% endif %}
```

(CSS-класс баннера — взять существующий warn/badge-класс страницы; никакого `|safe`.) Тест green.

- [ ] **1.8** `server/config.json`: `"static_ips": []` → `"static_ips": ["192.168.9.163"]` (Kyocera M4132idn, видна по локальным очередям, SNMP молчит — discovery добавит её кандидатом; RFC1918-фильтр в `printers/config.py:53` пропустит).

- [ ] **1.9** CHANGELOG (`## [Unreleased]`):
  - `### Исправлено` — «Инсталлятор реально включает журнал печати PrintService/Operational (опечатка \e:true → /e:true); ошибка включения теперь пишется в install.log».
  - `### Добавлено` — «Агент самолечит выключенный журнал печати (1 попытка на запуск); на /print — баннер со списком ПК без атрибуции печати (counter-режим)».

- [ ] **1.10** Гейт полностью (см. Global Constraints) → **security-reviewer** (агент, subprocess, SQL) → `git add` только тронутых файлов → conventional commit (`fix(print): включение журнала печати чинится у корня + самолечение + баннер counter-режима`) → `merge --no-ff` в main → `push origin main`. CONTINUITY.md обновить.

### Риски / инварианты
- `wevtutil` — exe, не PowerShell → PS 5.1-floor не задет. Argv фиксированный — инъекций нет.
- Самолечение меняет состояние ОС: это ровно то, что уже делает инсталлятор; 1 попытка/процесс не дерётся с GPO.
- Баннер: только имена из БД через autoescape; нет `|safe`.

### Верификация
`python -m pytest tests/test_setup_logic.py tests/test_print_fallback.py tests/test_print_page.py -q` + полный гейт + `python smoke.py`.

---

## Ветка 2: `feat/liveness-offline` — offline виден ≤10 минут

### Цель
Сейчас: единственный сигнал жизни — полный телеметрийный цикл раз в 4 ч (`client/config.py:49-53`, все интервалы 14400), порог stale ≈ 8.25 ч (`server/db.py:1537`) → выключенный ПК «онлайн» до 8 часов. Станет: лёгкий liveness-конверт каждые 300 с (без PowerShell, без записи строк в БД), порог stale = 600 с (конфигурируемый), «офлайн» на дашборде ≤ 10 мин.

### Диагноз (file:line)
- `client/agent.py:71-86` — `run_forever` уже просыпается каждые ≤60 с (`_MAX_SLEEP_SEC=60`) и гоняет задачи по их интервалам → частая задача добавляется одной строкой в `TASKS` (`agent.py:45-50`).
- `server/db.py:561-615` `touch_device` — обновляет `devices.last_seen` server-receipt-временем на любой конверт; больше ничего не нужно.
- `server/pipeline.py:232-347` — dispatch по `msg_type`; `:347` рескоринг пропускается для `{"events","print_jobs"}` — liveness обязан попасть в этот skip-set (иначе O(fleet) рескоринг каждые 5 мин).
- `shared/schema.py:27` `MsgType` Literal; `:284` `payload: dict` + `:339` `parse_payload` по `_PAYLOAD_MODELS` — новый тип регистрируется аддитивно.
- `client/transport.py:69` — конверт с falsy payload И falsy source_health ПРОПУСКАЕТСЯ → liveness-payload обязан быть непустым (`{"alive": true}`), не `{}`.
- Порог: `server/db.py:1537-1538` (константа+алиас), потребители `db.py:1672` (fleet flag), `db.py:2632` (pipeline-метрика, SQL-модификатор), `dashboard.py:377` (страница устройства), пины `tests/test_dashboard_api.py:59-60`.
- `server/trust/gate.py:36-45` — отдельный per-source trust-порог, НЕ трогать (liveness шлёт пустой `source_health` → `pipeline.py:334` `if env.source_health:` не срабатывает → trust не задет; collector⊥semantic сохранён).

### Переиспользование
- Loop/TASKS-паттерн агента (`agent.py:45-63`) — liveness = ещё один кортеж.
- `transport._envelope` (`transport.py:112-121`) — hostname уже на каждом конверте → `touch_device` продолжит COALESCE-ить имя.
- `touch_device` — единственный write; ни одной новой таблицы/строки (retention не растёт).
- Второй порог НЕ плодится: trust-gate остаётся своим, wall-clock порог остаётся один в `db.py`.

### Шаги

- [ ] **2.1 RED:** новый `tests/test_liveness.py`:

```python
"""Liveness-конверт: контракт, ingest, порог offline."""
from client.agent import TASKS
from server import db, pipeline
from shared.schema import Envelope, parse_payload


def test_contract_accepts_liveness_msg_type():
    env = Envelope(device_id="dev-1", msg_type="liveness", payload={"alive": True})
    assert env.msg_type == "liveness"
    assert parse_payload("liveness", {"alive": True}).alive is True


def test_agent_has_liveness_task_with_own_interval():
    assert ("liveness") in [t[0] for t in TASKS]
    attr = dict((t[0], t[2]) for t in TASKS)["liveness"]
    assert attr == "liveness_interval_sec"


def test_liveness_payload_is_truthy_so_transport_never_skips():
    from client.collectors.liveness import collect_liveness
    result = collect_liveness()
    assert result.payload  # transport.py:69 пропускает falsy payload без source_health


def test_ingest_liveness_touches_last_seen_and_skips_scoring(tmp_path, monkeypatch):
    # использовать существующую db-фикстуру проекта (как в tests/test_ingest.py)
    calls = []
    monkeypatch.setattr(pipeline, "recompute_scores", lambda did: calls.append(did))
    env = Envelope(device_id="dev-lv", msg_type="liveness", payload={"alive": True})
    pipeline.ingest_envelope(env)
    assert db.get_device("dev-lv") is not None  # last_seen проставлен
    assert calls == []  # рескоринга не было


def test_stale_threshold_default_600_and_configurable():
    assert db.STALE_AFTER_SEC == 600
    db.set_stale_threshold(1200)
    try:
        assert db.STALE_AFTER_SEC == 1200
    finally:
        db.set_stale_threshold(600)
```

(Фикстуру инициализации БД взять из `tests/test_ingest.py` — там уже есть паттерн temp-db.) Запуск: FAIL.

- [ ] **2.2 GREEN контракт:** `shared/schema.py`:
  - `:27` → `MsgType = Literal["inventory", "historical", "heartbeat", "events", "print_jobs", "liveness"]`
  - Рядом с `HeartbeatPayload` (после `:243`):

```python
# --------------------------------------------------------------------------- #
# Liveness  (частый пинг «я жив»; НИКАКОЙ телеметрии — только last_seen)
# --------------------------------------------------------------------------- #
class LivenessPayload(_Base):
    # Одно непустое поле: transport пропускает конверты с пустым payload.
    alive: Optional[bool] = None
```

  - В `_PAYLOAD_MODELS` (рядом с `:339`) добавить `"liveness": LivenessPayload`.
  - CONTRACT_VERSION НЕ трогать (аддитивный msg_type: старый агент его не шлёт; старый сервер новому агенту ответит 422 → `transport.py:170` дропнет ровно liveness, телеметрия не страдает; порядок деплоя: сервер раньше агентов).

- [ ] **2.3 GREEN клиент:** новый `client/collectors/liveness.py`:

```python
"""Liveness-пинг: минимальный конверт без PowerShell и без сбора данных.

Единственная задача — обновить devices.last_seen на сервере, чтобы «offline»
был виден за минуты, а не за полный 4-часовой телеметрийный цикл.
"""

from __future__ import annotations

from client.collectors.sources import CollectorResult


def collect_liveness() -> CollectorResult:
    return CollectorResult({"alive": True}, {})
```

`client/collectors/__init__.py`: экспортировать `collect_liveness` рядом с `collect_heartbeat`. `client/config.py` (после `:53`): `liveness_interval_sec: int = 300  # пинг «я жив»; offline на дашборде виден за ~10 мин`. `client/agent.py:45-50` — добавить в `TASKS` последним: `("liveness", collect_liveness, "liveness_interval_sec")` (импорт вместе с использованием).

- [ ] **2.4 GREEN сервер:** `server/pipeline.py`:
  - после ветки `print_jobs` (`:332`) добавить:

```python
    elif env.msg_type == "liveness":
        # Только last_seen: ни строк в БД, ни трастов, ни рескоринга.
        db.touch_device(
            did,
            ts,
            env.agent_version,
            hostname=env.hostname,
            site_code=env.site_code,
            site_name=env.site_name,
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
```

  - `:347` → `scores = None if env.msg_type in {"events", "print_jobs", "liveness"} else recompute_scores(did)`.

- [ ] **2.5 GREEN порог:** `server/db.py:1531-1538` заменить блок на:

```python
# Liveness-пинг агента идёт каждые ~300 с (client/config.py liveness_interval_sec),
# так что 2 пропущенных пинга = offline. Порог настраивается: server/config.json
# "stale_after_sec" -> set_stale_threshold() при старте (main.create_app).
# Это dashboard-only сигнал; trust-порог per-source живёт в server/trust/gate.py.
_AGENT_CADENCE_SEC = 14400  # полный телеметрийный цикл (справочно)
_DEFAULT_STALE_AFTER_SEC = 600
_STALE_AFTER_SEC = _DEFAULT_STALE_AFTER_SEC
STALE_AFTER_SEC = _STALE_AFTER_SEC  # public alias for dashboard


def set_stale_threshold(seconds: int) -> None:
    """Применить порог offline из server/config.json (вызывается при старте)."""
    global _STALE_AFTER_SEC, STALE_AFTER_SEC
    _STALE_AFTER_SEC = max(60, int(seconds))
    STALE_AFTER_SEC = _STALE_AFTER_SEC
```

`server/config.py`: в `ServerConfig` после `retain_events` добавить `stale_after_sec: int = 600  # порог «offline» на дашборде, сек (2 liveness-пинга)`. `server/main.py`: в `create_app` сразу после `cfg = cfg or load_config()` (`main.py:302`) → `db.set_stale_threshold(cfg.stale_after_sec)`. `server/config.json`: добавить `"stale_after_sec": 600` после `"retain_events"`. `dashboard.py:377` и `db.py:1672/2632` менять не надо — читают модульные глобалы в момент вызова.

- [ ] **2.6 Пины:** `tests/test_dashboard_api.py:59-60` переписать под новую семантику (комментарий «missed ~2 cycles» устарел): возраст 300 с → НЕ stale; возраст 1300 с → stale. Прогнать `python -m pytest tests/test_liveness.py tests/test_dashboard_api.py tests/test_contract.py tests/test_ingest.py -q` → PASS.

- [ ] **2.7** CHANGELOG `### Добавлено` — «Агент шлёт liveness-пинг каждые 5 мин; «offline» на дашборде виден за ≤10 мин (порог stale_after_sec=600 в server/config.json) вместо ~8 часов». `### Изменено` — «До обновления агентов парк будет помечен offline по новому порогу — обновите агенты сразу после сервера».

- [ ] **2.8** Гейт → **security-reviewer** (contract/ingest surface) → commit (`feat(liveness): offline-детект ≤10 мин без утяжеления телеметрии`) → merge --no-ff → push. CONTINUITY.md.

### Риски / инварианты
- Contract additive-only: новый Literal-член + новая модель; bump не нужен; порядок деплоя сервер→агенты зафиксирован в CHANGELOG.
- Нагрузка: liveness = 1 HTTP POST/5 мин/ПК, ноль строк БД (только UPDATE devices), ноль PowerShell, рескоринг пропущен.
- Trust-gate (`server/trust/gate.py:36`) не задет: пустой source_health → evaluate_trust не вызывается.
- Смешанный парк: старые агенты будут «offline» до обновления — осознанно (см. CHANGELOG).

### Верификация
`python -m pytest tests/test_liveness.py tests/test_dashboard_api.py tests/test_contract.py tests/test_ingest.py tests/test_transport.py -q` + полный гейт + `python smoke.py`.

---

## Ветка 3: `feat/netmap-medium-glyph` — принтер-фильтр и wireless-пунктир

### Цель
(З.4) Чекбокс «принтер» не влияет на принтеры, чей net_devices-тип классифицирован как «endpoint»: они и выглядят, и фильтруются как endpoint. (З.5) Wireless-uplink агента рисуется сплошным, если первым в списке адаптеров стоит неактивный ethernet с остаточным gateway. Станет: связанный принтер всегда несёт `dev_type="printer"` (кроме infra-типов), uplink-medium берётся с работающего адаптера.

### Диагноз (file:line)
- Фильтр сам по себе исправен: `_netgraph.html:417` `if(!state.types[n.type]) return false`; `:306-310` `t = n.dev_type || "unknown"` → `type: t`. Т.е. `n.type` = ровно `dev_type` ассемблера.
- Корень З.4: `unified.py:112-113` — glyph-upgrade `printer_id → dev_type="printer"` происходит ТОЛЬКО из `"unknown"`; сохранённый `dev_type="endpoint"` (пассивная классификация) остаётся → узел не «принтер» ни глифом, ни фильтром. Симметрично `_enrich_printer` `unified.py:299-300` (`in (None, "unknown")`).
- Это же закрывает «принтеры теряются на карте» из З.3: `_merge_printers` (`unified.py:322-345`) уже кладёт В ГРАФ ВСЕ строки `printers` (cache.py:38 передаёт `db.get_printers()` без фильтра; фантомы ARP удаляются на уровне БД — `db.py:2262` `_printer_is_unlisted_arp` + delete-sweep) — «пропавший» живой принтер на самом деле стоит на карте с чужим типом.
- Корень З.5: `unified.py:439-448` `_gateways` берёт kind ПЕРВОГО адаптера с данным gateway, игнорируя `up`. Цепочка до и после исправна: агент `network.py:52-57` (`ifType 71 → "wifi"`), snapshot несёт `kind`/`up`/`gateway` (`network.py:101-106`, сервер отдаёт сырые адаптеры `db.py:1871`), `_medium_for_adapter` (`unified.py:78-80`, `_WIRELESS_KINDS` содержит `"wifi"`), ребро несёт medium (`unified.py:519`), canvas пунктирит (`_netgraph.html:816`). Свойства компьютера уже показывают kind (`device.html:625`).

### Переиспользование
- `_medium_for_adapter`/`_WIRELESS_KINDS` — НЕ дублировать, чинится только выбор адаптера.
- Тест-паттерны: `tests/test_netmap_unified.py` (ассемблер, pure-функции), `tests/test_netmap_web.py:484-504` (structural-pin JS).
- `wireless.py:84` (client→AP рёбра) — уже wireless, не трогать.

### Шаги

- [ ] **3.1 RED:** в `tests/test_netmap_unified.py` добавить:

```python
def test_linked_printer_upgrades_endpoint_glyph():
    """Endpoint-классифицированный узел со связанным printer_id = принтер (З.4)."""
    from server.netdisco.unified import build_network_map
    nd = [{"device_nid": "nd-p1", "ip": "192.168.9.5", "mac": "aa:bb:cc:dd:ee:01",
           "dev_type": "endpoint", "printer_id": "prn-1", "device_id": None}]
    g = build_network_map(nd, [], [], [])
    node = next(n for n in g["nodes"] if n["nid"] == "nd-p1")
    assert node["dev_type"] == "printer"


def test_merge_printer_upgrades_existing_endpoint_node():
    from server.netdisco.unified import build_network_map
    nd = [{"device_nid": "nd-p2", "ip": "192.168.9.8", "mac": "aa:bb:cc:dd:ee:02",
           "dev_type": "endpoint", "printer_id": None, "device_id": None}]
    printers = [{"printer_id": "prn-2", "ip": "192.168.9.8", "mac": "aa:bb:cc:dd:ee:02"}]
    g = build_network_map(nd, [], [], printers)
    node = next(n for n in g["nodes"] if n["nid"] == "nd-p2")
    assert node["dev_type"] == "printer" and node["printer_id"] == "prn-2"


def test_stored_infra_type_survives_printer_link():
    """Принт-сервер, классифицированный как server/switch, НЕ перекрашивается."""
    from server.netdisco.unified import build_network_map
    nd = [{"device_nid": "nd-sw", "ip": "192.168.9.9", "mac": "aa:bb:cc:dd:ee:03",
           "dev_type": "switch", "printer_id": "prn-3", "device_id": None}]
    g = build_network_map(nd, [], [], [])
    assert next(n for n in g["nodes"] if n["nid"] == "nd-sw")["dev_type"] == "switch"


def test_uplink_medium_prefers_up_adapter():
    """Wi-Fi up=True бьёт ethernet up=False с тем же шлюзом (З.5)."""
    from server.netdisco.unified import build_network_map
    snap = {"device_id": "dev-a", "hostname": "PC-A",
            "adapters": [
                {"kind": "ethernet", "up": False, "gateway": "192.168.9.1", "mac": "aa:bb:cc:00:00:01"},
                {"kind": "wifi", "up": True, "gateway": "192.168.9.1", "mac": "aa:bb:cc:00:00:02"},
            ],
            "neighbors": [], "quality": []}
    g = build_network_map([], [], [snap], [])
    uplink = next(e for e in g["links"] if e["link_kind"] == "agent-uplink")
    assert uplink["medium"] == "wireless"
    assert g["totals"]["wireless_links"] == 1
```

Запуск: `python -m pytest tests/test_netmap_unified.py -q` → 4 FAIL (шаблон snap подогнать под соседние тесты файла, если ключи отличаются).

- [ ] **3.2 GREEN З.4:** `server/netdisco/unified.py`:
  - `:109-113` заменить на:

```python
    dev_type = d.get("dev_type") or "unknown"
    if device_id and dev_type == "unknown":
        dev_type = "agent"  # a linked agent gets the agent glyph, not "unknown"
    elif printer_id and dev_type in _PRINTER_UPGRADABLE:
        # SNMP-подтверждённый printer_id сильнее пассивной endpoint-классификации;
        # infra-типы (router/switch/ap/server) сохраняются — принт-сервер не принтер.
        dev_type = "printer"
```

  - рядом с `_WIRELESS_KINDS` (`:29`) добавить `_PRINTER_UPGRADABLE = ("unknown", "endpoint")`.
  - `_enrich_printer` `:299` → `if node.get("dev_type") in (None, "unknown", "endpoint"):`.

- [ ] **3.3 GREEN З.5:** `unified.py:439-448` заменить тело `_gateways`:

```python
def _gateways(snap: dict[str, Any]) -> dict[str, Optional[str]]:
    """gateway-IP -> kind адаптера, через который шлюз реально работает.

    up=True адаптеры идут первыми: неактивный ethernet с остаточным gateway не
    должен красить uplink в wired, когда живой линк — Wi-Fi (sorted стабилен,
    внутри групп порядок агента сохраняется)."""
    out: dict[str, Optional[str]] = {}
    adapters = [a for a in snap.get("adapters") or [] if isinstance(a, dict)]
    for a in sorted(adapters, key=lambda a: a.get("up") is not True):
        gw = a.get("gateway")
        if gw and str(gw) not in out:
            out[str(gw)] = a.get("kind")
    return out
```

- [ ] **3.4** Structural-pin в `tests/test_netmap_web.py` (по образцу `:484-504`):

```python
def test_netmap_type_filter_reads_dev_type(client):
    """Пин З.4: фильтр типов и данные узла используют ОДНО поле dev_type."""
    body = client.get("/netmap").text
    assert "state.types[n.type]" in body
    assert 'n.dev_type || "unknown"' in body
```

- [ ] **3.5** Прогнать `python -m pytest tests/test_netmap_unified.py tests/test_netmap_web.py tests/test_netmap_identity.py -q` → PASS. CHANGELOG `### Исправлено` — «Карта сети: связанный принтер больше не прячется под типом endpoint (фильтр «принтер» действует на все принтеры)»; «Карта сети: Wi-Fi-uplink агента рисуется пунктиром и при наличии неактивного ethernet-адаптера с остаточным шлюзом».

- [ ] **3.6** Гейт → **code-reviewer** → commit (`fix(netmap): printer-глиф для linked-endpoint узлов + medium uplink'а с работающего адаптера`) → merge --no-ff → push. CONTINUITY.md.

### Риски / инварианты
- «Stored wins» (память `[[netmap-snmp-deepen]]`) сужается ОСОЗНАННО только для `{unknown, endpoint}` при наличии printer_id (это SNMP-подтверждение, более сильное свидетельство, чем пассивная классификация); infra-типы неприкосновенны — тест 3.1/третий пинит.
- Ассемблер остаётся pure (никакого wall-clock/БД внутри).
- `_medium_for_link` (SNMP-рёбра) не тронут.

### Верификация
`python -m pytest tests/test_netmap_unified.py tests/test_netmap_web.py -q` + полный гейт. Живая проверка: `/netmap` → чекбокс «принтер» скрывает/показывает ВСЕ принтерные узлы; Wi-Fi-ПК → пунктир к шлюзу.

---

## Ветка 4: `feat/device-identity` — один резолвер имени + identity-карточка (З.2 + З.3)

### Цель
Сейчас: «dev-…» светится как заголовок в fleet/device/print-вкладках; логика имени размазана по ~9 SQL `COALESCE(d.hostname, p.device_id)` + Jinja `or d.device_id`; вкладки читают 3 таблицы идентичности врозь (fleet может не знать IP, который знает netmap). Станет: единый `db.display_name()` (нейтральная подпись «Без названия», device_id — только tooltip/дизамбигуация), единая `db.get_identity_card()` поверх devices⋈printers⋈net_devices, fleet берёт IP из net_devices, когда historical его не знает.

### Диагноз (file:line)
- Jinja-фоллбеки: `_fleet_body.html:85,86,171`; `device.html:2,120`.
- SQL-фоллбеки display-класса: `db.py:2184, 2856, 3015, 3080, 3190, 3368, 3398` (`COALESCE(d.hostname, p.device_id) AS hostname` в print-запросах); `:3032` (sort-map) и `:3064` (search LIKE) — семантика сортировки/поиска, display не является.
- Ordering-only (НЕ трогать): `db.py:2034` (printers), `:1208` (net_devices).
- Идентичность: 3 таблицы (`db.py: devices / printers:210 / net_devices:236`), FK `net_devices.device_id/printer_id:251-252`, merge уже существует, но только для графа (`unified.py:645-647`); fleet `local_ip` берётся только из historical (`db.py:1671` `_primary_ip(hist_payload)`).
- В `devices` уже есть `model`, `chassis` (`db.py:1641`) — приоритеты резолвера покрываются без новых JOIN'ов.

### Переиспользование
- `touch_device` COALESCE-персист hostname (тред 2026-06-29) — уже даёт максимум живых имён; резолвер только доедает пустые.
- `normalize_mac` (уже импортируется в `unified.py` из netdisco identity-модуля) — для карточки.
- FK `net_devices.device_id/printer_id` — проставляются планировщиком (`netdisco/scheduler.py:98`), карточка их просто читает.
- Structural-pin паттерн `test_netmap_web.py` — для «в шаблонах не осталось `or d.device_id`».

### Шаги

- [ ] **4.1 RED:** новый `tests/test_display_name.py`:

```python
from server.db import NEUTRAL_NAME, display_name


def test_hostname_wins():
    assert display_name("PC-01", model="OptiPlex", chassis="desktop") == "PC-01"


def test_falls_back_model_then_chassis_then_ip():
    assert display_name(None, model="OptiPlex 7080") == "OptiPlex 7080"
    assert display_name("", model=None, chassis="laptop") == "laptop"
    assert display_name(None, ip="192.168.9.50") == "192.168.9.50"


def test_never_returns_device_id_as_title():
    assert display_name(None, device_id="dev-1a2b3c4d") == NEUTRAL_NAME


def test_disambiguate_appends_short_suffix_only_when_empty():
    got = display_name(None, device_id="dev-1a2b3c4d", disambiguate=True)
    assert got.startswith(NEUTRAL_NAME) and "2b3c4d" in got
    assert display_name("PC-02", device_id="dev-x", disambiguate=True) == "PC-02"


def test_templates_have_no_device_id_fallback():
    """Пин З.2: ни один шаблон не рисует dev-… как имя."""
    import pathlib
    tpl = pathlib.Path(__file__).resolve().parents[1] / "server" / "web" / "templates"
    offenders = [p.name for p in tpl.glob("*.html") if "or d.device_id" in p.read_text(encoding="utf-8")]
    assert offenders == []
```

FAIL (функции нет; шаблоны содержат фоллбек).

- [ ] **4.2 GREEN резолвер:** `server/db.py` (перед `get_fleet`-блоком, рядом с `:1531`):

```python
NEUTRAL_NAME = "Без названия"


def display_name(
    hostname: Optional[str],
    *,
    model: Optional[str] = None,
    chassis: Optional[str] = None,
    ip: Optional[str] = None,
    device_id: Optional[str] = None,
    disambiguate: bool = False,
) -> str:
    """Единственный источник операторского имени устройства.

    Приоритет: hostname -> model -> chassis -> ip -> «Без названия». device_id
    НИКОГДА не возвращается как имя; в disambiguate-режиме (выпадающие списки,
    где пустые имена сливаются) добавляется короткий суффикс id.
    """
    for candidate in (hostname, model, chassis, ip):
        text = (candidate or "").strip()
        if text:
            return text
    if disambiguate and device_id:
        return f"{NEUTRAL_NAME} ({device_id[-6:]})"
    return NEUTRAL_NAME
```

- [ ] **4.3 GREEN потребители:**
  - `db.get_fleet` (`:1663`-словарь): добавить ключ `"display_name": display_name(r["hostname"], model=r["model"], chassis=r["chassis"])`.
  - `dashboard.device()` (`dashboard.py:374-379`): в словарь добавить `"display_name": db.display_name(d.get("hostname"), model=d.get("model"), chassis=d.get("chassis"))`.
  - `_fleet_body.html:85` → `data-name="{{ d.display_name|lower }}"`; `:86` → `<a href="/device/{{ d.device_id }}" title="{{ d.device_id }}">{{ d.display_name }}</a>` (суб-подпись chassis/model оставить); `:171` → `data-name="{{ d.display_name }}"`.
  - `device.html:2` → `{% block title %}{{ d.display_name }} · SRP{% endblock %}`; `:120` → `<h1 title="{{ d.device_id }}" …>{{ d.display_name }}</h1>`.
  - Print-запросы `db.py:2184, 2856, 3015, 3080, 3190, 3368, 3398`: в SELECT заменить `COALESCE(d.hostname, p.device_id) AS hostname` на `d.hostname AS hostname` (device_id в этих запросах уже выбирается), а в Python-обвязке каждой функции, где строится итоговый ряд, отдавать `"hostname": display_name(r["hostname"], device_id=r["device_id"], disambiguate=True)`. `:3032` (sort-map) и `:3064` (search) НЕ трогать — сортировка/поиск по id остаются рабочими. Баннер ветки 1 (`get_print_counter_mode_devices`) тоже перевести на `display_name`.
  - После правок обязательный само-чек: `grep -n "COALESCE(d.hostname, p.device_id)" server/db.py` → остаться должны ТОЛЬКО `:3032` и `:3064`; `grep -rn "or d.device_id" server/web/templates` → пусто.

- [ ] **4.4 RED карточка:** новый `tests/test_device_matrix.py` (temp-db фикстура по образцу `tests/test_netdisco_db.py`):

```python
def test_identity_card_merges_three_tables(seeded_db):
    """devices + printers + net_devices -> одна карточка, agent-поля приоритетнее."""
    from server import db
    # seed: устройство dev-1 (hostname PC-1), net_device nd-1 (device_id=dev-1,
    # ip 192.168.9.50, mac aa:..:01), принтер prn-1 (net_device nd-2, printer_id=prn-1)
    card = db.get_identity_card(device_id="dev-1")
    assert card["display_name"] == "PC-1"
    assert card["ip"] == "192.168.9.50"      # добрано из net_devices
    assert card["net_nid"] == "nd-1"
    assert "agent" in card["sources"] and "net" in card["sources"]


def test_identity_card_by_mac_and_ip(seeded_db):
    from server import db
    assert db.get_identity_card(mac="AA:BB:CC:DD:EE:01")["device_id"] == "dev-1"
    assert db.get_identity_card(ip="192.168.9.50")["device_id"] == "dev-1"


def test_identity_card_none_when_nothing_matches(seeded_db):
    from server import db
    assert db.get_identity_card(ip="10.99.99.99") is None
```

- [ ] **4.5 GREEN карточка:** в `server/db.py` (после `display_name`):

```python
def get_identity_card(
    *,
    device_id: Optional[str] = None,
    printer_id: Optional[str] = None,
    nid: Optional[str] = None,
    mac: Optional[str] = None,
    ip: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Единая карточка идентичности по ЛЮБОМУ ключу (З.3, read-only).

    Мердж fill-empty с приоритетом agent > printer > net (модули дополняют,
    не перезаписывают). Все три источника перечислены в card["sources"].
    """
    with _connect() as conn:
        net = _find_net_device(conn, device_id, printer_id, nid, mac, ip)
        if net is not None:
            device_id = device_id or net["device_id"]
            printer_id = printer_id or net["printer_id"]
        dev = (
            conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if device_id
            else None
        )
        prn = (
            conn.execute("SELECT * FROM printers WHERE printer_id=?", (printer_id,)).fetchone()
            if printer_id
            else None
        )
    if dev is None and prn is None and net is None:
        return None
    layers = [("agent", dev), ("printer", prn), ("net", net)]
    card: dict[str, Any] = {"device_id": device_id, "printer_id": printer_id,
                            "net_nid": net["device_nid"] if net is not None else None,
                            "sources": [name for name, row in layers if row is not None]}
    for field in ("hostname", "ip", "mac", "vendor", "model", "serial", "status"):
        for _, row in layers:
            value = row[field] if row is not None and field in row.keys() else None
            if value:
                card[field] = value
                break
        card.setdefault(field, None)
    seen = [row["last_seen"] for _, row in layers if row is not None and row["last_seen"]]
    card["last_seen"] = max(seen) if seen else None
    card["display_name"] = display_name(
        card["hostname"], model=card["model"], ip=card["ip"], device_id=device_id,
    )
    return card


def _find_net_device(conn, device_id, printer_id, nid, mac, ip):
    """net_devices по первому сработавшему ключу: nid > device_id > printer_id > mac > ip."""
    from server.netdisco.identity import normalize_mac  # тот же нормализатор, что в unified

    for column, value in (
        ("device_nid", nid),
        ("device_id", device_id),
        ("printer_id", printer_id),
        ("mac", normalize_mac(mac) if mac else None),
        ("ip", ip),
    ):
        if not value:
            continue
        row = conn.execute(
            f"SELECT * FROM net_devices WHERE {column}=? LIMIT 1", (value,)  # nosec B608 -- column из фикс. кортежа
        ).fetchone()
        if row is not None:
            return row
    return None
```

(Точный import-путь `normalize_mac` взять из `unified.py` — верхний блок импортов; `devices` не имеет колонок `serial`/`vendor` — обращение через `field in row.keys()` это гасит. `_find_net_device` — приватный, сигнатуру типизировать по месту.)

- [ ] **4.6 GREEN сшивка вкладок (минимальный видимый эффект З.3):**
  - `db.get_fleet` (`:1640`): в SELECT добавить `nd.ip AS net_ip`, во FROM — `LEFT JOIN (SELECT device_id, MIN(ip) AS ip FROM net_devices WHERE device_id IS NOT NULL GROUP BY device_id) nd ON nd.device_id = d.device_id` (подзапрос, НЕ прямой JOIN: у устройства может быть >1 net-строки — прямой JOIN размножил бы ряды флота); `:1671` → `"local_ip": _primary_ip(r["hist_payload"]) or r["net_ip"],` (fleet теперь знает IP, который знает карта). Пин-тест в `tests/test_device_matrix.py`: устройство с 2 net-строками встречается во флоте ровно один раз.
  - `server/api.py`: добавить `GET /api/v1/device-card` (query: `device_id|printer_id|nid|mac|ip`, все Optional) → `db.get_identity_card(...)`, 404 если None. Разместить рядом с существующими GET-роутами, тем же стилем.
  - `dashboard.device()` — в контекст добавить `"card": db.get_identity_card(device_id=device_id)`; в `device.html` в шапку — строка `IP/MAC` из card, если их нет в historical-блоке (мелкая вставка рядом с `:120`, autoescape).

- [ ] **4.7** Тесты: `python -m pytest tests/test_display_name.py tests/test_device_matrix.py tests/test_dashboard_api.py tests/test_print_page.py tests/test_print_records.py tests/test_print_summary.py tests/test_print_filter_options.py -q` → PASS (print-тесты, пинившие `dev-…` в подписи, обновить на `Без названия (…)`).

- [ ] **4.8** CHANGELOG `### Изменено` — «Технические имена dev-… исчезли из интерфейса: единый резолвер имени (hostname → модель → chassis → IP → «Без названия»); device_id остался в tooltip»; `### Добавлено` — «Единая identity-карточка устройства (/api/v1/device-card): agent+printer+net_device знания дополняют друг друга; Флот показывает IP из карты сети, когда телеметрия его не знает».

- [ ] **4.9** Гейт → **code-reviewer** (SQL параметризован; nosec с причиной) → commit (`feat(identity): display_name-резолвер + identity-карточка + IP-fallback во Флоте`) → merge --no-ff → push. CONTINUITY.md.

### Риски / инварианты
- SQL: единственная f-string — по фиксированному кортежу колонок (`# nosec B608` с причиной); всё остальное параметризовано.
- Группировки/JOIN'ы print-страниц по `device_id` не меняются — меняется только подпись; поиск/сортировка по id сохранены (`:3032`, `:3064`).
- `get_identity_card` read-only, не трогает link_identities/cleanup-семантику (`[[device-identity-cleanup-not-continuity]]`).
- fleet LEFT JOIN net_devices: у устройства может быть >1 net-строки — JOIN может размножить ряды; добавить в JOIN `AND nd.device_id IS NOT NULL` недостаточно — использовать подзапрос `LEFT JOIN (SELECT device_id, MIN(ip) AS ip FROM net_devices WHERE device_id IS NOT NULL GROUP BY device_id) nd ON nd.device_id = d.device_id` (пин-тест: fleet не дублирует устройства при 2 net-строках — добавить в `tests/test_device_matrix.py`).

### Верификация
Тесты шага 4.7 + полный гейт + `python smoke.py`; глазами: /fleet (нет dev-…, есть IP), /device/{id} (title=id только в tooltip), /print (подписи «Без названия (…)» вместо dev-…).

---

## Ветка 5: `feat/plotly-hover` — контрастная подпись при наведении (З.6)

### Цель
Hover-подписи Plotly на графиках печати наследуют дефолтные цвета и на тёмной теме нечитаемы. Станет: все графики печати получают `layout.hoverlabel` из CSS-переменных темы (тёмный фон/светлый текст в обеих темах), из ОДНОГО сниппета.

### Диагноз (file:line)
- `hoverlabel` в шаблонах отсутствует полностью (grep = 0 совпадений).
- Сайты: `printers.html:165` (1 график), `print.html:316, 319, 429, 436, 452, 482` (4 графика), `printer_detail.html:110` (1 график).
- Паттерн чтения CSS-var уже есть: `printers.html:173` `css.getPropertyValue("--line")`.

### Переиспользование
- Токены темы (`--line` подтверждён; имена фон/текст свериться в `:root` базового CSS — grep `":root"` по `server/web/static`/`base.html`; в сниппете есть литеральные фоллбеки).
- `{% include %}` — как другие `_*.html`-партиалы.

### Шаги

- [ ] **5.1 RED:** новый `tests/test_plotly_hover.py` (client-фикстура как в `test_netmap_web.py`):

```python
import pytest


@pytest.mark.parametrize("path", ["/printers", "/print"])
def test_chart_pages_define_and_use_hoverlabel(client, path):
    body = client.get(path).text
    assert "function srpHoverLabel()" in body            # сниппет подключён
    assert body.count("hoverlabel: srpHoverLabel()") >= 1  # применён к layout'ам
```

(printer_detail проверить в существующем тесте деталей принтера, добавив те же 2 assert'а.) FAIL.

- [ ] **5.2 GREEN:** новый `server/web/templates/_plotly_hover.html`:

```html
<script>
  /* Контрастный hoverlabel для всех Plotly-графиков страницы (обе темы).
     Цвета — из CSS-переменных темы; литералы — фоллбек до загрузки токенов. */
  function srpHoverLabel() {
    var css = getComputedStyle(document.documentElement);
    function v(name, fallback) { return (css.getPropertyValue(name) || fallback).trim(); }
    return {
      bgcolor: v("--panel", "#1b1e24"),
      bordercolor: v("--line", "#3a3f4a"),
      font: { color: v("--text", "#e8eaf0"), size: 13 }
    };
  }
</script>
```

(Имена `--panel`/`--text` заменить на фактические токены из `:root` проекта, `--line` оставить.) В `printers.html`, `print.html`, `printer_detail.html` перед первым chart-скриптом: `{% include "_plotly_hover.html" %}`. В КАЖДЫЙ layout-объект каждого `Plotly.newPlot(...)` этих трёх файлов добавить ключ `hoverlabel: srpHoverLabel(),` (перечень сайтов — grep `Plotly.newPlot` по трём файлам; ожидаемо ~6 layout'ов). Тесты green.

- [ ] **5.3** CHANGELOG `### Исправлено` — «Всплывающие подписи графиков печати стали контрастными в обеих темах (тёмный фон, светлый текст)».

- [ ] **5.4** Гейт → **code-reviewer** → commit (`fix(charts): контрастный hoverlabel из токенов темы, один сниппет на все графики печати`) → merge --no-ff → push. CONTINUITY.md.

### Риски / инварианты
- Никаких новых зависимостей; сниппет — чистый JS-хелпер, данные агента в него не попадают (XSS-поверхности нет, srpEsc не нужен).
- layout-level `hoverlabel` покрывает все trace'ы графика — по-trace дубли не плодить.

### Верификация
`python -m pytest tests/test_plotly_hover.py -q` + полный гейт; глазами: hover на столбцах /print и /printers в светлой и тёмной теме.

---

## Ветка 6: `feat/agent-invisible` — ни одного окна, ни одного мигания (З.7)

### Цель
Сейчас: `run_ps` окно прячет (`ps.py:28-31`), но 4 subprocess-сайта в tray/deploy живут без `creationflags`; иконка трея перерисовывается каждый тик таймера даже без изменений; scheduled task виден в списке планировщика. Станет: каждый запуск процесса в `client/` несёт `creationflags=NO_WINDOW` (пин-тест на AST), трей перерисовывается только при смене состояния, задача планировщика скрыта.

### Диагноз (file:line)
- `client/tray/__main__.py:158` `Popen(self._child("--panel"))` и `:211` `run(self._child("--ask-password"))` — без флагов: при запуске из python.exe (dev/скриптовый деплой) мигает консоль.
- `client/deploy/setup.py:279` `_run()` (robocopy/icacls/schtasks/wevtutil), `:376` (валидационный прогон агента), `:409` (запуск трея) — без флагов: из windowed-инсталлятора каждый вызов мигает консолью.
- `client/tray/icon.py:243-253` `show()` шлёт `Shell_NotifyIconW(NIM_MODIFY)` на КАЖДЫЙ refresh-тик (`__main__.py:216` таймер) — перерисовка без изменений = потенциальное мигание/мерцание tooltip.
- `client/deploy/task_template.xml:36` `<Hidden>false</Hidden>` — задача видна в UI планировщика.
- `NO_WINDOW` уже экспортирован из `ps.py` (ветка 1, шаг 1.3).

### Переиспользование
- `client/collectors/ps.py::NO_WINDOW` — tray импортирует его (пакет тот же, stdlib-only не нарушен).
- `setup.py` — НЕ импортирует collectors (инсталлятор морозится отдельным exe; не тащить пакет ради константы): локальная константа с тем же именем.

### Шаги

- [ ] **6.1 RED:** новый `tests/test_no_window.py`:

```python
"""Пин З.7: каждый subprocess-вызов в client/ обязан прятать окно."""
import ast
import pathlib

CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"
_CALLS = {"run", "Popen", "call", "check_output", "check_call"}


def test_every_subprocess_call_passes_creationflags():
    offenders = []
    for path in sorted(CLIENT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in _CALLS
            ):
                if not any(k.arg == "creationflags" for k in node.keywords):
                    offenders.append(f"{path.relative_to(CLIENT)}:{node.lineno}")
    assert offenders == [], f"subprocess без creationflags=NO_WINDOW: {offenders}"
```

FAIL: 5 сайтов (tray:158,211; setup:279,376,409).

- [ ] **6.2 GREEN:**
  - `client/tray/__main__.py`: импорт `from client.collectors.ps import NO_WINDOW` (вместе с использованием); `:158` → `subprocess.Popen(self._child("--panel"), creationflags=NO_WINDOW)  # nosec B603`; `:211` → `subprocess.run(self._child("--ask-password"), creationflags=NO_WINDOW)  # nosec B603` (диалог пароля — GUI-окно ребёнка, консоль ему не нужна).
  - `client/deploy/setup.py`: рядом с константами (`:46-52`) добавить `_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # инсталлятор standalone: не тащим client.collectors ради константы`; `:279` → `subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)  # nosec B603`; `:376` — добавить `creationflags=_NO_WINDOW` в kwargs; `:409` → `subprocess.Popen([str(destp / TRAY_EXE)], creationflags=_NO_WINDOW)  # nosec B603 -- launch tray in this session`.
  - Тест 6.1 green.

- [ ] **6.3 RED:** в `tests/test_tray_state.py` (или новый test в `tests/test_tray_panel_logic.py` — где уже мокается TrayIcon):

```python
def test_tray_show_skips_unchanged_state(fake_shell):
    """Одинаковые (state, tooltip) не дёргают Shell_NotifyIconW повторно."""
    icon = make_tray_icon(fake_shell)  # существующая фабрика/мок файла тестов трея
    icon.show("ok", "SRP · всё хорошо")
    calls_after_first = fake_shell.call_count
    icon.show("ok", "SRP · всё хорошо")
    assert fake_shell.call_count == calls_after_first  # no-op
    icon.show("warn", "SRP · всё хорошо")
    assert fake_shell.call_count == calls_after_first + 1
```

(Подогнать под фактический мок-паттерн тестов трея; если ctypes там не мокается — тестировать чистую логику: вынести решение в `TrayIcon._should_redraw(state, tooltip) -> bool` и тестировать его.)

- [ ] **6.4 GREEN:** `client/tray/icon.py:243` в начало `show()`:

```python
        key = (state, tooltip[:127])
        if self._added and getattr(self, "_last_shown", None) == key:
            return  # состояние не изменилось -- не дёргать Shell_NotifyIconW (нет мигания)
```

и в конец (после `self._added = True`): `self._last_shown = key`.

- [ ] **6.5** `client/deploy/task_template.xml:36` → `<Hidden>true</Hidden>` (агент не должен светиться в списке задач; управление — только setup/uninstall).

- [ ] **6.6** CHANGELOG `### Исправлено` — «Агент полностью невидим: все запуски процессов скрывают консольное окно (tray-панель, диалог пароля, шаги инсталлятора); иконка трея не перерисовывается без изменений; задача планировщика скрыта».

- [ ] **6.7** Гейт → **security-reviewer** (subprocess/installer поверхность) → commit (`fix(agent): CREATE_NO_WINDOW на всех subprocess-сайтах + memo-перерисовка трея + hidden task`) → merge --no-ff → push. CONTINUITY.md.

### Риски / инварианты
- `CREATE_NO_WINDOW` — stdlib-константа; на не-Windows `getattr(..., 0)` = no-op → тесты на CI зелёные.
- Диалоги (панель, пароль) — собственные GUI-окна детей, они НЕ подавляются флагом (флаг прячет только консоль).
- Hidden-задача остаётся видимой через `schtasks /query` — админ-управляемость сохранена.

### Верификация
`python -m pytest tests/test_no_window.py tests/test_tray_state.py tests/test_setup_logic.py -q` + полный гейт. Живая: открыть панель из трея — без вспышки консоли.

---

## Ветка 7: `feat/ipp-get-jobs` — user-статистика с самого принтера (З.10-P1)

### Цель
Сейчас `server/printers/ipp.py` умеет только `Get-Printer-Attributes` (идентификация). Живой опрос показал: HP отдаёт завершённые задания с `job-originating-user-name` через `Get-Jobs which-jobs=completed`. Станет: опциональный (в коде OFF, в shipped-конфиге ON) серверный сбор завершённых IPP-заданий в отдельную таблицу `printer_ipp_jobs` + блок «Последние задания (IPP)» на странице принтера. Источник ДОПОЛНЯЮЩИЙ (буфер 1–3 задания, поля часто пусты) — в `print_jobs` НЕ пишем, счётчики НЕ фабрикуем.

### Диагноз (file:line)
- `ipp.py:57-72` `build_request` — только операция `_GET_PRINTER_ATTRIBUTES`; `:75-106` `parse_attributes` плющит группы (перезапись ключей) → для мульти-job ответа нужен группный парсер.
- `print_jobs` (`db.py:185-196`) — `device_id NOT NULL` + dedup `(device_id, job_id)`: printer-события туда не ложатся без фиктивного устройства → отдельная таблица.
- `scheduler.py:113-129` `work()` — точка после успешного SNMP-чтения; `probe`/`store` уже инжектируются параметрами → тот же паттерн для jobs.
- `printers/config.py:22-29` — PrinterConfig; `server/config.json:8-16` — shipped-блок.

### Переиспользование
- `_NoRedirect`/`_open`/`_attr`/`_MAX_RESPONSE`/`is_rfc1918` из `ipp.py` — Get-Jobs строится на них (SSRF-гварды бесплатно).
- Инъекционный паттерн `run_poll_cycle(probe=…, store=…)` — jobs-функция передаётся так же (тестируемость без сети).
- `printer_is_confirmed` — jobs собираются только с живых подтверждённых принтеров.

### Шаги

- [ ] **7.1 RED:** в `tests/test_printer_ipp.py` добавить:

```python
def _attr_bytes(tag, name, value):
    import struct
    return bytes([tag]) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value


def test_build_get_jobs_request_shape():
    from server.printers import ipp
    body = ipp.build_get_jobs_request("ipp://192.168.9.8/ipp/print")
    assert body[:2] == b"\x01\x01" and body[2:4] == b"\x00\x0a"  # IPP/1.1, Get-Jobs
    assert b"which-jobs" in body and b"completed" in body
    assert b"job-originating-user-name" in body


def test_parse_job_groups_splits_jobs():
    from server.printers import ipp
    data = (
        b"\x01\x01" + b"\x00\x00" + b"\x00\x00\x00\x01"      # version/status/req-id
        + b"\x02"                                              # job-attributes group #1
        + _attr_bytes(0x21, b"job-id", (7).to_bytes(4, "big"))
        + _attr_bytes(0x42, b"job-name", b"doc-A")
        + _attr_bytes(0x42, b"job-originating-user-name", b"WORKGROUP\\ivanov")
        + b"\x02"                                              # group #2
        + _attr_bytes(0x21, b"job-id", (8).to_bytes(4, "big"))
        + b"\x03"                                              # end-of-attributes
    )
    jobs = ipp.parse_job_groups(data)
    assert [j["job-id"] for j in jobs] == [7, 8]
    assert jobs[0]["job-originating-user-name"].endswith("ivanov")
```

(Тэги значений сверить с `_INT_TAGS`/`_TEXT_TAGS` в шапке `ipp.py` — 0x21 integer, 0x42 nameWithoutLanguage там уже перечислены для `parse_attributes`; если множества другие — использовать входящие в них.) FAIL.

- [ ] **7.2 GREEN ipp:** в `server/printers/ipp.py` (константы рядом с `_GET_PRINTER_ATTRIBUTES`):

```python
_GET_JOBS = 0x000A
_JOB_GROUP_TAG = 0x02


def build_get_jobs_request(printer_uri: str, request_id: int = 1, limit: int = 50) -> bytes:
    """IPP/1.1 Get-Jobs (which-jobs=completed) -- завершённые задания с user-именем."""
    return (
        bytes([0x01, 0x01])
        + struct.pack(">H", _GET_JOBS)
        + struct.pack(">I", request_id)
        + bytes([_OP_ATTRS_TAG])
        + _attr(0x47, b"attributes-charset", b"utf-8")
        + _attr(0x48, b"attributes-natural-language", b"en")
        + _attr(0x45, b"printer-uri", printer_uri.encode())
        + _attr(0x44, b"which-jobs", b"completed")
        + _attr(0x21, b"limit", struct.pack(">i", limit))
        + _attr(0x44, b"requested-attributes", b"job-id")
        + _attr(0x44, b"", b"job-name")
        + _attr(0x44, b"", b"job-originating-user-name")
        + _attr(0x44, b"", b"job-impressions-completed")
        + bytes([_END_TAG])
    )


def parse_job_groups(data: bytes) -> List[Dict[str, object]]:
    """Ответ Get-Jobs -> список словарей, по одному на job-attributes группу (0x02).

    Тот же байтовый декодер, что parse_attributes, но с разрезом по группам --
    parse_attributes плющит группы и теряет все job'ы кроме последнего.
    """
    jobs: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    pos, last_name, n = 8, "", len(data)
    try:
        while pos < n:
            tag = data[pos]
            pos += 1
            if tag == _END_TAG:
                break
            if tag in _GROUP_TAGS:
                cur = {} if tag == _JOB_GROUP_TAG else None
                if cur is not None:
                    jobs.append(cur)
                continue
            (name_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            name = data[pos : pos + name_len].decode("ascii", "replace")
            pos += name_len
            (val_len,) = struct.unpack_from(">H", data, pos)
            pos += 2
            raw = data[pos : pos + val_len]
            pos += val_len
            key = name or last_name
            if name:
                last_name = name
            if cur is None:
                continue
            if tag in _INT_TAGS and len(raw) == 4:
                cur[key] = int.from_bytes(raw, "big", signed=True)
            elif tag in _TEXT_TAGS:
                cur[key] = raw.decode("utf-8", "replace")
    except (struct.error, IndexError):
        pass
    return [j for j in jobs if j]


def get_completed_jobs(ip: str, *, timeout: float = 3.0) -> List[Dict[str, object]]:
    """Завершённые задания принтера: [{job_id, name, user_name, impressions}].

    Дополняющий источник (буфер принтера 1-3 задания; поля часто пусты) --
    UNKNOWN over false confidence: отсутствующее поле -> None, не выдумываем.
    """
    if not is_rfc1918(ip):
        return []
    for path in ("/ipp/print", "/"):
        body = build_get_jobs_request(f"ipp://{ip}{path}")
        req = urllib.request.Request(
            f"http://{ip}:631{path}",
            data=body,
            headers={"Content-Type": "application/ipp"},
            method="POST",
        )
        try:
            with _open(req, timeout) as resp:
                data = resp.read(_MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException):
            continue
        if not data or len(data) > _MAX_RESPONSE:
            continue
        out = []
        for j in parse_job_groups(data):
            jid = j.get("job-id")
            if not isinstance(jid, int):
                continue
            out.append(
                {
                    "job_id": jid,
                    "name": j.get("job-name") if isinstance(j.get("job-name"), str) else None,
                    "user_name": j.get("job-originating-user-name")
                    if isinstance(j.get("job-originating-user-name"), str)
                    else None,
                    "impressions": j.get("job-impressions-completed")
                    if isinstance(j.get("job-impressions-completed"), int)
                    else None,
                }
            )
        if out:
            return out
    return []
```

(`List`/`Optional` — typing-импорты файла уже есть/дополнить вместе с использованием.) Тесты 7.1 green.

- [ ] **7.3 RED БД:** в `tests/test_printer_db.py`:

```python
def test_store_and_get_printer_ipp_jobs_upsert_and_prune(tmp_db):
    from server import db
    jobs = [{"job_id": 7, "name": "doc-A", "user_name": "ivanov", "impressions": 3}]
    db.store_printer_ipp_jobs("prn-1", jobs, received_at="2026-07-02T10:00:00+00:00")
    db.store_printer_ipp_jobs("prn-1", jobs, received_at="2026-07-02T10:15:00+00:00")  # идемпотентно
    got = db.get_printer_ipp_jobs("prn-1")
    assert len(got) == 1 and got[0]["user_name"] == "ivanov"
    many = [{"job_id": i, "name": None, "user_name": None, "impressions": None} for i in range(250)]
    db.store_printer_ipp_jobs("prn-1", many, received_at="2026-07-02T11:00:00+00:00")
    assert db.count_printer_ipp_jobs("prn-1") <= 200  # prune-кап
```

- [ ] **7.4 GREEN БД:** `server/db.py`: в DDL-блок после `printer_readings` (`:235`):

```sql
CREATE TABLE IF NOT EXISTS printer_ipp_jobs (
  printer_id  TEXT NOT NULL,
  job_id      INTEGER NOT NULL,
  name        TEXT,
  user_name   TEXT,
  impressions INTEGER,
  received_at TEXT,
  PRIMARY KEY (printer_id, job_id)
);
```

и функции (рядом с printer-хелперами):

```python
_IPP_JOBS_KEEP = 200  # на принтер; буфер IPP короткий, история дополняющая


def store_printer_ipp_jobs(printer_id: str, jobs: list[dict[str, Any]], *, received_at: str) -> None:
    """Upsert завершённых IPP-заданий (идемпотентно по (printer_id, job_id)) + prune."""
    if not jobs:
        return
    with _lock, _connect() as conn:
        conn.executemany(
            """
            INSERT INTO printer_ipp_jobs (printer_id, job_id, name, user_name, impressions, received_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(printer_id, job_id) DO UPDATE SET
              name = COALESCE(excluded.name, printer_ipp_jobs.name),
              user_name = COALESCE(excluded.user_name, printer_ipp_jobs.user_name),
              impressions = COALESCE(excluded.impressions, printer_ipp_jobs.impressions)
            """,
            [
                (printer_id, j["job_id"], j.get("name"), j.get("user_name"),
                 j.get("impressions"), received_at)
                for j in jobs
                if isinstance(j.get("job_id"), int)
            ],
        )
        conn.execute(
            """
            DELETE FROM printer_ipp_jobs WHERE printer_id = ? AND job_id NOT IN (
              SELECT job_id FROM printer_ipp_jobs WHERE printer_id = ?
              ORDER BY received_at DESC, job_id DESC LIMIT ?
            )
            """,
            (printer_id, printer_id, _IPP_JOBS_KEEP),
        )


def get_printer_ipp_jobs(printer_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT job_id, name, user_name, impressions, received_at
            FROM printer_ipp_jobs WHERE printer_id = ?
            ORDER BY received_at DESC, job_id DESC LIMIT ?
            """,
            (printer_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_printer_ipp_jobs(printer_id: str) -> int:
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM printer_ipp_jobs WHERE printer_id = ?", (printer_id,)
        ).fetchone()[0]
```

- [ ] **7.5 RED scheduler/config:** в `tests/test_printer_scheduler.py`:

```python
def test_poll_cycle_collects_ipp_jobs_only_when_enabled_and_online():
    """ipp_jobs=True + принтер ответил -> jobs_probe вызван и результат сохранён."""
    calls = []
    cfg = PrinterConfig(ipp_jobs=True)
    run_poll_cycle(
        [make_candidate(ip="192.168.9.8")],
        cfg,
        probe=lambda ip, **k: make_reading(ip),          # фабрики из этого же файла
        store=lambda *a, **k: None,
        jobs_probe=lambda ip, **k: [{"job_id": 1, "user_name": "u", "name": None, "impressions": 2}],
        jobs_store=lambda pid, jobs, received_at: calls.append((pid, jobs)),
    )
    assert len(calls) == 1


def test_poll_cycle_skips_ipp_jobs_when_disabled_or_unreachable():
    calls = []
    run_poll_cycle([make_candidate(ip="192.168.9.8")], PrinterConfig(ipp_jobs=False),
                   probe=lambda ip, **k: make_reading(ip), store=lambda *a, **k: None,
                   jobs_probe=lambda ip, **k: calls.append(ip) or [], jobs_store=lambda *a, **k: None)
    run_poll_cycle([make_candidate(ip="192.168.9.9")], PrinterConfig(ipp_jobs=True),
                   probe=lambda ip, **k: None, store=lambda *a, **k: None,
                   jobs_probe=lambda ip, **k: calls.append(ip) or [], jobs_store=lambda *a, **k: None)
    assert calls == []
```

и в `tests/test_config.py` (или где тестируется `load_printer_config`): `load_printer_config({"ipp_jobs": True}).ipp_jobs is True`, дефолт False.

- [ ] **7.6 GREEN scheduler/config:**
  - `server/printers/config.py`: в `PrinterConfig` добавить `ipp_jobs: bool = False  # OFF в коде (secure default); shipped config.json включает`; в `load_printer_config` → `ipp_jobs=d.get("ipp_jobs") is True`.
  - `server/printers/scheduler.py::run_poll_cycle`: добавить kwargs `jobs_probe: Callable[..., list] = ipp.get_completed_jobs` и `jobs_store: StoreFn = db.store_printer_ipp_jobs` (импорт `ipp` вместе с использованием); в `work()` после `store(pid, payload, received_at=stamp)`:

```python
            if reading is not None and printer_cfg.ipp_jobs:
                jobs = jobs_probe(cand.ip)
                if jobs:
                    jobs_store(pid, jobs, received_at=stamp)
```

  - `server/config.json` printers-блок: добавить `"ipp_jobs": true` (правило `[[no-disabled-by-default]]`).

- [ ] **7.7 GREEN страница:** роут деталей принтера (grep `"/printers/"` в `server/web/`) — в контекст `ipp_jobs=db.get_printer_ipp_jobs(printer_id)`; в `printer_detail.html` после блока расходников:

```html
{% if ipp_jobs %}
<h3 class="section-label">Последние задания (IPP)</h3>
<table class="small">
  <tr><th>Получено</th><th>Пользователь</th><th>Документ</th><th>Страниц</th></tr>
  {% for j in ipp_jobs %}
  <tr><td>{{ j.received_at }}</td><td>{{ j.user_name or "—" }}</td>
      <td>{{ j.name or "—" }}</td><td>{{ j.impressions if j.impressions is not none else "—" }}</td></tr>
  {% endfor %}
</table>
<p class="muted small">Дополняющий источник: буфер принтера короткий, основная атрибуция — журнал печати на ПК.</p>
{% endif %}
```

Тест: в тесте деталей принтера (`tests/test_printers_api.py`) — при насеянных ipp-jobs строка «Последние задания (IPP)» и user в HTML.

- [ ] **7.8** CHANGELOG `### Добавлено` — «Опрос завершённых заданий принтера по IPP (Get-Jobs completed): имя пользователя/документ/страницы на странице принтера; дополняющий источник для direct-IP печати мимо агента (printers.ipp_jobs в server/config.json, включено)».

- [ ] **7.9** Гейт → **security-reviewer** (сетевой парсер бинарного протокола + SQL) → commit (`feat(printers): IPP Get-Jobs completed -- user-статистика с самого принтера`) → merge --no-ff → push. CONTINUITY.md.

### Риски / инварианты
- SSRF/privacy: RFC1918-гейт + no-redirect + `_MAX_RESPONSE`-кап — переиспользованы из probe; наружу ничего не уходит.
- UNKNOWN-over-false-confidence: отсутствующие impressions/user → None/«—», в `print_jobs`/счётчики НЕ смешивается (отдельная таблица, отдельная подпись «дополняющий источник»).
- Нагрузка: 1 POST на живой принтер за poll-цикл (900 с), внутри существующего bounded fan-out (`_MAX_WORKERS=16`) и `_poll_lock`.
- Парсер бинарщины: try/except по образцу `parse_attributes`, мусор → пустой список.

### Верификация
`python -m pytest tests/test_printer_ipp.py tests/test_printer_db.py tests/test_printer_scheduler.py tests/test_config.py tests/test_printers_api.py -q` + полный гейт + `python smoke.py`. Живая: `/printers/<id>` HP 192.168.9.8 → блок «Последние задания (IPP)» с user-именем.

---

## Финальный чек всего плана (после ветки 7)

- [ ] `python -m ruff check .` · `python -m mypy` · `python -m bandit -c pyproject.toml -q -r server shared client` · `python -m pytest --cov=server --cov=shared --cov-report=term-missing` (≥80%) · `python smoke.py` — всё green на main.
- [ ] `grep -rn "or d.device_id" server/web/templates` → пусто; `grep -n "COALESCE(d.hostname, p.device_id)" server/db.py` → только сорт/поиск (`:3032`, `:3064`).
- [ ] `server/config.json` содержит: `stale_after_sec: 600`, `printers.ipp_jobs: true`, `printers.static_ips: ["192.168.9.163"]` — ничего OFF-by-default не осталось.
- [ ] CHANGELOG `## [Unreleased]` содержит все строки веток 1–7; CONTINUITY.md отражает завершение треда.
- [ ] Деплой-заметка оператору (в CHANGELOG уже есть): сначала сервер, затем агенты (liveness); до обновления агентов флот помечен offline по новому порогу.
