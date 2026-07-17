# Telemetry-Trust Contract (W0.3) — operational spec

> P0-подсистема из `cctodo.md` §2 W0.3. До неё любой скор — мусор-на-мусоре.
> Это контракт (строгие semantics), а не направление. Зависимость вниз: trend-based
> валидаторы (§10) ждут append-only истории (W0.1).

---

## 0. Класс системы (зафиксировать первым)
SRP — **high-trust degradation detection platform**, НЕ «AI предсказывает отказы». Это
смена класса, не маркетинг. Слой telemetry-trust определяет, **может ли система вообще
делать утверждение по сигналу.** ML/survival/labels — гейтованные фазы (`cctodo.md` §7),
не ядро.

## 1. Прайм-директива
Быть правым в том, что знаешь; честным в том, чего не знаешь. **Под неопределённостью —
отказ от утверждения (UNKNOWN), не угадывание.** Confidence существует ТОЛЬКО для:
suppression · ranking · graceful degradation · auditability. **НЕ для вычисления истины.**

## 2. Что контракт предотвращает (why — чтобы будущие правки его не разъели)
1. **Компенсацию неизвестности.** `disk = unavailable`, остальное healthy → НЕ «low risk»,
   а `storage domain = UNKNOWN`. Неизвестность не маскируется здоровьем соседей.
2. **Probabilistic soup.** Без state-gate система сползает в `0.82·0.71·0.93` confidence
   fusion, которую никто не объясняет / не дебажит / не доверяет.
3. **Смешение transport и semantics.** «Данные пришли» ≠ «данные правдивы». Разные миры.

## 3. Load-bearing решение: `state` = gate, `weight` = modulation
- **`state`** — authoritative gate: структурное решение (включить домен / suppress / UNKNOWN).
- **`weight ∈ [0..1]`** — только вторичная модуляция ВНУТРИ допустимого; вычисляется
  ИСКЛЮЧИТЕЛЬНО для gate-pass состояний.
- **Жёсткое правило:** `weight` НИКОГДА не реанимирует gate-failed источник. Высокий вес
  не компенсирует `unavailable / stale / suspect`. States принимают structural decisions,
  weights только ранжируют внутри допустимого.

## 4. Две ортогональные оси (НЕ смешивать)
| Ось | Владелец | Вопросы |
|---|---|---|
| **collector-trust** | агент | доступен? свежий? payload полный? коллектор не упал? |
| **semantic-trust** | сервер | физически возможно? counters не frozen? cross-field консистентно? delta возможна? known-bad? |

**Канонический пример (зачем развязка):** `thermal zone = 27°C`, fresh=yes, collector
healthy — но Dell OEM возвращает фейковую константу, не меняется под нагрузкой.
→ `collector_trust = HIGH`, `semantic_trust = LOW`. Эта развязка спасает от целого класса
false confidence. Владение: collector-логика стабильна → агент; semantic-валидаторы
эволюционируют → сервер (иначе редеплой агента ради фикса валидатора).

## 5. Атрибуты источника
**Сырьё:**
- `collector_status` (агент): `ok | partial | empty | timeout | blocked | absent`
- `semantic_status` (сервер): `plausible | implausible | inconsistent | frozen | known_bad | unchecked`

**Производное:**
- `state`: `OK | DEGRADED | STALE | UNAVAILABLE | SUSPECT | NOT_APPLICABLE`
- `weight ∈ [0..1]` — только для `OK` (1.0) / `DEGRADED` (<1.0)

**Порядок вывода `state` (первое совпадение):**
1. `NOT_APPLICABLE` — легитимно неприменим (датчика на этом железе не существует). **Не деградация**, домен не существует.
2. `SUSPECT` — `semantic_status ∈ {implausible, inconsistent, frozen, known_bad}`. **Семантика бьёт коллектор** (свежая полная ложь опаснее отсутствия).
3. `UNAVAILABLE` — `collector_status ∈ {empty, timeout, blocked, absent}`.
4. `STALE` — возраст > stale-порога источника.
5. `DEGRADED` — `partial` | мягко-устарел | low-but-plausible.
6. `OK`.

**UI-схлопывание (оператор):** `OK / DEGRADED / UNKNOWN (=UNAVAILABLE+STALE) / SUSPECT`;
`NOT_APPLICABLE → «—»`. Внутри — гранулярно для lineage.

**Единица доверия = ИСТОЧНИК** (не поле, не домен). `partial` покрывает «часть полей пуста».
Field-level trust — вне scope (§13).

## 6. Outcome scoring-движка — UNKNOWN как first-class
Домен / устройство → **`HEALTHY | DEGRADED | AT RISK | UNKNOWN`**. UNKNOWN — полноправный
исход, отдельный bucket, **не «здорово»**. Движок ОБЯЗАН уметь сказать «не знаю» — это
зрелость, которой нет у большинства enterprise monitoring (они всегда производят score).

## 7. Mandatory / optional + карта доменов
- **Глобально mandatory:** идентичность (`inventory`: id/os/chassis/возраст). Gate-fail →
  device = `untrusted`, **скоров нет вообще** (нет приоров/когорты).
- **Per-domain required → optional:**

| Домен | required | optional |
|---|---|---|
| Storage | StorageReliability (SMART) | heartbeat disk latency (только подтверждение) |
| Disk-fill | heartbeat free_space_pct | — |
| OS-degradation | historical RSI / event-counts | — |
| Boot-trend | historical avg_boot_ms | — |
| Thermal-proxy | heartbeat cpu_perf_pct (throttle) | — |

## 8. Реакция scoring = Tiered
- **req** gate-fail → домен = **UNKNOWN**; явно показан; **не сворачивается** в оптимистичный агрегат/флот-ранкинг.
- **opt** gate-fail → домен скорится на req; улика мёртвого opt дропается; confidence-бэнд понижен.
- `DEGRADED` req → домен скорится, вес аттенюирован, бэнд это отражает.
- device `untrusted` → утверждений нет.
- Флот-ранкинг: known-risk DESC; **UNKNOWN — отдельный bucket «мы тут слепы»**, не «хорошо».

## 9. Materiality governor (ключевой ограничитель v1)
**Semantic-валидация — ТОЛЬКО для сигналов, материально влияющих на решения.** Иначе
строим «систему оценки качества телеметрии», а не «систему деградации ПК».

| Сигнал | Semantic-валидация |
|---|---|
| SMART wear / reallocated / errors | ДА |
| free_space_pct | ДА |
| RSI / event-counts (KP41/6008/bugcheck/WHEA) | ДА |
| boot-time | ДА |
| thermal / throttle-residency | ТОЛЬКО frozen/constant-check (ловит OEM-фейк-константу, §4) |
| raw CPU% | НЕТ |
| queue length | НЕТ |
| прочие perf-counters | НЕТ |

**Правило:** decision-immaterial источник → `semantic_status = unchecked` → **не может стать
SUSPECT**, но и не блокирует (его роль мелкая). **collector-trust применяется ко ВСЕМ
источникам** (freshness/availability дёшевы и универсальны); materiality сужает только
semantic-trust.

## 10. Semantic-валидаторы v1 (F3)
**v1 (на decision-material источниках, §9):**
- range / физ-границы (wear 0..100, FCC≤design, % в 0..100, neg→implausible) — stateless
- cross-field консистентность (SSD media_type с HDD-only полями) — stateless
- known-bad **hook** + seed-список (OEM/model/fw → SUSPECT или cap weight) — stateless lookup
- **frozen-counter + impossible-delta — на `last-good-sample per source`** (1 строка, БЕЗ W0.1)

**Отложено до W0.1** (нужна реальная история): trend/drift-плаузибилити («slope счётчика
статистически невозможен против его прошлого»), volatility по многим сэмплам. Контракт
резервирует `SUSPECT` + enum + lineage сейчас → дефер не меняет схему.

**A1:** агент чистит+репортит факты (тривиальная нормализация типа дроп
`"to be filled by o.e.m."` — это data-cleaning, не суждение); сервер судит плаузибилити.

## 11. Lineage (auditability)
В каждом скоре per-domain: участвовавшие источники + `{collector_status, semantic_status,
state, weight, age}`. Полный аудит «почему система утверждает / отказывается утверждать X».
Хранится рядом со скором (расширение `scores.risk` JSON или параллельный blob).

## 12. Контракт агент↔сервер
- **Агент:** payload + блок `source_health` (per logical source: `{status, collected_at}`).
  Forward-compat (`extra="allow"` уже позволяет; формализовать в `shared/schema.py`).
- **Сервер:** `collector_status` ← агент; `semantic_status` ← валидаторы; `state`+`weight`+
  `lineage` ← вывод.
- **Capability matrix** per-device/cohort: что _должно_ быть доступно → отличает `absent`
  (норма для модели) от `newly-blocked` (раньше слал, перестал = сигнал: регресс прав /
  смена железа).

## 13. Scope ceiling — НЕ строить (anti-over-engineering, жёстко)
**Запрещено:** nested confidence · confidence-of-confidence · propagated uncertainty trees ·
evidence DAGs · confidence propagation graphs · OEM-registry-as-platform · plausibility-
engine-as-research.

**Останавливаемся НА:** `state` · `weight` · `collector_trust` · `semantic_trust` ·
`freshness`. Этого более чем достаточно для industrial-grade. Любой дрейф в probabilistic
epistemology / confidence calculus — режется на ревью.

## 14. Acceptance criteria (когда v1 готов)
- `disk` req unavailable → `storage = UNKNOWN`; device НЕ выглядит healthy; агрегат не оптимистичен.
- thermal fake-constant → `semantic_status = low/implausible` → throttle-улика не повышает «здоровье»; `state` ∈ {SUSPECT, DEGRADED} по materiality.
- **weight не может поднять gate-failed источник** (явный тест).
- lineage объясняет любой UNKNOWN/score.
- semantic-валидаторы НЕ применяются к CPU% / queue length (materiality, тест).
- scoring-движок возвращает UNKNOWN как первоклассный исход (не суррогат 0 или 100).

## 15. Open questions
- Точные stale-пороги per source (× ожидаемой каденции).
- Порог coverage «score vs UNKNOWN» на уровне домена.
- Где живёт known-bad seed (формат, обновление) — БЕЗ превращения в платформу.
