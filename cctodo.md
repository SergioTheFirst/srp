# SRP — план развития (cctodo)

> Источник: стратегическое ревью кода (`ClaudeCodeSRP.md` ч.1–3, `srp_mvp_plan.md` +
> весь `server/`, `client/`, `tests/`, CI) → адверсариальная критика спеки → два
> решения по scope. Документ — рабочий roadmap с привязкой к реальному коду, а не
> очередная «единая теория отказов».

---

## 0. Ведущий тезис (циничный, без подыгрывания)

Главный риск проекта — **не «небезопасный агент»**, а построить предиктивную систему
на недостоверной телеметрии и потом месяцами крутить ML поверх ложных данных. Windows
как источник **врёт**: `MSAcpi_ThermalZoneTemperature` — мусор на половине OEM, SMART/NVMe
поля vendor-specific, WMI-провайдеры висят, Event ID плывут между билдами, коллектор без
прав SYSTEM возвращает пусто. Поэтому слой доверия телеметрии — **P0 без компромиссов,
раньше любой аналитики.**

Второй тезис: **предсказуемость доменно-ограничена.** Реально прогнозируемо — износ
накопителя, батарея, заполнение диска, тренд boot-time, fleet-anomaly, throttle-trend,
driver-regression. Остальное (random BSOD, VRM/PSU/мать, intermittent, док) — постфактум.
Строить «оракул отказов» поверх непрогнозируемого = дорогой noise amplifier.

Третий тезис: **в прогнозируемых деплеция-доменах ML не нужен.** SSD %used / battery FCC /
disk-fill — это детерминированная арифметика (порог + slope + ETA), а не survival и не
классификатор. ML/survival/петля меток — **отложенные гейтованные фазы**, строятся только
на доказанной необходимости, не как центр системы.

---

## 1. Решения и инварианты (decision record)

| # | Решение | Почему |
|---|---------|--------|
| D1 | **Telemetry-trust = P0, до аналитики.** | Garbage in → Bayesian garbage out. Фатальнее, чем отсутствие TLS-rotation. |
| D2 | **«Unknown != healthy» enforced end-to-end.** | Сейчас коллектор отдаёт `None`, но скор всё равно стартует со 100 → дохнущий диск без прав выглядит здоровым. |
| D3 | **Прогноз только в предсказуемых доменах.** Непрогнозируемое — инцидент/корреляция для показа, не prediction. | Не плодить false positives там, где сигнала нет. |
| D4 | **Wear-домены = тренд-математика + ETA, НЕ ML.** | survival там избыточен, а не «слабо калиброван». |
| D5 | **Тонкий объяснимый Bayesian-приоритизатор остаётся.** Никакого unified ML-оракула. | Объяснимость by-construction — конкурентное преимущество (`bayesian.py`). |
| D6 | **KP41 и WHEA — только correlation/burst-enhancer, не самостоятельные драйверы.** | KP41 specificity ≈ 0; WHEA в основном firmware/ASPM-шум. Сейчас в коде они прямые драйверы — дефект. |
| D7 | **Deployable architecture, но НЕ deployment-driven sequencing.** | Форсировать prod-hardening = installer/cert/ACL/GPO hell при всё ещё мусорных данных. |
| D8 | **Scope = Phased/gated.** survival + label loop + per-device anomaly — запланированы, но за гейтом доказанной нужды, не убиты. | Сохранить опцию, не строить собор ради ~20% непрогнозируемого остатка сейчас. |
| D9 | **Retain compressed raw windows для репроцессинга.** | Premature compression убивает ретроспективный feature-mining: «нашли новый precursor» → а данных нет. |

### Анти-цели (НЕ строить сейчас — premature infra gravity)
- Unified ML-оракул / «general PC health AI» / random-death prediction.
- RBAC-cathedral, PKI-empire, multi-tenant policy orchestration.
- Auto-remediation, fleet-wide remote execution, сложный updater.
- Survival/labels как центр до прохождения гейтов §7.

---

## 2. P0 — Целостность слоя данных (АБСОЛЮТНЫЙ ФУНДАМЕНТ, до аналитики)

Без этого любой скор — мусор-на-мусоре. Это не откладывается вообще.

### W0.1 — Append-only лонгитюдное хранение (перестать стирать производные)
- [ ] `historical`: снять `PRIMARY KEY(device_id)`, добавить `id INTEGER PK AUTOINCREMENT` + индекс `(device_id, id)`; `store_historical` — `INSERT` без `ON CONFLICT`; «последнее» — `ORDER BY id DESC LIMIT 1` (`db.py:39`, `:148`, `:332`).
- [ ] `scores`: то же — история скоров, не перезапись (`db.py:62`, `:199`). Без неё нельзя показать «ухудшается ли» и нельзя потом цеплять метки.
- [ ] `heartbeats`: поднять/заменить cap `_retain_hb=500` (`db.py:19`) политикой downsample: сырое окно last-N часов + почасовая/посуточная свёртка дольше. Цель — недели истории для slope, а не 41 час.
- [ ] Сохранять **compressed raw windows** ключевых метрик (не только производные) для будущего репроцессинга (D9).
- [ ] **Replayability:** возможность переиграть сохранённую историю через обновлённый scoring (офлайн-пересчёт) — тест «replay даёт детерминированный результат».

### W0.2 — Серверное время + дрейф часов
- [ ] Штамповать `received_at` на сервере для всех телеметрийных строк; перестать доверять `env.ts` как истине (`pipeline.py:27`). Хранить оба.
- [ ] Детект клок-дрейфа как сигнал (двойной смысл: и порча трендов, и старение CMOS) — флаг при |received_at − ts| выше порога.

### W0.3 — Telemetry trust layer (ядро P0)
> Контракт: `telemetry-trust-contract.md` · имплемент-план (Plan 1 Trust Core): `telemetry-trust-plan.md`.
- [ ] **Capability matrix per device:** какие источники реально доступны (SMART/StorageReliabilityCounter/battery/thermal/RSI/perf-классы). Источник недоступен ≠ «всё ок».
- [ ] **Collector self-diagnostics:** каждый коллектор возвращает статус `ok | blocked | empty | partial | stale` + причину, а не молчаливый `None` (`ps.py`, все `collectors/*`). Контракт расширить полем здоровья источников в конверте (`shared/schema.py`, `extra="allow"` это позволяет forward-compat).
- [ ] **Per-source confidence/freshness:** возраст и полнота каждого сигнала; пробрасывается в скоринг.
- [ ] **Surface на дашборде:** строка «датчик X недоступен / устарел» (`device.html`). Оператор обязан отличать «здоров» от «данных нет».

### W0.4 — Дисциплина схемы
- [ ] Формализовать `CONTRACT_VERSION` (`shared/schema.py:25`): тесты forward/backward-compat, капабилити-негоциация версий агент↔сервер.

### W0.5 — Confidence-gated scoring (связка D2)
- [ ] Скоры и риск **деградируют до «insufficient data / low confidence»** при нехватке покрытия, а не стартуют со 100 (`scores.py` все `_*` начинают со 100.0; `_risk_exposure` с 0.0). Coverage низкий → не «здоров», а «не знаю».
- [ ] Риск показывать **бэндами** (`level` уже есть в `bayesian.py:52`), убрать голый процент — некалиброванная вероятность ≠ probability (ложная точность на `device.html`).

---

## 3. P1 — Минимальная deployability (ПАРАЛЛЕЛЬНО, не блокирует аналитику, без церемоний)

Проектируем как deployable, но план не заложник enterprise-hardening.

- [ ] **ingest auth:** общий токен или per-agent ключ на `/api/v1/ingest` (`api.py:19`). Сейчас открыт всем → спуфинг `device_id` + отравление телеметрии тривиальны.
- [ ] **TLS** на reverse-proxy (без собственного PKI-эмпайра).
- [ ] **Закрыть дефолт-конфиг:** перестать дефолтить на публичный `http://212.42.56.189:8000` (`client/config.py:19`); дефолт — localhost или явный обязательный `server_url`. Текущий дефолт = «случайный небезопасный деплой».
- [ ] **Windows Service под LocalSystem** (nssm/sc) — иначе SMART/StorageReliabilityCounter/часть WMI пусты (D2). Сейчас консольный `--once`/loop (`agent.py`).
- [ ] **Signed config/schema** (лёгкая подпись, не PKI).
- [ ] **Transport hardening (`transport.py`):** джиттер на реконнект (убить thundering herd при массовом восстановлении сервера); idempotency-key в конверте → серверный дедуп (сейчас retry-после-коммита = дубли); cap размера payload.
- [ ] **Server input limits:** лимит тела запроса, per-device rate-limit (DoS через синхронный rescore под глобальным локом).

---

## 4. Аналитический фундамент (на ДОВЕРЕННЫХ лонгитюдных данных) — собственно ценность

Строится поверх P0. Детерминированно, узко, объяснимо. Multi-engine, не unified core.

### W4.0 — Платформа
- [ ] **Развязать ingest и scoring:** писать быстро, пересчитывать async (сначала in-process очередь) — снимает дефект «store+rescore под глобальным `threading.Lock`» (`pipeline.py:50`, `db.py:21`) и заодно scaling-предпосылка. Убрать бессмысленный rescore на `events` (события скорингом не читаются).

### W4.1 — Тренд-движок (теперь возможен — история есть)
- [ ] Slopes + ETA по деплеция-доменам: **SSD wear→ETA**, **disk-fill→дата заполнения**, **battery FCC→ETA**, **boot-time trend**, **throttle-residency trend**. Это D4 — арифметика, не ML.

### W4.2 — Независимые доменные движки
- [ ] **Storage health** (детерминированный + тренд): SMART/StorageReliabilityCounter ведущий; latency — только **подтверждение** к SMART, не самостоятельный сигнал (causal confounding: Defender/OneDrive/BitLocker/low-RAM/thermal).
- [ ] **Battery** (детерминированный): FCC/Design тренд + циклы + charge-residency (риск swelling ≠ возраст).
- [ ] **Disk-fill / servicing collapse** (детерминированный forecast): free-space slope → upstream WU-сбоев.
- [ ] **OS-degradation** (эвристический): RSI тренд, crash-rate, boot-rot, pending-reboot как множитель.
- [ ] **Fleet-anomaly** (статистический): когорта по model+build; детект bad-patch/driver-rollout, site-wide power (кластер KP41 по локации/окну = электрика здания, не отказ ПК). Снимает массовые ложные «железные» тревоги.

### W4.3 — Фиксы риск-движка (`bayesian.py`)
- [ ] **Демотировать KP41 и WHEA** до burst/correlation-enhancer; самостоятельно риск не поднимают (D6). Сейчас `_power_thermal`/`_memory` дают им прямой вес.
- [ ] **Тонкий Bayesian-приоритизатор** поверх доменных движков (D5): объяснимость сохранить, оракул не строить.
- [ ] Пересмотреть `overall = max(prob)` (`bayesian.py:221`) в свете двух шкал риска (`risk_exposure` 0–100 vs class prob) — одна когерентная иерархия для оператора.

---

## 5. Дашборд / оператор (triage)

- [ ] **Тренд-вьюхи** (теперь возможны): «ухудшается ли» — главный недостающий элемент early-warning.
- [ ] **Staleness-флаг:** мёртвый агент / выключенная машина не должны выглядеть здоровыми (пропавшая машина ≠ ОК).
- [ ] **Source-coverage / confidence** на карточке устройства (связка W0.3).
- [ ] Фильтр/поиск/сортировка флота (`fleet.html` — фикс-таблица не масштабируется на 50+).
- [ ] Разделять **predictive** («вероятно сломается, lead-time N») и **incident** («уже случилось»).
- [ ] Минимальный «newly escalated» вид (push-light), иначе «раннее предупреждение» требует ручного наблюдения.

---

## 6. Наблюдаемость пайплайна + тех-долг качества

### Observability (operate-ability)
- [ ] Структурные логи сервера; `/metrics`: ingest rate, ожидание write-лока, протухшие устройства, отказы буфера, rollup collector-health.
- [ ] Внутренняя страница «здоровье пайплайна».

### Тестовый долг (самый хрупкий слой — наименее проверен)
- [ ] **Тесты коллекторов** через golden recorded-JSON фикстуры (PowerShell-вывод → парсер). Сейчас весь data-acquisition слой без тестов; CI гоняет только `pytest`.
- [ ] **Locale-фикстуры** (вывод русской Windows) для парсеров — проверка языконезависимости фактом.
- [ ] Расширить `mypy` на `client/` или хотя бы типизировать парсинг (`pyproject.toml files=["shared","server"]`).
- [ ] Boundary/property-тесты скоринга за пределами двух синтетических машин (`tests/conftest.py`).

---

## 7. Отложенные ГЕЙТОВАННЫЕ фазы (Phased/gated — D8)

Запланированы, не убиты. Каждая — с явным гейтом. Не начинать без прохождения гейта.

- [ ] **L1 — Label loop scaffolding:** хранить предсказания с id + поле исхода; минимальный приём подтверждённых отказов (ITSM/CSV).
  - **GATE:** есть реальный потребитель вопроса «прогноз сбылся?» И накоплена история предсказаний. Помнить: enterprise-метки контаминированы (reimage, замена без тикета, шаблонное закрытие) → не строить тяжёлый supervised поверх мусора.
- [ ] **L2 — Per-device anomaly** (EWMA / control-charts) как enhancer likelihood.
  - **GATE:** детерминированные движки §4 доказанно пропустили реальный precursor.
- [ ] **L3 — Survival** — ТОЛЬКО для деплеция-доменов, где тренд-ETA доказанно недостаточен, И метки прошли проверку контаминации.
  - **GATE:** калибровка (Brier/reliability) бьёт детерминированный ETA-baseline в shadow-режиме. Иначе не промоутить.

---

## 8. Последовательность

```
P0 (W0.*) ── абсолютный фундамент, БЕЗ компромиссов, первым
   │  append-only история · серверное время · telemetry-trust · capability/confidence
   │  · collector-diagnostics · «unknown != healthy» · confidence-gated scoring
   │
   ├── P1 (W1.*) ── параллельно, минимально, без церемоний (auth/TLS/service/transport)
   │
   ▼
Аналитика (W4.*) ── на доверенных лонгитюдных данных: тренд-ETA + доменные движки
   │                + фиксы KP41/WHEA + тонкий Bayesian-приоритизатор
   ▼
Дашборд/оператор (§5) + observability/тесты (§6)
   ▼
Гейтованные фазы (§7) ── только по доказанной нужде: L1 → L2 → L3
```

**Правило:** ничего из «Аналитики» не считается доверенным, пока не закрыт P0. Гейтованные
фазы §7 не стартуют без своего гейта. Анти-цели §1 не строятся вообще.

---

## 9. Открытые вопросы (решить по ходу)
- Формат retention raw-окон (W0.1): фикс-окно vs adaptive по волатильности?
- Где проходит граница «достаточного confidence» для показа скора vs «insufficient data» (W0.5)?
- Когортирование fleet-anomaly (W4.2): model+build достаточно, или нужен ещё firmware/OEM-image?
- Async-rescore (W4.0): in-process очередь достаточно до какого масштаба, прежде чем выделять воркер?
