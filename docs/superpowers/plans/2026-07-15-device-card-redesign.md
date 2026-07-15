# План: редизайн карточки устройства (/device/{id}) — иерархия для инженера

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Исполнитель:** Claude Code **Sonnet · max** на задачу (CLAUDE.md §2 R2: «execute ONE task of an APPROVED plan»). Перед вёрсткой каждой UI-задачи исполнитель обязан invoke skill `frontend-design` (авто-правило пользователя). Финальное ревью всей ветки — обязательное (T5).

**Goal:** Карточка устройства ведёт инженера сверху вниз по важности: железо → вердикт «что с машиной и что делать» → что требует внимания → прогноз сроков → всё остальное в одной раскрывашке. Путаница снята: «Риск-экспозиция» и «Риск траектории» переименованы и понижены, каждое значение подписано по-русски, ползунок деградации заменён линией со статичным пояснением.

**Architecture:** Только Jinja2-шаблоны и тесты — **ноль правок Python-кода сервера** (все данные уже в контексте роута `/device/{id}`, dashboard.py:436-480). device.html становится оркестратором; повторяющиеся оси уходят в макро `_axis_macros.html`; три новых/переработанных партиала: `_device_specs.html` (железо), `_device_hero.html` (вердикт), `_device_diagnostics.html` (раскрывашка). Jinja include/import наследуют контекст вызывающего — паттерн уже подтверждён `_device_hero.html` (комментарий в его шапке).

**Tech Stack:** Jinja2 (autoescape ON), CSS-токены base.html (3 темы: тёмная/meta/vercel), pytest + FastAPI TestClient (фикстура `client` из tests/conftest.py).

## Мотивировка (что не так сейчас — все факты проверены по коду 2026-07-15)

- C40 (cctodo): **две конкурирующие иерархии** — hero D/R/O (Ф7) и рядом старые Day-1-карты. Чип «проекция (D, R, O): 78» (выше=лучше) стоит визуально рядом со scorecard «Риск-экспозиция: 62» (выше=хуже) — две противоположные шкалы без пояснений.
- «Риск-экспозиция» (device.html:189 и :572) и «Риск траектории» (device.html:244) — названия-кальки, вводят в заблуждение. **Ни одна из этих строк не запинена тестами** (grep по tests/ = 0) — переименование свободно.
- «Ползунок» деградации = `.riskbar` с заливкой в hero (_device_hero.html:46-48): выглядит интерактивным, а пояснение (evidence) живёт только в `title=` — видно лишь при наведении мыши.
- Значения без подписей: чип координаты — голое «78»; `axis-conf` — голое «высокая» (а надо «уверенность: высокая»); «Δ7д» — латиница+сокращение.
- Латиница в интерфейсе: «D · R · O», «(D, R, O)».
- Характеристики компа (CPU/RAM/диски/ОС) есть в данных и даже на странице — но в самом низу («Инвентарь», device.html:737-765). Собираются, но не показываются: `cpu_cores/cpu_logical`, `memory_modules`, `os_build`, `os_install_date`, `pending_reboot`, `driver_problem_count` (client/collectors/inventory.py:140-159).
- Теряются молча: `Coordinate.confidence` (у каждой из D/R/O), `health.confidence`, `health.blind_spots` при известном состоянии h0-h4 (рендерятся только в unknown-ветке) — см. server/analytics/health.py:162-188.
- Направления шкал разные и нигде не подписаны: D выше=хуже; R и O выше=лучше (health.py:163).

## Что сознательно НЕ делаем (и почему)

- **Не добавляем новые графики.** История D/R/O по отдельности не извлекается (`_health_index_series` отдаёт только ts+index, dashboard.py:102-112) — потребовался бы Python; а класть D (выше=хуже) и index (выше=лучше) на один график = создать ту же путаницу, которую убираем. Существующие 2 графика (sparkline индекса, латентность p95) остаются.
- **Не трогаем `reason`-строки движков** с литералом «(UNKNOWN — …)» (battery.py:151 и др.): это R4-поверхность скоринга, текст запинен tests/test_dashboard_trust.py:40 как контрактный; UNKNOWN здесь — техтермин класса RSI/BSOD/SMART (CLAUDE.md §5).
- **Не «лечим»** приватный импорт `_STATE_LABELS` в dashboard.py:20 и двойной include `_plotly_hover.html` (повторное объявление одинаковой функции безвредно) — вне задачи, поведение не меняют.
- **Ничего не удаляем из данных**: каждый показатель либо остаётся видимым, либо переезжает в раскрывашку. Единственное настоящее удаление — секция «Инвентарь», всё её содержимое переезжает наверх в «Компьютер» (T3).

## Global Constraints

- Jinja2 autoescape ON; **никакого `|safe`**; JSON только через `|tojson`-острова; агентские строки в JS-синках — только через `window.srpEsc` (в этом плане новых JS-синков нет).
- **Цвета только через CSS-токены** `var(--…)` и `band_class`/`risk_color` — hex в разметке hero запинен тестом (tests/test_device_hero.py:243-256), новые блоки обязаны пройти тот же пин.
- Все 3 темы (тёмная по умолчанию, `[data-theme="meta"]`, `[data-theme="vercel"]`) обязаны выглядеть корректно: новые классы строятся ТОЛЬКО на токенах (--panel, --line, --text, --muted, --good/warn/high/bad/na, --r-card, --shadow-card, --mono) — тогда темы работают автоматически.
- Операторская проза — русская; машинные значения (enum/band/state в API и БД) — английские, **API не трогаем вообще**.
- Файлы < 800 строк (device.html сейчас 848 — план уменьшает до ~620); Python-тесты: line 100, двойные кавычки, py3.9 (`Optional`, без `X | None`).
- Каждое значение на карточке подписано: «уверенность: высокая», «NN из 100», «выше = хуже», «за 7 дней: ▲ 1.2» — никаких голых чисел и голых слов.
- Строки, запиненные другими тестами, НЕ менять: «Покрытие источников», «Идентичность недостоверна», «пропали», «Сертификаты», «Личные сертификаты пользователей», «Действующих личных сертификатов нет», «Комментарий», «обновляется», «обновление доступно», «ошибка обновления», «туннель», id `comment-input`.
- Гейты перед merge (T5): `python -m ruff check .` · `python -m ruff format --check .` · `python -m mypy server shared client` · `python -m bandit …` (рецепты из Makefile) · `python -m pytest -q` cov ≥80% · `python smoke.py`.
- Git: ветка `feat/device-card-redesign` с main; коммит после каждой задачи; конвенциональные сообщения; НИКАКОГО `git add -A` — только явные файлы; `client/config.json`/`org_directory.json` не трогать и не коммитить.

## File Structure

| Файл | Действие | Ответственность |
|---|---|---|
| `server/web/templates/_axis_macros.html` | Create (T1) | макро `axis_card` + `render_axis` — единственное место разметки осей |
| `server/web/templates/device.html` | Modify (T1,T2,T3) | оркестратор: порядок блоков, метаданные осей, правило видимости |
| `server/web/templates/_device_diagnostics.html` | Create (T2) | содержимое раскрывашки «Подробная диагностика» |
| `server/web/templates/_device_specs.html` | Create (T3) | верхний блок «Компьютер» |
| `server/web/templates/_device_hero.html` | Modify (T4) | вердикт «Состояние машины» |
| `tests/test_device_card.py` | Create (T1-T3) | новые пины редизайна |
| `tests/test_device_hero.py` | Modify (T1,T4) | якоря фрагмента + пины hero |
| `CHANGELOG.md` | Modify (T5) | строка в `## [Unreleased]` → `### Changed` |

Целевой порядок блоков device.html (сверху вниз):

```
шапка (имя, id, орг/отдел, агент, контакт, IP/MAC)      — как есть
«Компьютер» (_device_specs)                              — T3, вне {% if s %}
{% if not s %} заглушка {% else %}
  баннеры доверия                                        — как есть
  «Состояние машины» (_device_hero)                      — T4
  «Требует внимания» (оси value ≥ 25)                    — T1
  «Прогноз — ресурс и сроки» (таблица трендов + p95)     — T1
  <details> «Подробная диагностика» (_device_diagnostics)— T1 контейнер, T2 содержимое
{% endif %}
события · сертификаты ×2 · сеть · топология · печать · комментарий — как есть
(«Инвентарь» удалён — переехал в «Компьютер»)            — T3
```

---

### Task 1: Оси диагностики — макро, правило видимости, «Требует внимания», переименование «Риска траектории»

**Files:**
- Create: `server/web/templates/_axis_macros.html`
- Modify: `server/web/templates/device.html` (зона строк 158-534 текущей версии)
- Create: `tests/test_device_card.py`
- Modify: `tests/test_device_hero.py` (только `_hero_fragment` и `test_device_page_hero_precedes_score100_axes`)

**Interfaces:**
- Consumes: контекст роута `/device/{id}` (`s = d.scores`, `s.risk.score100`, `net_subnet_note`), Jinja-globals `risk_color`, локальные set device.html (`conf_ru`, `conf_tip`, `score_tip`).
- Produces: макро `axm.render_axis(key, name, tip, na_tip)` (файл `_axis_macros.html`, импорт `{% import "_axis_macros.html" as axm with context %}`); Jinja-переменная `ns_ax` (namespace: `attention`/`rest` — списки кортежей `(key, name, tip, na_tip)`, `total` int); DOM-якоря `id="attention-label"` и `id="device-diagnostics"` — на них опираются T2 и тесты.

- [ ] **Step 1: Написать падающие тесты** — создать `tests/test_device_card.py`:

```python
"""Пины редизайна карточки устройства (2026-07-15): иерархия для инженера.

Сеялка — тот же минимальный паттерн, что в tests/test_device_hero.py::_seed
(пишем напрямую в хранилище, минуя скоринг-пайплайн).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from server import db

pytestmark = pytest.mark.integration


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(device_id: str, hostname: str, risk: dict) -> None:
    # day1_factors обязателен (страница чейнит его без guard — см. docstring
    # test_device_hero._seed); classes/domains добавлены защитно.
    risk = {
        "day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []},
        "classes": [],
        "domains": {},
        **risk,
    }
    db.touch_device(device_id, _iso_now(), "0.1.0", hostname=hostname)
    db.store_scores(device_id, _iso_now(), {"risk": risk})


def _axis(value, confidence="high", reason="", factors=None):
    return {
        "value": value,
        "confidence": confidence,
        "reason": reason,
        "factors": factors or [],
        "missing_evidence": [],
    }


# --------------------------------------------------------------------------- #
# T1: правило видимости осей
# --------------------------------------------------------------------------- #
def test_axis_over_threshold_visible_before_details(client) -> None:
    _seed("card-1", "CARD-1", {"score100": {"storage_risk": _axis(55.0, reason="ошибки чтения растут")}})
    body = client.get("/device/card-1").text
    axis = body.find("Здоровье диска (SMART)")
    details = body.find('id="device-diagnostics"')
    assert axis != -1 and details != -1
    assert axis < details, "плохая ось (55) должна быть видна сразу, не в раскрывашке"


def test_axis_healthy_hidden_inside_details(client) -> None:
    _seed("card-2", "CARD-2", {"score100": {"storage_risk": _axis(5.0)}})
    body = client.get("/device/card-2").text
    axis = body.find("Здоровье диска (SMART)")
    details = body.find('id="device-diagnostics"')
    assert axis != -1 and details != -1
    assert details < axis, "здоровая ось (5) должна лежать внутри раскрывашки"


def test_axis_unknown_value_hidden_inside_details(client) -> None:
    _seed("card-3", "CARD-3", {"score100": {"network_risk": _axis(None, confidence=None)}})
    body = client.get("/device/card-3").text
    axis = body.find("Здоровье сети")
    details = body.find('id="device-diagnostics"')
    assert details != -1 and axis != -1 and details < axis


def test_axis_confidence_is_labelled(client) -> None:
    _seed("card-4", "CARD-4", {"score100": {"storage_risk": _axis(55.0)}})
    body = client.get("/device/card-4").text
    assert "уверенность: высокая" in body
    assert 'class="axis-conf"' in body


def test_all_clear_line_when_every_axis_healthy(client) -> None:
    _seed("card-5", "CARD-5", {"score100": {"storage_risk": _axis(3.0), "disk_fill_risk": _axis(8.0)}})
    body = client.get("/device/card-5").text
    assert "По рассчитанным проверкам замечаний нет" in body


def test_no_axes_never_claims_all_clear(client) -> None:
    _seed("card-6", "CARD-6", {})  # score100 отсутствует (стар. устройство)
    body = client.get("/device/card-6").text
    assert "Оси диагностики ещё не рассчитаны" in body
    assert "замечаний нет" not in body.lower()


def test_trajectory_axis_renamed_no_english_calque(client) -> None:
    _seed("card-7", "CARD-7", {"score100": {"trajectory_risk": _axis(40.0)}})
    body = client.get("/device/card-7").text
    assert "Риск по трендам" in body
    assert "Риск траектории" not in body
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/test_device_card.py -q`
Expected: 7 failed (нет `id="device-diagnostics"`, нет «уверенность:», нет «Риск по трендам»).

- [ ] **Step 3: Создать `server/web/templates/_axis_macros.html`** (полное содержимое):

```jinja
{# Единая разметка score100-оси. Импорт: {% import "_axis_macros.html" as axm with context %}
   -- "with context" даёт макро доступ к локальным set вызывающего шаблона
   (conf_ru / conf_tip / score_tip из device.html) и к контексту роута
   (s, net_subnet_note). Jinja-globals (risk_color) доступны всегда. #}

{% macro axis_card(ax, name, tip, na_tip, reason="", na_chip_text="НЕИЗВЕСТНО", extra_note=None) %}
<div class="axis-card {{ 'na' if ax.value is none else risk_color(ax.value) }}">
  <div class="axis-head">
    <div>
      <div class="axis-name" title="{{ tip }}">{{ name }}</div>
      <div class="axis-reason">{{ reason }}</div>
    </div>
    {% if ax.value is none %}
      <span class="chip na" title="{{ na_tip }}">{{ na_chip_text }}</span>
    {% else %}
    <div style="text-align:right">
      <div class="axis-val {{ risk_color(ax.value) }}" title="{{ score_tip }}">{{ "%.0f"|format(ax.value) }}</div>
      <div class="axis-conf" title="{{ conf_tip }}">уверенность: {{ conf_ru.get(ax.confidence, ax.confidence or "?") }}</div>
    </div>
    {% endif %}
  </div>
  {% if ax.factors %}<div class="axis-factors">{% for f in ax.factors %}<div class="axis-factor-item">{{ f.label }}</div>{% endfor %}</div>{% endif %}
  {% if extra_note %}
  <div class="axis-blind" title="Деградация видна на нескольких машинах подсети одновременно — причина почти наверняка общая (инфраструктура)">⚠ {{ extra_note }}</div>
  {% endif %}
  {% if ax.value is not none and ax.missing_evidence %}
  <div class="axis-blind">⚠ не видно: {{ ax.missing_evidence | join("; ") }}</div>
  {% endif %}
</div>
{% endmacro %}

{% macro render_axis(key, name, tip, na_tip) %}
  {% set ax = (s.risk.score100 or {}).get(key) %}
  {% if ax %}
    {% if key == "battery_risk" %}
      {% set no_bat = (ax.value is none and ax.source_lineage is defined and ax.source_lineage and ax.source_lineage.battery_present == false) %}
      {{ axis_card(ax, name, tip,
                   ("Устройство без батареи" if no_bat else na_tip),
                   reason=("нет батареи (десктоп)" if no_bat else ((ax.reason or "нет данных о батарее") if ax.value is none else (ax.reason or ""))),
                   na_chip_text=("—" if no_bat else "НЕИЗВЕСТНО")) }}
    {% elif key == "network_risk" %}
      {{ axis_card(ax, name, tip, na_tip, reason=(ax.reason or ""), extra_note=net_subnet_note) }}
    {% elif key == "trajectory_risk" %}
      {{ axis_card(ax, name, tip, na_tip, reason=(ax.reason or "совокупный риск выхода за пределы")) }}
    {% else %}
      {{ axis_card(ax, name, tip, na_tip, reason=(ax.reason or "")) }}
    {% endif %}
  {% endif %}
{% endmacro %}
```

- [ ] **Step 4: Переписать зону осей в device.html.**

4a. Сразу после строки `{% set level_ru = … %}` (строка 15) добавить импорт и метаданные осей:

```jinja
{% import "_axis_macros.html" as axm with context %}
{# Метаданные всех score100-осей: (ключ, название, tip, na_tip).
   Tip-тексты перенесены 1:1 со старых axis-card — не менять формулировки. #}
{% set axes_meta = [
  ("storage_risk", "Здоровье диска (SMART)",
   "Риск отказа диска на основе данных SMART: переназначенные секторы, ошибки чтения/записи, температура, износ SSD. 0–100, выше = хуже",
   "Нет данных SMART или диск не поддерживает мониторинг"),
  ("battery_risk", "Здоровье батареи",
   "Износ батареи: отношение реальной ёмкости к заводской. 0–100, выше = хуже. Внимание: вздутие не определяется по ёмкости",
   "Нет данных о батарее или батарея не определена"),
  ("os_degradation_risk", "Стабильность ОС",
   "Нестабильность ОС: RSI Windows (индекс стабильности 1–10, ниже = хуже), синие экраны, неожиданные выключения, время загрузки. 0–100, выше = хуже",
   "Нет данных RSI и счётчиков сбоев — не можем оценить"),
  ("disk_fill_risk", "Заполнение диска / обслуживание Windows",
   "Риск нехватки места на диске и сбоя обслуживания Windows (обновления, патчи). Основан на медиане свободного места за 14 дней и ошибках обновлений. 0–100, выше = хуже",
   "Нет данных о свободном месте"),
  ("network_risk", "Здоровье сети",
   "Состояние сети машины: потери и задержка до шлюза, адрес без DHCP (APIPA), слабый Wi-Fi. 0–100, выше = хуже. Блокировка ICMP честно показывается как «не видно», а не как авария",
   "Нет сетевой телеметрии или источник не прошёл проверку доверия"),
  ("fleet_anomaly_risk", "Аномалии флота",
   "Сравнение с другими устройствами того же типа или объекта. Выявляет массовые проблемы: плохой патч ОС, нестабильное питание в здании. 0–100, выше = хуже",
   "Менее 2 устройств того же типа — сравнение невозможно"),
  ("trajectory_risk", "Риск по трендам",
   "Насколько быстро показатели движутся к допустимым пределам: износ SSD, батарея, заполнение диска, время загрузки. 0–100, выше = хуже",
   "Недостаточно точек данных для расчёта тренда (нужно минимум 3)"),
] %}
```

4b. Сразу после `{% include "_device_hero.html" %}` (строка 156) вставить правило видимости, секцию «Требует внимания» и «Прогноз» (порог 25 = граница «оранжевого» существующей шкалы `risk_color`, device.html:14):

```jinja
{# ── Правило видимости: ось «горит» (value ≥ 25) → видна сразу; норма/нет
   данных → в раскрывашке «Подробная диагностика». Порог 25 = граница warn
   существующей шкалы risk_color — карточка и правило не расходятся. #}
{% set s100 = s.risk.score100 or {} %}
{% set ns_ax = namespace(attention=[], rest=[], total=0) %}
{% for key, name, tip, na_tip in axes_meta %}
  {% set ax = s100.get(key) %}
  {% if ax %}
    {% set ns_ax.total = ns_ax.total + 1 %}
    {% if ax.value is not none and ax.value >= 25 %}
      {% set _ = ns_ax.attention.append((key, name, tip, na_tip)) %}
    {% else %}
      {% set _ = ns_ax.rest.append((key, name, tip, na_tip)) %}
    {% endif %}
  {% endif %}
{% endfor %}

{# ── ТРЕБУЕТ ВНИМАНИЯ ──────────────────────────────────────────────────── #}
<div class="section-label incident" id="attention-label" style="margin-top:24px">Требует внимания</div>
{% if ns_ax.total == 0 %}
<p class="muted small">Оси диагностики ещё не рассчитаны.</p>
{% elif ns_ax.attention %}
{% for key, name, tip, na_tip in ns_ax.attention %}{{ axm.render_axis(key, name, tip, na_tip) }}{% endfor %}
{% else %}
<p class="small" style="color:var(--good)">✓ По рассчитанным проверкам замечаний нет.</p>
{% endif %}
```

4c. Секцию «Прогноз» перестроить: заголовок `Прогноз — траектории и ресурс` → `Прогноз — ресурс и сроки`; **удалить** axis-card «Риск траектории» (старые строки 238-256, блок `{% if traj_ax %}…</div>` с его `axis-card`); таблицу трендов оставить,但 развернуть её из-под `{% if traj_ax %}`: она должна рендериться от `s.risk.trajectory` независимо. Итоговый вид зоны:

```jinja
{# ════════════════════════════════════════════════════════════════════════
   ПРОГНОЗ — ресурс и сроки
   ════════════════════════════════════════════════════════════════════════ #}
<div class="section-label predictive" style="margin-top:28px">Прогноз — ресурс и сроки</div>

{% set tr = s.risk.trajectory or {} %}
{% if tr %}
{# … сюда 1:1 переезжают существующие dir_chip / dir_label / traj_tips / tmeta
   и цикл .traj-table (старые строки 260-321) — БЕЗ изменений содержимого … #}
{% else %}
<p class="muted small">Трендов пока нет — мало точек данных.</p>
{% endif %}
```

График «Латентность диска (p95)» (`{% if heartbeat_rollups %}`, старые строки 324-382) остаётся на месте, сразу после таблицы трендов — не трогать.

4d. После графика p95 — контейнер раскрывашки (в T1 внутри только оси-«норма»; T2 доложит остальное). **Все старые axis-card-блоки (Storage/Battery/OS/Disk fill/Network/Fleet anomaly, старые строки 384-534) и заголовок «Текущее состояние» (строки 435-438) удалить** — их заменяет макро:

```jinja
{# ── ПОДРОБНАЯ ДИАГНОСТИКА (свёрнута по умолчанию) ─────────────────────── #}
<details id="device-diagnostics" style="margin-top:26px">
  <summary>Подробная диагностика{% if ns_ax.rest %} · проверок без замечаний: {{ ns_ax.rest|length }}{% endif %}</summary>
  <div style="margin-top:12px">
  {% for key, name, tip, na_tip in ns_ax.rest %}{{ axm.render_axis(key, name, tip, na_tip) }}{% endfor %}
  </div>
</details>
```

Блоки Day-1 scorecards (160-194), «Покрытие источников» (196-231), «Классы отказа» (536-563) и «Диагностика — что повлияло…» (565-591) в T1 **не трогать** — они остаются на своих местах между hero и «Требует внимания»? Нет: они остаются там, где были (после details-контейнера их переносит T2). В T1 просто не менять их разметку; допустимо, что они временно живут между «Подробной диагностикой» и «Недавними событиями».

- [ ] **Step 5: Обновить якоря в tests/test_device_hero.py.**

`_hero_fragment` (строки 148-160): заменить `end = body.index("Оси score100")` на `end = body.index('id="attention-label"')` и поправить docstring (hero теперь заканчивается на секции «Требует внимания»). `test_device_page_hero_precedes_score100_axes` (287-294) переименовать в `test_device_page_hero_precedes_attention_and_details` и заменить тело:

```python
def test_device_page_hero_precedes_attention_and_details(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    assert devices
    html = seeded_client.get(f"/device/{devices[0]['device_id']}").text
    hero = html.find('id="device-hero"')
    attention = html.find('id="attention-label"')
    details = html.find('id="device-diagnostics"')
    assert hero != -1 and attention != -1 and details != -1
    assert hero < attention < details, "порядок: вердикт → требует внимания → раскрывашка"
```

- [ ] **Step 6: Прогнать тесты задачи**

Run: `python -m pytest tests/test_device_card.py tests/test_device_hero.py tests/test_dashboard_trust.py tests/test_trust_capability.py -q`
Expected: PASS (все). Если `jinja2.exceptions.UndefinedError` на `conf_ru` внутри макро — проверить, что импорт написан именно `with context`.

- [ ] **Step 7: Commit**

```bash
git add server/web/templates/_axis_macros.html server/web/templates/device.html tests/test_device_card.py tests/test_device_hero.py
git commit -m "feat(dashboard): оси карточки устройства -- макро, правило видимости, «Риск по трендам»"
```

---

### Task 2: Перенос старой иерархии в «Подробную диагностику», переименование «Риск-экспозиции»

**Files:**
- Create: `server/web/templates/_device_diagnostics.html`
- Modify: `server/web/templates/device.html`
- Modify: `tests/test_device_card.py` (добавить тесты)

**Interfaces:**
- Consumes: `ns_ax`, `axm` из T1 (контекст include наследуется — партиал видит переменные device.html); `s.risk.domains`, `s.risk.classes`, `s.risk.day1_factors`, `trust_full`, `conf_ru`, Jinja-globals `health_color`, `risk_color`, `level_color`, `pct`.
- Produces: партиал `_device_diagnostics.html`; device.html падает ниже 800 строк.

- [ ] **Step 1: Написать падающие тесты** — добавить в `tests/test_device_card.py`:

```python
# --------------------------------------------------------------------------- #
# T2: старая иерархия внутри раскрывашки, «Риск-экспозиция» переименована
# --------------------------------------------------------------------------- #
def test_risk_exposure_renamed_everywhere(client) -> None:
    _seed("card-8", "CARD-8", {})
    body = client.get("/device/card-8").text
    assert "Риск-экспозиция" not in body
    assert "Суммарный риск сбоя" in body


def test_day1_scorecards_inside_details(client) -> None:
    _seed("card-9", "CARD-9", {})
    body = client.get("/device/card-9").text
    details = body.find('id="device-diagnostics"')
    day1 = body.find("Производительность")
    assert details != -1 and day1 != -1
    assert details < day1, "старые сводные баллы должны лежать внутри раскрывашки"


def test_coverage_widget_moved_but_string_preserved(client) -> None:
    """«Покрытие источников» запинено test_dashboard_trust.py:41 — строка обязана
    остаться в DOM (внутри раскрывашки)."""
    _seed(
        "card-10",
        "CARD-10",
        {"domains": {"smart": {"state": "trusted", "weight": 1.0}}, "classes": []},
    )
    body = client.get("/device/card-10").text
    details = body.find('id="device-diagnostics"')
    cov = body.find("Покрытие источников")
    assert details != -1 and cov != -1 and details < cov


def test_failure_classes_inside_details(client) -> None:
    _seed(
        "card-11",
        "CARD-11",
        {"classes": [{"label": "деградация накопителя", "trust": "unknown", "level": "low",
                      "probability": 0.1, "factors": []}]},
    )
    body = client.get("/device/card-11").text
    details = body.find('id="device-diagnostics"')
    cls = body.find("Классы отказа")
    assert details != -1 and cls != -1 and details < cls
```

Примечание сеялке: `s.risk.classes` рендерится циклом без guard (device.html:539) — в `_seed` T1 blob его нет, но цикл по отсутствующему ключу в Jinja упадёт с Undefined только при обращении; если базовый `_seed` даёт 500 на этих тестах — добавить в `_seed` дефолт `"classes": []` и `"domains": {}` рядом с `day1_factors` (это уже фактическое требование страницы, зафиксированное в docstring test_device_hero.py::_seed).

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `python -m pytest tests/test_device_card.py -q`
Expected: 4 новых FAIL («Риск-экспозиция» ещё в body; details > day1 по позиции).

- [ ] **Step 3: Создать `server/web/templates/_device_diagnostics.html`** — содержимое = ПЕРЕНОС 1:1 четырёх блоков из device.html (в этом порядке), с двумя точечными правками текста:

```jinja
{# Содержимое раскрывашки «Подробная диагностика». Включается device.html'ом
   ВНУТРИ <details id="device-diagnostics">. Контекст наследуется от
   device.html (s, ns_ax, axm, словари) -- тот же паттерн, что _device_hero. #}

{# 1. Оси без замечаний (T1) #}
{% for key, name, tip, na_tip in ns_ax.rest %}{{ axm.render_axis(key, name, tip, na_tip) }}{% endfor %}

{# 2. Старые сводные баллы Day-1 -- перенос строк 160-194 device.html.
   ЕДИНСТВЕННЫЕ правки при переносе:
   - подзаголовок секции;
   - "Риск-экспозиция" -> "Суммарный риск сбоя";
   - подпись под значением "выше = хуже" -> "старая шкала · выше = хуже". #}
<div class="section-label" style="margin-top:18px"
     title="Четыре сводных балла старой модели. Основной вердикт теперь выше — «Состояние машины»; эти баллы оставлены для сверки и истории">Сводные баллы (старая модель)</div>
{# … сюда 1:1 старые строки 161-193 (scoregrid из 4 scorecard) с правками:
   строка 189: <div class="lab">Суммарный риск сбоя</div>
   строка 192: <div class="lab" style="margin-top:6px">старая шкала · выше = хуже</div> … #}

{# 3. Покрытие источников -- перенос строк 196-231 device.html БЕЗ изменений
   (строка "Покрытие источников" запинена тестом trust). #}

{# 4. Классы отказа -- перенос строк 536-563 device.html БЕЗ изменений. #}

{# 5. Факторы базовых скоров -- перенос строк 565-591 device.html; в списке
   d1groups заменить кортеж ("Риск-экспозиция", ...) на
   ("Суммарный риск сбоя", s.risk.day1_factors.risk_exposure). #}
```

(Исполнителю: «перенос 1:1» = вырезать указанные строки из device.html и вставить сюда без переформатирования; менять только 4 перечисленные строки. Machine-ключ `risk_exposure` в данных/БД НЕ переименовывать.)

- [ ] **Step 4: В device.html** внутри `<details id="device-diagnostics">` заменить цикл `ns_ax.rest` на include партиала:

```jinja
<details id="device-diagnostics" style="margin-top:26px">
  <summary>Подробная диагностика{% if ns_ax.rest %} · проверок без замечаний: {{ ns_ax.rest|length }}{% endif %}</summary>
  <div style="margin-top:12px">
  {% include "_device_diagnostics.html" %}
  </div>
</details>
```

и **вырезать** из device.html четыре перенесённых блока (Day-1 scoregrid, покрытие, классы, факторы — их разметка теперь живёт в партиале, включая заголовок «Диагностика — что повлияло на базовые скоры» внутри блока факторов). Отдельно **удалить совсем** заголовок `Оси score100 — детализация` (бывш. строка 159 — старый якорь `_hero_fragment`, в T1 уже заменённый на `id="attention-label"`; его роль теперь играет summary раскрывашки). `{% endif %}{# end if s %}` (бывш. строка 592) должен остаться сразу после `</details>`.

- [ ] **Step 5: Прогнать тесты**

Run: `python -m pytest tests/test_device_card.py tests/test_device_hero.py tests/test_dashboard_trust.py -q`
Expected: PASS. Дополнительно проверить размер: `(Get-Content server/web/templates/device.html | Measure-Object -Line).Lines` — ожидается ~600-650 (< 800).

- [ ] **Step 6: Commit**

```bash
git add server/web/templates/_device_diagnostics.html server/web/templates/device.html tests/test_device_card.py
git commit -m "feat(dashboard): старые сводки карточки -- в «Подробную диагностику», «Суммарный риск сбоя»"
```

---

### Task 3: Блок «Компьютер» вверху карточки, снос «Инвентаря»

**Files:**
- Create: `server/web/templates/_device_specs.html`
- Modify: `server/web/templates/device.html`
- Modify: `tests/test_device_card.py`

**Interfaces:**
- Consumes: `d.inventory` (ключи снапшота: `cpu_name, cpu_cores, cpu_logical, total_ram_gb, memory_modules[], disks[].model/media_type/size_gb, os_caption, os_version, os_build, os_install_date, bios_version, bios_release_date, pending_reboot, driver_problem_count` — client/collectors/inventory.py:140-159), `d.latest_heartbeat` (`cpu_pct, mem_avail_mb, free_space_pct, uptime_hours`), `d.first_seen`, `d.last_seen`.
- Produces: партиал `_device_specs.html`, CSS-классы `.spec-grid/.spec-item/.spec-l/.spec-v/.spec-now/.spec-flags` в `{% block extra_head %}` device.html.

- [ ] **Step 1: Написать падающие тесты** — добавить в `tests/test_device_card.py`:

```python
# --------------------------------------------------------------------------- #
# T3: блок «Компьютер» вверху, «Инвентарь» снесён
# --------------------------------------------------------------------------- #
_INV = {
    "hostname": "CARD-PC",
    "os_caption": "Microsoft Windows 10 Pro",
    "os_version": "10.0.19045",
    "os_build": "19045",
    "cpu_name": "Intel(R) Core(TM) i5-10400",
    "cpu_cores": 6,
    "cpu_logical": 12,
    "total_ram_gb": 16,
    "memory_modules": [{"capacity_gb": 8, "speed_mhz": 2666, "manufacturer": "Kingston"}],
    "disks": [{"model": "Samsung SSD 870 EVO", "media_type": "SSD", "size_gb": 500}],
    "bios_version": "1.5.0",
    "bios_release_date": "2022-09-01",
    "pending_reboot": True,
    "driver_problem_count": 2,
}


def _seed_with_inventory(device_id: str, hostname: str) -> None:
    _seed(device_id, hostname, {})
    db.store_inventory(device_id, _iso_now(), _INV)


def test_specs_block_labelled_and_on_top(client) -> None:
    _seed_with_inventory("card-12", "CARD-12")
    body = client.get("/device/card-12").text
    for label in ("Процессор", "Память", "Диски", "Система"):
        assert label in body, f"подпись «{label}» обязана быть в блоке «Компьютер»"
    assert "Intel(R) Core(TM) i5-10400" in body
    assert "ядер: 6" in body
    assert "Samsung SSD 870 EVO" in body
    # блок «Компьютер» идёт раньше вердикта
    assert body.find("Процессор") < body.find('id="device-hero"')


def test_specs_flags_pending_reboot_and_drivers(client) -> None:
    _seed_with_inventory("card-13", "CARD-13")
    body = client.get("/device/card-13").text
    assert "требуется перезагрузка" in body
    assert "проблемных драйверов: 2" in body


def test_old_inventory_section_gone(client) -> None:
    _seed_with_inventory("card-14", "CARD-14")
    body = client.get("/device/card-14").text
    assert ">Инвентарь<" not in body
    # но содержимое не потеряно: BIOS и период активности живут в раскрывашке блока
    assert "BIOS" in body
    assert "Период активности" in body


def test_specs_fallback_when_no_inventory(client) -> None:
    _seed("card-15", "CARD-15", {})
    body = client.get("/device/card-15").text
    assert "Характеристики ещё не получены от агента" in body
```

- [ ] **Step 2: Убедиться, что падают**

Run: `python -m pytest tests/test_device_card.py -q` → новые 4 FAIL.

- [ ] **Step 3: Создать `server/web/templates/_device_specs.html`** (полное содержимое):

```jinja
{# «Компьютер» -- краткие характеристики железа, первый блок карточки
   (владелец 2026-07-15: инженер сначала видит, ЧТО за машина).
   Данные: d.inventory (снапшот) + d.latest_heartbeat (живые значения).
   Рендерится и без скорингов (вне if s). #}
<div class="section-label" style="margin-top:18px">Компьютер</div>
{% set inv = d.inventory %}
{% set hb = d.latest_heartbeat %}
{% if inv %}
<div class="spec-grid">
  <div class="spec-item">
    <div class="spec-l">Процессор</div>
    <div class="spec-v">{{ inv.cpu_name or "—" }}{% if inv.cpu_cores %} · ядер: {{ inv.cpu_cores }}{% if inv.cpu_logical %} / потоков: {{ inv.cpu_logical }}{% endif %}{% endif %}</div>
    {% if hb and hb.cpu_pct is not none %}<div class="spec-now">загрузка сейчас: {{ "%.0f"|format(hb.cpu_pct) }}%</div>{% endif %}
  </div>
  <div class="spec-item">
    <div class="spec-l">Память</div>
    <div class="spec-v">{{ (inv.total_ram_gb ~ " ГБ") if inv.total_ram_gb is not none else "—" }}</div>
    {% if hb and hb.mem_avail_mb is not none %}<div class="spec-now">свободно сейчас: {{ hb.mem_avail_mb }} МБ</div>{% endif %}
  </div>
  <div class="spec-item">
    <div class="spec-l">Диски</div>
    <div class="spec-v">
      {% for disk in inv.disks or [] %}
      <div>{{ disk.model or "?" }} · {{ disk.media_type or "?" }} · {{ (disk.size_gb ~ " ГБ") if disk.size_gb is not none else "объём неизвестен" }}</div>
      {% else %}—{% endfor %}
    </div>
    {% if hb and hb.free_space_pct is not none %}<div class="spec-now">свободно на системном: {{ "%.0f"|format(hb.free_space_pct) }}%</div>{% endif %}
  </div>
  <div class="spec-item">
    <div class="spec-l">Система</div>
    <div class="spec-v">{{ inv.os_caption or "—" }}{% if inv.os_version %} · {{ inv.os_version }}{% endif %}</div>
    {% if hb and hb.uptime_hours is not none %}<div class="spec-now">аптайм: {{ "%.0f"|format(hb.uptime_hours) }} ч</div>{% endif %}
  </div>
</div>
{% if inv.pending_reboot or (inv.driver_problem_count or 0) > 0 %}
<div class="spec-flags">
  {% if inv.pending_reboot %}<span class="chip warn" title="Windows ожидает перезагрузку для завершения обновлений">требуется перезагрузка</span>{% endif %}
  {% if (inv.driver_problem_count or 0) > 0 %}<span class="chip warn" title="Устройства с ошибками драйверов в диспетчере устройств">проблемных драйверов: {{ inv.driver_problem_count }}</span>{% endif %}
</div>
{% endif %}
<details style="margin-top:8px">
  <summary>Прочее железо</summary>
  <table style="max-width:700px;margin-top:8px">
    <tbody>
      <tr><td class="muted small" style="width:180px">BIOS</td><td class="small mono">{{ inv.bios_version or "—" }} ({{ inv.bios_release_date or "?" }})</td></tr>
      {% if inv.os_build %}<tr><td class="muted small">Сборка ОС</td><td class="small mono">{{ inv.os_build }}</td></tr>{% endif %}
      {% if inv.os_install_date %}<tr><td class="muted small">ОС установлена</td><td class="small mono">{{ inv.os_install_date }}</td></tr>{% endif %}
      {% if inv.memory_modules %}
      <tr><td class="muted small">Модули памяти</td><td class="small">
        {% for m in inv.memory_modules %}<div>{{ (m.capacity_gb ~ " ГБ") if m.capacity_gb is not none else "?" }}{% if m.speed_mhz %} · {{ m.speed_mhz }} МГц{% endif %}{% if m.manufacturer %} · {{ m.manufacturer }}{% endif %}</div>{% endfor %}
      </td></tr>
      {% endif %}
      <tr><td class="muted small">Период активности</td><td class="small mono">{{ d.first_seen or "—" }} → {{ d.last_seen or "—" }}</td></tr>
    </tbody>
  </table>
</details>
{% else %}
<p class="muted small">Характеристики ещё не получены от агента.</p>
{% endif %}
```

- [ ] **Step 4: В device.html:**

4a. Вставить `{% include "_device_specs.html" %}` сразу после закрывающего `</div>` блока `upd_parts` (бывш. строка 139), ДО `{% if not s %}` — блок виден даже без скорингов.

4b. **Удалить целиком** секцию «Инвентарь» (якорь: `<div class="section-label">Инвентарь</div>` … до закрывающего `</table>` перед `{# ── Print summary … #}`). Строки `{% set inv = d.inventory %}` / `{% set hb = d.latest_heartbeat %}` из неё тоже удалить (партиал ставит свои).

4c. В `{% block extra_head %}` (после стилей `.traj-*`) добавить CSS — только токены, без hex:

```css
/* ── Спец-полоса «Компьютер» ───────────────────────────────────────────── */
.spec-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 10px; margin-top: 4px;
}
.spec-item {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--r-card); padding: 11px 14px;
  box-shadow: var(--shadow-card);
}
.spec-l { font: 600 11px/1 var(--mono); letter-spacing: 1.2px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
.spec-v { font-size: 14px; line-height: 1.45; }
.spec-now { font: 12px/1.4 var(--mono); color: var(--muted); margin-top: 5px; }
.spec-flags { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
```

- [ ] **Step 5: Прогнать тесты**

Run: `python -m pytest tests/test_device_card.py tests/test_org_directory.py tests/test_ingest.py -q`
Expected: PASS (org/ingest пины шапки не задеты).

- [ ] **Step 6: Commit**

```bash
git add server/web/templates/_device_specs.html server/web/templates/device.html tests/test_device_card.py
git commit -m "feat(dashboard): блок «Компьютер» вверху карточки, снос дублирующего «Инвентаря»"
```

---

### Task 4: Вердикт «Состояние машины» — переработка hero

**Files:**
- Modify: `server/web/templates/_device_hero.html` (вся разметка внутри `#device-hero`, JS sparkline не трогать)
- Modify: `server/web/templates/device.html` (2 строки: словарь `conf_ru` + заголовок секции)
- Modify: `tests/test_device_hero.py`

**Interfaces:**
- Consumes: `health` (поля: `state, state_label, state_evidence, band, index, confidence, dominant, dominant_label, horizon_days, horizon_reason, delta_7d, blind_spots, damage/resilience/observability{value, band, confidence, evidence}` — health.py:162-188 + delta_7d из pipeline.py:697), `state_labels`, globals `band_class`, `action_for`; `conf_ru` из device.html.
- Produces: CSS-классы `.hero-verdict/.hero-kv-l/.hero-state/.hero-action/.hero-why/.coord-card/.coord-sub/.coord-line/.coord-why/.coord-conf` (в `<style>` hero-партиала, как сейчас).

- [ ] **Step 1: Обновить/дополнить тесты `tests/test_device_hero.py`** (RED):

1a. `test_hero_index_caption_present` (173-178) заменить тело:

```python
def test_hero_index_caption_present(client) -> None:
    _seed("hero-2", "HERO-2", {"health": _real_health(index=78.0)})
    body = client.get("/device/hero-2").text
    frag = _hero_fragment(body)
    assert "сводный индекс" in frag
    assert "78 из 100" in frag
    assert "(D, R, O)" not in frag  # латиница ушла (владелец 2026-07-15)
```

1b. `test_hero_delta_7d_none_renders_neutral_dash_not_broken` (271-276): заменить `assert "Δ7д" in frag` на `assert "за 7 дней" in frag`.

1c. Добавить новые тесты (в конец файла):

```python
# --------------------------------------------------------------------------- #
# Редизайн 2026-07-15: вердикт, линия вместо ползунка, подписи
# --------------------------------------------------------------------------- #
def test_hero_verdict_line_prominent(client) -> None:
    _seed("hero-14", "HERO-14", {"health": _real_health()})
    frag = _hero_fragment(client.get("/device/hero-14").text)
    assert "состояние" in frag
    assert "ранняя деградация" in frag          # state_label
    assert "что делать" in frag
    assert "снять образ данных" in frag         # action_for("storage")
    assert "главный фактор" in frag
    assert "накопитель" in frag                 # dominant_label


def test_hero_coordinate_evidence_is_static_text_not_tooltip(client) -> None:
    ev = [{"label": "переназначенные секторы: 12"}]
    _seed(
        "hero-15",
        "HERO-15",
        {"health": _real_health(damage={"value": 40.0, "band": "watch",
                                        "confidence": "medium", "evidence": ev})},
    )
    frag = _hero_fragment(client.get("/device/hero-15").text)
    assert "почему: переназначенные секторы: 12" in frag       # статичный абзац
    assert 'title="переназначенные секторы: 12"' not in frag   # НЕ hover-подсказка
    assert 'class="coord-line"' in frag                        # линия, не ползунок
    assert 'class="riskbar' not in frag                        # старой заливки нет


def test_hero_coordinate_values_and_confidence_labelled(client) -> None:
    _seed("hero-16", "HERO-16", {"health": _real_health()})
    frag = _hero_fragment(client.get("/device/hero-16").text)
    assert "20 из 100" in frag                 # damage value подписан
    assert "уверенность: высокая" in frag      # Coordinate.confidence всплыла
    assert "выше = хуже" in frag               # направление шкалы Повреждений
    assert "выше = лучше" in frag              # направление Устойчивости/Наблюдаемости


def test_hero_blind_spots_surface_in_known_state(client) -> None:
    """До редизайна blind_spots молча терялись, если state известен."""
    _seed(
        "hero-17",
        "HERO-17",
        {"health": _real_health(blind_spots=["нет анализа событий"])},
    )
    frag = _hero_fragment(client.get("/device/hero-17").text)
    assert "не видно: нет анализа событий" in frag


def test_hero_unknown_branch_offers_action(client) -> None:
    _seed("hero-18", "HERO-18", {"health": _blind_health()})
    frag = _hero_fragment(client.get("/device/hero-18").text)
    assert "что делать" in frag
    assert "восстановить видимость" in frag


def test_hero_section_has_no_latin_dro(client) -> None:
    _seed("hero-19", "HERO-19", {"health": _real_health()})
    body = client.get("/device/hero-19").text
    assert "Состояние машины" in body
    assert "D · R · O" not in body
```

- [ ] **Step 2: Убедиться, что падают**

Run: `python -m pytest tests/test_device_hero.py -q` → новые FAIL + 1a/1b FAIL.

- [ ] **Step 3: Переписать `_device_hero.html`.** JS sparkline (строки 84-156) и его остров НЕ менять, кроме подписи «индекс здоровья, динамика» → «сводный индекс, динамика». Разметку выше — заменить на:

```jinja
{# Вердикт «Состояние машины»: сначала словами что с машиной и что делать,
   потом лестница состояний, потом три координаты (линия-шкала + статичное
   «почему» вместо hover-тултипа -- владелец 2026-07-15), потом чипы с
   подписанными значениями и sparkline. Контекст наследуется от device.html. #}
<div class="section-label" style="margin-top:16px">Состояние машины</div>
<div id="device-hero" class="hero-health">
  {% if health is none %}
  <p class="muted small" style="padding:6px 0">нет данных о координатах здоровья</p>
  {% elif health.state == "unknown" %}
  {% set obs = health.observability or {} %}
  {% set blind_ev = (health.blind_spots or [])|join("; ") %}
  <div class="alert-banner alert-warn">
    ⚠ данных недостаточно — видимость {{ "%.0f"|format(obs.value) if obs.value is not none else "?" }}%
  </div>
  {% if blind_ev %}
  <div class="axis-blind">⚠ не видно: {{ blind_ev }}</div>
  {% endif %}
  <p class="hero-action"><span class="hero-kv-l">что делать</span> {{ action_for(health.dominant) }}</p>
  {% else %}
  {% if health_stale_msg %}
  <div class="alert-banner alert-warn">⚠ {{ health_stale_msg }}</div>
  {% endif %}

  {# 1. Вердикт словами -- самое важное, крупно, контрастно #}
  <div class="hero-verdict">
    <div>
      <span class="hero-kv-l">состояние</span>
      <span class="hero-state {{ band_class(health.band) }}">{{ health.state_label }}</span>
    </div>
    <p class="hero-action">
      <span class="hero-kv-l">главный фактор</span> {{ health.dominant_label }}
      · <span class="hero-kv-l">что делать</span> {{ action_for(health.dominant) }}
    </p>
  </div>

  {# 2. Лестница h0..h4 (шаги дискретны -- остаётся лестницей, не ползунком);
     обоснование ступени -- статичным текстом, не title #}
  {% set ladder_band = band_class(health.band) %}
  <div class="ladder">
    {% for step in ["h0", "h1", "h2", "h3", "h4"] %}
    <div class="ladder-step {{ 'current ' ~ ladder_band if step == health.state else '' }}">
      <div class="ladder-dot"></div>
      <div class="ladder-label">{{ state_labels.get(step, step) }}</div>
    </div>
    {% endfor %}
  </div>
  {% set state_ev = ((health.state_evidence or [])|map(attribute="label")|join("; ")) %}
  {% if state_ev %}<p class="hero-why">почему: {{ state_ev }}</p>{% endif %}

  {# 3. Три координаты: значение подписано («NN из 100»), направление шкалы
     подписано, «почему» -- статичный контрастный абзац под линией #}
  {% set coord_meta = [
      ("damage", "Повреждения", "накопленный необратимый износ железа · выше = хуже"),
      ("resilience", "Устойчивость", "запас прочности к новым сбоям · выше = лучше"),
      ("observability", "Наблюдаемость", "полнота данных для оценки · выше = лучше"),
  ] %}
  <div class="hero-coords">
    {% for key, label, sub in coord_meta %}
    {% set coord = health[key] or {} %}
    {% set cband = band_class(coord.band) %}
    {% set cev = ((coord.evidence or [])|map(attribute="label")|join("; ")) %}
    <div class="coord-card">
      <div class="head">
        <span class="name">{{ label }}</span>
        <span class="chip {{ cband }}">{{ ("%.0f"|format(coord.value)) ~ " из 100" if coord.value is not none else "—" }}</span>
      </div>
      <div class="coord-sub">{{ sub }}</div>
      <div class="coord-line">
        <i class="{{ cband }}" style="left:{{ coord.value if coord.value is not none else 0 }}%"></i>
      </div>
      <p class="coord-why">{% if cev %}почему: {{ cev }}{% else %}отклонений не зафиксировано{% endif %}</p>
      {% if coord.confidence %}<div class="coord-conf">уверенность: {{ conf_ru.get(coord.confidence, coord.confidence) }}</div>{% endif %}
    </div>
    {% endfor %}
  </div>

  {# 4. Слепые зоны видны и при известном состоянии (раньше терялись) #}
  {% if health.blind_spots %}
  <div class="axis-blind">⚠ не видно: {{ health.blind_spots|join("; ") }}</div>
  {% endif %}

  {# 5. Чипы: каждое значение подписано, латиницы нет #}
  {% set horizon_label = {7: "≤7 дней", 30: "≤30 дней", 90: "≤90 дней"}.get(health.horizon_days, "не прогнозируется") %}
  {% set delta = health.delta_7d %}
  {% set delta_cls = "na" if delta is none or delta == 0 else ("good" if delta > 0 else "bad") %}
  {% set delta_arrow = "—" if delta is none else ("▲" if delta > 0 else ("▼" if delta < 0 else "→")) %}
  <div class="hero-chips">
    <span class="chip accent" title="сводный индекс здоровья: свод повреждений и устойчивости; 0–100, выше = лучше; никогда не заменяет три координаты выше">
      сводный индекс: {{ ("%.0f"|format(health.index)) ~ " из 100" if health.index is not none else "—" }}
    </span>
    <span class="chip {{ band_class(health.band) }}" title="{{ health.horizon_reason or 'прогноз окна до ухудшения состояния' }}">окно до ухудшения: {{ horizon_label }}</span>
    <span class="chip {{ delta_cls }}" title="изменение сводного индекса за 7 дней">за 7 дней: {{ delta_arrow }}{% if delta is not none %} {{ "%.1f"|format(delta) }}{% endif %}</span>
    {% if health.confidence and health.confidence != "unknown" %}
    <span class="chip na" title="{{ conf_tip }}">уверенность: {{ conf_ru.get(health.confidence, health.confidence) }}</span>
    {% endif %}
  </div>
```

Дальше без изменений: `{% if health_series %}` sparkline-блок (подпись «сводный индекс, динамика»), `{% endif %}{% endif %}</div>`.

- [ ] **Step 4: Заменить `<style>` hero-партиала** (старые `.hero-coords`-grid остаётся, `.ladder*` остаётся, добавить новые классы, ничего из старого кроме неиспользуемого не удалять; `.riskbar`-разметки в hero больше нет — глобальный класс из base.html не трогаем):

```css
.hero-coords { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin: 14px 0 0; }
.hero-verdict { margin: 4px 0 2px; }
.hero-kv-l { font: 600 11px/1 var(--mono); letter-spacing: 1.2px; text-transform: uppercase; color: var(--muted); margin-right: 6px; }
.hero-state { font: 700 22px/1.2 var(--font-body); }
.hero-state.good { color: var(--good); } .hero-state.warn { color: var(--warn); }
.hero-state.high { color: var(--high); } .hero-state.bad { color: var(--bad); }
.hero-state.na { color: var(--na); }
.hero-action { font-size: 15px; color: var(--text); margin: 8px 0 0; line-height: 1.55; }
.hero-why { font-size: 13px; color: var(--text); margin: 6px 0 0; line-height: 1.5; }
.coord-card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r-card); padding: 14px 16px; box-shadow: var(--shadow-card); }
.coord-card .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.coord-card .name { font-weight: 600; font-size: 16px; }
.coord-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
.coord-line { position: relative; height: 2px; background: var(--line); border-radius: 1px; margin: 14px 0 2px; }
.coord-line > i { position: absolute; top: -5px; width: 3px; height: 12px; border-radius: 2px; transform: translateX(-50%); }
.coord-line > i.good { background: var(--good); } .coord-line > i.warn { background: var(--warn); }
.coord-line > i.high { background: var(--high); } .coord-line > i.bad { background: var(--bad); }
.coord-line > i.na { background: var(--na); }
.coord-why { font-size: 13px; color: var(--text); margin: 8px 0 0; line-height: 1.5; }
.coord-conf { font: 600 11px/1 var(--mono); letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-top: 6px; }
```

(Старые `.hero-chips/.hero-spark-head/.btn-small.active/.no-data/.ladder*` — оставить как есть. Hex нигде — пин-тест хексов обязан остаться зелёным.)

- [ ] **Step 5: В device.html** дополнить словарь (строка 8): `{% set conf_ru = {"low": "низкая", "medium": "средняя", "high": "высокая", "unknown": "неизвестна"} %}` — контракт `Coordinate.confidence` допускает `"unknown"` (health.py:165), словарь обязан его покрывать.

- [ ] **Step 6: Прогнать тесты**

Run: `python -m pytest tests/test_device_hero.py tests/test_device_card.py -q`
Expected: PASS, включая оба хекс-пина (`test_hero_no_hardcoded_hex_colors_*`).

- [ ] **Step 7: Commit**

```bash
git add server/web/templates/_device_hero.html server/web/templates/device.html tests/test_device_hero.py
git commit -m "feat(dashboard): вердикт «Состояние машины» -- линия вместо ползунка, статичное «почему», все значения подписаны"
```

---

### Task 5: Гейты, живая проверка, CHANGELOG, ревью, merge

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `CONTINUITY.md`

- [ ] **Step 1: Полные гейты** (рецепты из Makefile, `make` на машине нет):

```
python -m ruff check .                # Makefile:25
python -m mypy                        # Makefile:32 (цели в pyproject, БЕЗ аргументов)
python -m bandit -c pyproject.toml -q -r server client shared   # Makefile:35
python -m pytest --cov=server --cov=shared --cov-report=term-missing   # Makefile:41, гейт 80%
python smoke.py
```

Expected: всё зелёное. Известная ловушка: smoke/print-tracking могут падать из-за уехавшей фикстурной даты НЕЗАВИСИМО от этой ветки — проверять через `git stash` перед охотой (memory `[[smoke-print-tracking-date-timebomb]]`).

- [ ] **Step 2: Живая проверка глазами** (skill `run`/webapp-testing): поднять сервер (`python -m uvicorn server.main:app --port 8000` или как в smoke.py), открыть `/device/{id}` живого устройства, проверить ВО ВСЕХ ТРЁХ ТЕМАХ (кнопка тем в шапке): порядок блоков (Компьютер → Состояние машины → Требует внимания → Прогноз → Подробная диагностика → События…), линию-шкалу с меткой, статичные «почему», раскрывашку. Сделать скриншоты до/после для владельца. Мобильная ширина ~360px: spec-grid и hero-coords обязаны схлопнуться в 1 колонку (auto-fit).

- [ ] **Step 3: CHANGELOG** — в `## [Unreleased]` под `### Changed` добавить:

```markdown
- Карточка устройства переработана под приоритеты инженера: вверху — блок «Компьютер»
  (процессор, память, диски, система, живые значения); вердикт «Состояние машины» ведёт
  словами (состояние · главный фактор · что делать), координаты получили линию-шкалу со
  статичным пояснением «почему» вместо hover-подсказки, каждое значение подписано
  («NN из 100», «уверенность: высокая», направление шкалы); «Риск-экспозиция» →
  «Суммарный риск сбоя», «Риск траектории» → «Риск по трендам»; оси без замечаний, старые
  сводные баллы, покрытие источников и классы отказа свёрнуты в «Подробную диагностику»;
  дублирующий «Инвентарь» удалён (содержимое — в «Компьютере»); латинские обозначения
  (D · R · O) убраны из интерфейса.
```

- [ ] **Step 4: Финальное ревью всей ветки** — субагент `code-reviewer` (Sonnet) по `git diff main...HEAD`, в брифе ОБЯЗАТЕЛЬНО: (а) XSS-чеклист — все агентские строки только через autoescape, `|tojson`-острова не тронуты, `|safe` не появился, атрибуты в двойных кавычках; (б) grep-симптом по ВСЕМУ диффу: голые значения без подписи и hover-only пояснения (класс бага, который правим, не должен уцелеть в других местах диффа — memory `[[review-scope-blind-spots]]`); (в) три темы держатся на токенах (нет hex — кроме уже существующих fallback в JS). Править CRITICAL/HIGH сразу.

- [ ] **Step 5: CONTINUITY.md** — обновить активный тред: карточка переработана, план исполнен, T3 из cctodo-completion-plan (C40) закрыт этой веткой.

- [ ] **Step 6: Merge + push** (авто, без вопросов — `[[auto-merge-push-no-ask]]`):

```bash
git add CHANGELOG.md CONTINUITY.md
git commit -m "docs(changelog): карточка устройства -- иерархия для инженера"
git checkout main
git merge --no-ff feat/device-card-redesign -m "Merge feat/device-card-redesign: карточка устройства -- иерархия для инженера"
git push origin main
```

---

## Самопроверка плана (выполнена автором 2026-07-15)

- Каждое требование владельца покрыто задачей: «риск-экспозиция»/«риск траектории» → T1/T2 (переименование+понижение); скрыть ненужное в раскрывашке → T1/T2; дубли → T2 (Day-1/coverage/классы), T3 (Инвентарь); характеристики вверху → T3; приоритеты инженера → порядок блоков (File Structure); «уверенность высокая» и подписи → T1 (axis-conf) + T4 (координаты/чипы); без латиницы → T4; статичное пояснение вместо hover → T4; ползунок → линия → T4; графики не ухудшены → «Что сознательно НЕ делаем»; «не сделать хуже» → ничего не удаляется из DOM (кроме дубля «Инвентаря», переехавшего наверх), все чужие пины перечислены в Global Constraints, живая проверка в 3 темах в T5.
- Плейсхолдеров нет; «перенос 1:1» всегда указывает точные строки исходника и точечные правки.
- Имена согласованы между задачами: `axm.render_axis`, `ns_ax`, `id="attention-label"`, `id="device-diagnostics"`, `conf_ru`, класс `coord-line` — единые в T1-T4 и тестах.
