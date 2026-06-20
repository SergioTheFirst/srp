# RFC: SRP Network Discovery (подсистема автообнаружения и топологии сети)

> Статус: **DRAFT для реализации** · Автор: Lead Architect (Claude, Opus 4.8, R4) · Дата: 2026-06-20
> Тип: Enterprise RFC. Достаточен для реализации без дополнительного проектирования.
> План реализации (12 фаз, атомарные задачи): [`docs/superpowers/plans/2026-06-20-network-discovery-plan.md`](../plans/2026-06-20-network-discovery-plan.md)
> Источник идей NetXMS: архитектурное знание проекта (web-доступ в сессии заблокирован; идеи проверяемы по `src/server/core/{netinfo,topology,session,poll,fdb}.cpp`, `src/snmp/`, wiki.netxms.org — отмечено где требуется верификация).

---

## 0. Сводка решения (TL;DR)

SRP уже содержит **скрытую подсистему сетевого обнаружения** — пакет `server/printers/` (stdlib-SNMP, bounded active-scan, anti-DoS poll-cycle, candidate-dedup) и read-side `server/analytics/netmap.py` (gateway-кластеры из ARP агентов). Эти куски **доказали** ключевые инженерные паттерны на проде. Но они: (а) заточены под принтеры; (б) топология эфемерна — `netmap` пересчитывается на каждый просмотр страницы из `historical`-payload, нет персистентной инвентаризации, нет связей (links), нет истории, нет детекции изменений.

**Решение:** новый серверный пакет **`server/netdisco/`** — обобщение принтерного движка до «всех сетевых устройств» + добавление настоящего **графа топологии L2/L3** с персистентным хранением, агрегацией из нескольких источников (data fusion), детекцией изменений и topology-aware корреляцией событий. Лучшие идеи NetXMS адаптируются (см. §1), **код не переносится** — переносятся паттерны и алгоритмы.

**Ключевые архитектурные ставки (обоснование в §3):**
1. **Server-side, поллинг-центрично.** Обнаружение и топология живут на сервере, переиспользуя stdlib-SNMP из `printers/`. Агент остаётся тонким (stdlib, zero-deps) и лишь поставляет L2/L3 *улики* (ARP/adapters — уже есть; опционально расширяется аддитивно).
2. **Разделение типов поллинга** (NetXMS poll-type separation) — независимые планировщики: `discovery` (найти адреса), `classify` (что это), `topology` (связи), `reachability` (живо ли). Каждый со своим интервалом, rate-limit, jitter.
3. **Топология = граф улик с примирением источников** (evidence-graph). Связь L2 не «факт», а *вывод* из конкурирующих улик (LLDP > CDP > FDB > эвристика), каждая с весом доверия. Прямое наследование SRP-принципа «collector⊥semantic, UNKNOWN over false confidence».
4. **Персистентная инвентаризация + история + change-detection** через зеркалирование паттерна `printers`/`printer_readings` (COALESCE-инвентарь + append-only readings).
5. **Reachability-корреляция** (NetXMS unreachable-vs-down): шлюз/аплинк недоступен → подавить алармы за ним, поднять один root-cause. SRP уже имеет зачаток (`netmap` subnet-anomaly) — поднимаем до графа.

**Не нарушаем:** контракт `shared/schema.py` (изменения только аддитивные-опциональные, без bump CONTRACT_VERSION), агент stdlib/zero-deps/WinPS5.1, «только RFC1918 покидает агента», «никаких приватных ключей», autoescape ON, SQL параметризован, trust state=gate/weight=modulation, UNKNOWN-first.

---

## 1. NetXMS: добыча инженерных идей + приоритизация

Для каждой идеи: **ЧТО → почему хорошо/какую проблему решает → почему масштабируется → минусы → адаптация под SRP → вердикт**. Приоритет в конце.

### 1.1 Разделение типов поллинга (poll-type separation) — **MUST**
- **ЧТО:** NetXMS не «опрашивает узел», а гоняет независимые поллы: *status* (быстрый, жив ли — ICMP/SNMP-ping), *configuration* (медленный — sysObjectID, ifTable, capabilities), *instance discovery* (auto-DCI на интерфейс), *routing-table poll*, *topology poll* (LLDP/FDB/CDP), *network discovery poll* (новые адреса). У каждого свой интервал и свой пул потоков.
- **Почему хорошо:** разные данные меняются с разной скоростью. «Жив ли» нужно знать каждую минуту; «какой это вендор» — раз в сутки. Слитный поллинг заставил бы делать дорогую конфигурацию так же часто, как дешёвый ping → перегрузка сети и сервера.
- **Масштаб:** дешёвые частые поллы не блокируются дорогими редкими; пулы изолируют отказы (зависший SNMP-walk не тормозит ping-цикл).
- **Минусы:** больше движущихся частей, нужна координация состояния между поллами одного узла.
- **Адаптация SRP:** идеально ложится на `lifespan`-loop из `main.py` (`_printer_poll_loop`). Делаем 3–4 независимых async-петли: `reachability` (быстрая), `classify` (редкая), `topology` (средняя), `discovery` (средняя). Каждая = отдельный `asyncio.create_task`, общий `app.state.netdisco_config`.
- **Вердикт: MUST.** Дёшево внедрить (паттерн уже есть), даёт главный выигрыш масштаба.

### 1.2 Дуальность active + passive discovery — **MUST**
- **ЧТО:** активное — ICMP/ARP-sweep диапазона; пассивное — *сбор адресов из уже известных узлов*: ARP-кэши, IP-routing-таблицы, IP-neighbor (ND), LLDP/CDP соседи. Пассивное находит то, что не пингуется (firewall'нутое), и масштабируется без флуда.
- **Почему хорошо:** один опрошенный по SNMP коммутатор «выдаёт» десятки хостов из своей FDB/ARP бесплатно — на порядок дешевле, чем пинговать /16, и находит молчаливые хосты.
- **Масштаб:** пассив растёт ~линейно от числа инфра-устройств, а не от размера адресного пространства. Active-sweep ограничивается /24-сегментом.
- **Минусы:** пассив требует SNMP-доступа к инфраструктуре (community); качество зависит от свежести ARP-кэшей.
- **Адаптация SRP:** SRP уже делает «пассив бедняка» — ARP-соседи агентов в `network_neighbors`. Добавляем **второй пассивный источник**: SNMP-walk `ipNetToMediaTable`/`ipNetToPhysical` (ARP) и `ipCidrRouteTable` с инфра-устройств → новые адреса-кандидаты. Active-sweep = уже готовый `printers/scan.py`, обобщённый (порты не только принтерные).
- **Вердикт: MUST.** Пассив — главный множитель охвата при минимуме трафика; прямо отвечает на «автоматическое обнаружение сети».

### 1.3 L2-топология примирением источников (LLDP/CDP/FDB/STP) — **MUST**
- **ЧТО:** физические связи «порт↔порт» NetXMS строит из нескольких MIB: **LLDP** (`lldpRemTable`, стандарт, авторитетно), **CDP** (Cisco), **FDB/bridge** (`dot1dTpFdbTable` — какие MAC за каким портом), **STP** (`dot1dStp` — root/designated порты для разрешения «кто выше»). Противоречия разрешаются приоритетом: LLDP > CDP > FDB-вывод.
- **Почему хорошо:** ни один протокол не покрывает всё (LLDP выключен на дешёвых свитчах; немые хосты не говорят LLDP). FDB-вывод даёт связи даже к немым устройствам: порт, за которым ровно один не-инфраструктурный MAC = edge-линк к этому хосту.
- **Масштаб:** walk FDB — O(число MAC), линейно; делается в topology-полле, отдельном от быстрых поллов.
- **Минусы:** FDB-вывод хрупок на trunk/uplink (много MAC за портом) и требует «знания» какие MAC — инфраструктурные; STP-данные нужны для развязки.
- **Адаптация SRP:** это ядро Topology Builder (§4.3). Реализуем `evidence`-модель: каждая улика связи = `(a_endpoint, b_endpoint, source, confidence)`; reconcile выбирает победителя. SRP-философия «UNKNOWN over false confidence» → при недостатке улик связь помечается `inferred/low-confidence`, не «факт».
- **Вердикт: MUST.** Это и есть «определение соседей/связей/интерфейсов». FDB-вывод — нестандартный сильный алгоритм.

### 1.4 Абстракция драйверов сетевых устройств (sysObjectID → driver) — **SHOULD**
- **ЧТО:** NetXMS по `sysObjectID` (enterprise-OID вендора) выбирает *driver* — класс, знающий квирки вендора: как читать топологию, какие OID для интерфейсов/VLAN, как чинить кривой ifTable.
- **Почему хорошо:** вендоры нарушают стандарты; драйвер изолирует «грязь» от ядра. Открытая точка расширения.
- **Масштаб:** добавить вендор = добавить драйвер, ядро не трогаем (Open/Closed).
- **Минусы:** нужна таблица OID→driver и поддержка.
- **Адаптация SRP:** **уже реализовано** в `server/printers/drivers/*` (standard + hp/canon/epson/…, `vendor.py` диспетчер). Обобщаем тот же паттерн до `server/netdisco/drivers/` со `standard` драйвером (host-MIB/bridge-MIB/LLDP стандартно) и вендор-оверлеями по мере верификации на железе. До верификации — пусто (как сделано в принтерных OID-картах: «empty until hardware-verified»).
- **Вердикт: SHOULD.** v1 — только `standard`-драйвер; вендор-драйверы добавляются по факту наличия железа (иначе спекулятивно).

### 1.5 Reachability-корреляция событий (unreachable vs down) — **MUST**
- **ЧТО:** NetXMS до объявления узла DOWN проверяет *достижимость пути* до него. Если путь идёт через упавший роутер — узел помечается UNREACHABLE (не DOWN), его алармы подавляются, поднимается **один** root-cause-аларм на роутере.
- **Почему хорошо:** убирает alarm-storm. Один сбой аплинка иначе порождает сотни ложных «узел упал». Оператор видит причину, а не симптомы.
- **Масштаб:** требует графа топологии (см. 1.3) — переиспользует его, новых дорогих опросов не плодит.
- **Минусы:** нужен достоверный граф; неверный граф → неверное подавление.
- **Адаптация SRP:** SRP уже имеет зачаток в `netmap._finalize`: «≥60% подсети теряют пакеты до шлюза → инфраструктура, не ПК». Поднимаем с эвристики «по подсети» до «по графу»: если шлюз/аплинк недостижим, помечаем за-ним устройства `unreachable` и аннотируем причину. Кладётся в Event Correlation (§3.7), вписывается в существующий Score100/`network_risk` как модулятор уверенности (blind-spot, не ложная тревога — принцип D5 SRP).
- **Вердикт: MUST.** Прямо отвечает на бриф «выявление связей/корень проблемы» и переиспользует SRP-инвариант ICMP-честности.

### 1.6 Configuration-poll reconciliation + журнал изменений — **MUST**
- **ЧТО:** периодический config-poll сверяет текущую конфигурацию узла с сохранённой; добавление/удаление интерфейса, смена IP/имени/прошивки логируются как change-события.
- **Почему хорошо:** «что изменилось» — это и есть мониторинг. Новый интерфейс/исчезнувший сосед/смена прошивки = сигнал.
- **Масштаб:** diff — O(размер конфигурации узла), редкий полл.
- **Минусы:** нужен стабильный идентификатор объектов (иначе «переименование» = удаление+добавление).
- **Адаптация SRP:** прямое отображение на «выявление изменений / удаление исчезнувших». Реализуем `change-detection` (§3.13) как diff между предыдущим и текущим `TopologySnapshot`; устойчивый identity (§4.1) убирает ложные дельты. Зеркалит append-only паттерн SRP (история = серия снимков).
- **Вердикт: MUST.** Бриф требует change-detection явно.

### 1.7 Object-model со статус-пропагацией по иерархии — **SHOULD**
- **ЧТО:** `NetObj → Node → Interface`, `Subnet`, `Container`, `Cluster`; статус считается снизу (DCI-порог → статус интерфейса) и пропагируется вверх (худший ребёнок красит родителя).
- **Почему хорошо:** единая модель для любого объекта; статус агрегируется естественно.
- **Масштаб:** пропагация по дереву — O(узлы).
- **Минусы:** полноценная ОО-иерархия — тяжёлый каркас, для SRP избыточен.
- **Адаптация SRP:** берём *идею* агрегации статуса, не классовую иерархию. SRP уже имеет Score100-агрегацию. Делаем плоские dataclass-модели (`NetDevice`/`NetInterface`/`NetLink`) + функция-агрегатор статуса устройства из его интерфейсов/линков. Без `NetObj`-базы (YAGNI).
- **Вердикт: SHOULD** (только агрегатор статуса, без ОО-каркаса).

### 1.8 Instance discovery для DCI (auto-метрики на интерфейс) — **COULD**
- **ЧТО:** walk `ifTable` → автосоздание метрик (трафик/ошибки) на каждый интерфейс без ручной настройки.
- **Почему хорошо:** ноль ручной конфигурации, покрытие масштабируется само.
- **Минусы:** SRP heartbeat — фиксированной формы; per-interface time-series — большой новый объём данных и UI.
- **Адаптация:** v1 храним per-interface *статус* (up/down, speed, errors из ifTable) в reading, без полноценных time-series-метрик. Полные DCI-метрики — будущее.
- **Вердикт: COULD** (статус интерфейсов — да; time-series на интерфейс — отложить).

### 1.9 In-memory object index + DB persistence — **SHOULD**
- **ЧТО:** «горячий» индекс объектов в памяти для O(1)-доступа, периодический flush в БД.
- **Почему хорошо:** рендер карты и корреляция читают граф много раз — из памяти быстрее, чем гонять SQLite.
- **Минусы:** инвалидация кэша, риск рассинхрона.
- **Адаптация SRP:** лёгкий TTL-кэш собранного графа (§3.15) — топология меняется медленно, кэш на 30–60с снимает повторную сборку при опросах API/UI. Источник истины — БД; кэш только read-through.
- **Вердикт: SHOULD** (простой TTL-кэш графа, не полноценный object-store).

### 1.10 Map auto-layout + сохранённые позиции — **SHOULD**
- **ЧТО:** автораскладка карты (spring/force) + ручные позиции, сохраняемые оператором.
- **Адаптация SRP:** **уже есть** force-граф на canvas в `netmap.html` (mulberry32-детерминизм, пружины/отталкивание). Переиспользуем; добавляем (а) реальные L2/L3-линки вместо gateway-кластеров; (б) опц. сохранение позиций (COULD).
- **Вердикт: SHOULD** (рендер готов, кормим настоящими линками).

### 1.11 NXSL — встроенный скриптовый язык (фильтры/трансформы/действия) — **DROP (v1)**
- **ЧТО:** Turing-полный DSL внутри сервера для discovery-фильтров, threshold-логики, действий.
- **Почему хорошо у них:** максимальная гибкость без перекомпиляции.
- **Минусы для SRP:** огромная поверхность (парсер/sandbox/безопасность), нарушает SRP-минимализм («scope ceiling: no nested calculus»). Безопасность скриптового движка — отдельный проект.
- **Адаптация:** заменяем декларативной конфигурацией + чистыми Python-предикатами (discovery-filter = функция `accept(ip, evidence) -> bool` из конфигурации: CIDR-include/exclude, OUI-allow/deny). 95% пользы за 1% сложности.
- **Вердикт: DROP.** Декларативный фильтр вместо движка.

### 1.12 Agent subagent/loadable-module система — **DROP**
- **ЧТО:** агент NetXMS грузит .so/.dll-модули-сборщики.
- **Минусы для SRP:** ломает железобетонный инвариант «`client/` = чистый stdlib, zero deps». Плагин-лоадер = деп + поверхность атаки.
- **Вердикт: DROP.** SRP-агент остаётся монолитным stdlib.

### 1.13 SNMP-trap / syslog receiver (push-приём) — **COULD/DROP**
- **ЧТО:** сервер слушает trap'ы/syslog — мгновенные события вместо поллинга.
- **Почему хорошо:** реактивность (линк упал — trap пришёл за секунды).
- **Минусы:** новый сетевой listener (UDP 162/514) = поверхность атаки + парсинг недоверенного ввода; противоречит SRP «всё через ingest-токен».
- **Вердикт: COULD (поздно).** v1 — только поллинг. Если добавлять — отдельный bounded UDP-listener с теми же anti-spoof-гардами, что в `snmp.py`. Отложено за горизонт 12 фаз.

### 1.14 Address-zones (перекрывающиеся IP / мультитенант) — **DROP (v1)**
- **ЧТО:** «зоны» для перекрывающихся RFC1918 разных площадок.
- **Минусы:** SRP сейчас single-trusted-network (CONTINUITY: «multi-tenant — anti-goal»). YAGNI.
- **Адаптация:** identity несёт `site_code` (уже в контракте) как мягкий разделитель — на будущее, без зон-движка.
- **Вердикт: DROP** (site_code-namespace достаточно).

### 1.15 Bounded poller pools / очереди (не thread-per-node) — **MUST**
- **ЧТО:** фиксированные пулы воркеров + очередь задач, не поток на узел.
- **Адаптация SRP:** **уже есть** — `ThreadPoolExecutor(max_workers=…)` + `_poll_lock` в `printers/scheduler.py`. Переиспользуем дословно как паттерн.
- **Вердикт: MUST** (тривиально, уже доказано).

### Сводная приоритизация

| Идея | Приоритет | Где в архитектуре |
|---|---|---|
| 1.1 Poll-type separation | **MUST** | Discovery Scheduler §3.6 |
| 1.2 Active+Passive discovery | **MUST** | Network Discovery §3.1 |
| 1.3 L2 reconcile (LLDP/CDP/FDB/STP) | **MUST** | Topology Builder §3.2 / §4.3 |
| 1.5 Reachability-корреляция | **MUST** | Event Correlation §3.7 |
| 1.6 Config-reconcile + change-log | **MUST** | Change Detection §3.13 |
| 1.15 Bounded pools | **MUST** | Background Tasks §3.14 |
| 1.4 Драйверы устройств | **SHOULD** | Classification §4.2 / drivers |
| 1.7 Status-пропагация | **SHOULD** | Graph Engine §3.5 |
| 1.9 In-mem кэш графа | **SHOULD** | Caching §3.15 |
| 1.10 Map layout | **SHOULD** | Map Renderer §3.11 |
| 1.8 Instance discovery DCI | **COULD** | History §3.12 (статус да, метрики нет) |
| 1.13 Trap/syslog receiver | **COULD** | за горизонтом |
| 1.11 NXSL | **DROP** | → декларативный фильтр |
| 1.12 Agent module-loader | **DROP** | инвариант stdlib |
| 1.14 Address-zones | **DROP** | site_code достаточно |

---

## 2. SRP сегодня: что есть / менять / удалять / добавить / не трогать

### 2.1 Что уже существует (переиспользуем)
- **Контракт** `shared/schema.py`: `Envelope` + `HistoricalPayload.{network_adapters,network_neighbors,network_connections,network_quality}` — агент уже шлёт L2/L3 улики. Аддитивно-опциональная политика (`extra="allow"`, без bump).
- **Агент** `client/collectors/network.py`: adapters/ARP/conns/quality через WinPS5.1, RFC1918-only, ifType→kind.
- **stdlib-SNMP** `server/printers/{snmp,ber}.py`: v1/v2c GET/GETNEXT/walk, `SnmpSession`, anti-spoof (request-id+source-IP), таймауты, мусор→{}. **Crown jewel переиспользования.**
- **Active-scan** `server/printers/scan.py`: RFC1918-only, capped, ThreadPool, injectable host_check. Уже owner-authorized (memory `printer-active-scan-authorized`).
- **Discovery-merge** `server/printers/discovery.py`: union/dedup, `is_rfc1918`/`is_rfc1918_cidr`.
- **Scheduler** `server/printers/scheduler.py`: anti-DoS lock, pool, ghost-handling (synthetic unreachable reading).
- **Драйверы** `server/printers/drivers/*` + `oids.py`: sysObjectID→driver, OID-карты (паттерн классификации).
- **Read-side карта** `server/analytics/netmap.py`: gateway-кластеры, agent-MAC identity, OUI-вендор, subnet-anomaly.
- **OUI** `server/analytics/oui.py`: `normalize_mac`, `vendor_for_mac`.
- **Network-risk** `server/analytics/network_risk.py`: ось `network_risk` (gateway loss/lat, APIPA, Wi-Fi, DNS), ICMP-честность.
- **Storage-паттерн** `printers`/`printer_readings`: COALESCE-инвентарь + append-only readings + latest-by-id.
- **App-wiring** `server/main.py` (lifespan poll-loop, `app.state`), `server/api.py` (`/api/v1`, `/netmap`, `/printers`), `server/web/*` (Jinja2 autoescape, canvas force-граф, `srpEsc`/`tojson` XSS-гард).
- **Trust/Score100**: state=gate, weight=modulation, UNKNOWN-first, Score100-конверт.

### 2.2 Что менять (минимально-инвазивно)
- `server/main.py`: добавить netdisco poll-петли в `lifespan` + `app.state.netdisco_config` (как `printer_poll`).
- `server/api.py`: добавить роуты `/api/v1/topology`, `/topology/graph`, `/netdisco/devices`, `/discovery/poll`.
- `server/db.py`: добавить новые таблицы (раздел §5.2) + store/get-функции (зеркало принтерных). Существующие таблицы НЕ трогаем.
- `server/config.py` (`ServerConfig`): добавить `netdisco_config()` + флаг `netdisco_enabled` (по умолчанию OFF, как `printer_poll_enabled`).
- `client/config.json` (template): добавить опц. секцию `netdisco` (только если делаем агент-side enrich; v1 не обязательно).
- Навигация UI (`base.html`): пункт «Топология».

### 2.3 Что удалить
- **Ничего не удаляем.** `netmap.py`/`netmap.html` остаются (обратная совместимость + текстовая/доступная форма). Новый граф-движок их *дополняет*; старая страница может позже переиспользовать новый `topology`-эндпоинт (опц., за горизонтом). Принцип: zero-regression.

### 2.4 Что добавить
Новый пакет **`server/netdisco/`** (stdlib + server-bound glue), подсистемы §3. Опц. аддитивные поля контракта (§5.1) для агент-side enrich (LLDP-локально, маршруты) — строго additive-optional, без bump.

### 2.5 Что НЕ менять (стабильные ядра — заморожены)
- `shared/schema.py` существующие поля/типы (только добавлять optional).
- `client/` инварианты: stdlib, zero-deps, WinPS5.1, RFC1918-only, no private keys.
- `server/trust/*` контракт (state/weight/collector⊥semantic).
- `server/pipeline.py` логика существующих score (netdisco подключается отдельной петлёй, **не** в `recompute_scores` hot-path — урок D7 SRP: cross-device работа не на каждый ingest).
- Существующие таблицы БД и их миграции.

### 2.6 Стабильные модули vs точки расширения
- **Стабильные (frozen API):** `printers/snmp.py`, `printers/ber.py`, `analytics/oui.py`, `trust/*`, `scoring/score100.py`, контракт. Их сигнатуры — фундамент; netdisco *вызывает*, не меняет.
- **Точки расширения:** `netdisco/drivers/` (новые вендоры), `netdisco/evidence.py` (новые источники улик), `netdisco/config.py` (фильтры/диапазоны/интервалы), discovery-source-плагины (новый пассивный источник = новая чистая функция, возвращающая кандидатов).

---

## 3. Целевая архитектура

Пакет `server/netdisco/` (по одному модулю на подсистему; <800 строк/файл, <50 строк/функция; stdlib + `server.db`-glue только там, где нужно). Поток данных:

```
            ┌─────────────────────── СЕРВЕР SRP (FastAPI + SQLite) ───────────────────────┐
agent ─┐    │  ingest (есть)                                                              │
ARP/   ├──▶ │   └─▶ historical → get_network_snapshots() ──┐                              │
adapt. │    │                                              │ (пассивный источник #1)      │
       │    │  netdisco poll-петли (lifespan):             ▼                              │
infra ─┘    │   discovery ──▶ seeds ──▶ DiscoverySources ──▶ Candidates ──▶ classify ──┐  │
(SNMP) ────▶│   (active scan §3.1 + passive SNMP ARP/route §3.1)                        │  │
            │                                                                            ▼  │
            │   topology poll ──▶ EvidenceCollector (LLDP/FDB/CDP/route/ARP) ──▶ Fusion ──▶ Graph
            │                                                                            │  │
            │   reachability poll ──▶ status ──────────────────────────────────────────┤  │
            │                                                                            ▼  │
            │   Persist: net_devices / net_interfaces / net_links / net_topology_snapshots  │
            │                          │                    │                               │
            │            Change-Detection (diff снимков)    Caching (TTL граф)              │
            │                          │                    │                               │
            │   API /api/v1/topology · /netdisco/devices ───┴──▶ Web UI (карта + инвентарь) │
            └──────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Network Discovery (`netdisco/discovery.py`, `netdisco/scan.py`)
- **Назначение:** собрать множество *адресов-кандидатов* RFC1918 из всех источников, отфильтровать, передать дальше.
- **Источники (DiscoverySource — чистая функция `() -> list[Candidate]`):**
  1. **Passive — agent ARP/adapters** (есть): `db.get_network_snapshots()` → соседи + сами агенты.
  2. **Passive — SNMP ARP/route harvest** (новое): для каждого известного инфра-устройства walk `ipNetToMediaPhysAddress` (1.3.6.1.2.1.4.22) / `ipNetToPhysicalPhysAddress` + `ipCidrRouteTable`/`ipRouteTable` → next-hop и привязанные MAC/IP. Даёт новые адреса без пинга.
  3. **Active — bounded scan** (обобщённый `printers/scan.py`): ICMP-эквивалент (TCP-connect к набору «живых» портов) + SNMP-ping (`sysObjectID`) по RFC1918-/24. Порты обобщаются (не только 9100/631): добавить 22/80/443/445/161 как «хост жив» сигнал. **Все safety-рейлы scan.py сохраняются** (RFC1918-only double-check, cap hosts, cap workers, короткие таймауты, OFF-by-default, kill-switch `max_hosts=0`).
  4. **Static** — список из конфигурации (RFC1918-filtered).
- **Discovery-filter (замена NXSL):** `netdisco/filter.py` — чистый предикат `accept(candidate, config) -> bool`: include/exclude CIDR, OUI allow/deny, RFC1918-гард. Декларативно из `NetdiscoConfig`.
- **Merge/dedup:** обобщённый `discovery.merge` (identity-precedence chassis-id/serial > MAC > IP), несёт `sources` для приоритизации опроса.
- **Альтернатива (отклонена):** агент сам сканирует сегмент. Отклонено — раздувает агент, копирует трафик на каждой машине, нарушает «тонкий агент» и rate-limit (N агентов × scan = шторм). Сервер-центрично: один контролируемый сканер.

### 3.2 Topology Builder (`netdisco/topology.py`, `netdisco/evidence.py`)
- **Назначение:** из улик построить рёбра графа (L2-линки, L3-смежности).
- **EvidenceCollector:** на topology-полле для каждого SNMP-устройства собирает:
  - **LLDP** `lldpRemTable` (1.0.8802.1.1.2.1.4.1.1) → (локальный порт ↔ удалённый chassis/port). Авторитет высший.
  - **CDP** `cdpCacheTable` (1.3.6.1.4.1.9.9.23) → Cisco-соседи. Авторитет высокий.
  - **FDB/bridge** `dot1dTpFdbPort` (1.3.6.1.2.1.17.4.3.1.2) + `dot1dBasePortIfIndex` → MAC↔порт. Вывод edge-линков (§4.3).
  - **ARP/ipNetToMedia** → IP↔MAC (для L3 и для разрешения FDB-MAC в IP).
  - **Routing** `ipCidrRouteTable` → L3 next-hop рёбра (роутер↔роутер/подсеть).
  - **ifTable** (1.3.6.1.2.1.2.2) → интерфейсы (ifIndex/ifType/ifSpeed/ifOper/ifPhysAddress).
- **Каждая улика** = `LinkEvidence(a, b, source, confidence, observed_at)` (frozen dataclass). Не «связь», а «свидетельство связи».
- **Альтернатива (отклонена):** хранить только финальные линки без улик. Отклонено — теряем объяснимость и возможность примирения/деградации (SRP-инвариант «объясни почему»: dashboard показывает factors/source_lineage).

### 3.3 Topology Storage (`server/db.py` новые таблицы — §5.2)
- `net_devices` (COALESCE-инвентарь, зеркало `printers`), `net_interfaces`, `net_links` (рёбра с источником/уверенностью), `net_device_readings` (append-only снапшоты состояния), `net_topology_snapshots` (append-only снимок всего графа для истории/diff).
- **Identity-стабильность** (§4.1) — PK устройства устойчив к смене IP (DHCP).
- **Почему персистентно (vs текущий ephemeral netmap):** история, change-detection, «исчезнувшие устройства», нагрузка (не пересобирать граф на каждый GET). Прямой ответ на «хранение топологии / обновление / удаление исчезнувших».

### 3.4 Data Fusion Engine (`netdisco/fusion.py`)
- **Назначение:** агрегировать улики из разных источников/моментов в **один** граф с разрешением конфликтов.
- **Алгоритм (§4.4):** группировка улик по нормализованной паре endpoint'ов; выбор победителя по приоритету источника (LLDP>CDP>FDB>ARP-эвристика) и свежести; присвоение `confidence` ребру; идентификация узлов слиянием улик (один chassis-id из LLDP = один узел, даже если виден под разными IP).
- **Принцип SRP:** конфликт без явного победителя → ребро `low-confidence/ambiguous`, не выдуманное. «UNKNOWN over false confidence».

### 3.5 Graph Engine (`netdisco/graph.py`)
- **Назначение:** операции над графом: связность, путь до шлюза/корня, агрегация статуса, поиск root-cause.
- **Структура:** in-memory adjacency (dict), построенная из `net_devices`+`net_links` (чистая функция `build_graph(devices, links) -> Graph`).
- **Операции:** `neighbors(node)`, `path_to_root(node, roots)`, `reachable_from(roots)` (BFS), `status_rollup(device)` (худший интерфейс/линк), `find_root_cause(down_set)` (наивысший общий предок по путям к корню).
- **Масштаб:** граф LAN — сотни-тысячи узлов; BFS O(V+E) дёшев; кэш §3.15.

### 3.6 Discovery Scheduler (`netdisco/scheduler.py` + петли в `main.py`)
- **Реализация poll-type separation (1.1):** независимые async-петли, каждая `asyncio.to_thread`-оборачивает блокирующий SNMP/scan:
  - `reachability_loop` — быстрый (default 120с): жив ли каждый known-device (ICMP/SNMP-ping).
  - `topology_loop` — средний (default 900с): EvidenceCollector + Fusion + persist snapshot + change-detect.
  - `classify_loop` — редкий (default 3600с): sysObjectID/sysServices/ifTable → тип/вендор.
  - `discovery_loop` — средний (default 1800с): active scan (если enabled) + passive harvest → новые кандидаты.
- **Rate-limit / jitter / adaptive (SAFETY-требование):**
  - Каждая петля: `interval = base + random.uniform(0, jitter)` (расфазировка, anti-thundering-herd).
  - Глобальный `_poll_lock`-эквивалент на тяжёлые циклы (одна активная fan-out за раз — как `printers/scheduler`).
  - Token-bucket на исходящие probe (cap probes/сек), cap parallelism, короткие per-probe таймауты.
  - **Adaptive backoff:** если устройство N раз подряд недостижимо → реже опрашивать (экспоненциальный backoff до cap); ожило → вернуть базовый интервал. Снижает шум и трафик.
- **Все интервалы — clamp снизу** (как `_MIN_INTERVAL_SEC=60` в printers): что бы ни стояло в конфиге, не чаще минимума.

### 3.7 Event Correlation (`netdisco/correlation.py`)
- **Reachability-корреляция (1.5):** на reachability-полле строим `down_set`; через Graph Engine классифицируем каждый down как `DOWN` (сам упал) либо `UNREACHABLE` (путь к корню пересекает down-узел). Поднимаем один root-cause на верхний down-узел; downstream — `suppressed`.
- **Интеграция со SRP:** результат — read-side аннотация (как `subnet_context_for`), плюс модулятор `network_risk`-confidence (недостижимость = blind-spot/MEDIUM-cap, не ложная тревога — D5). НЕ создаём новый «alarm-движок» (scope ceiling) — переиспользуем Score100/factors/reason.
- **Change-correlation:** дельты топологии (§3.13) — отдельный класс событий «появилось/исчезло/линк сменился», в журнал изменений (не алармы).

### 3.8 Network Inventory (`netdisco/inventory.py`)
- Персистентный список устройств: agent / infrastructure(router/switch/AP) / endpoint / unknown. Поля: identity, тип, вендор(OUI/sysObjectID), модель, первый/последний раз виден, интерфейсы, статус, источники-обнаружения.
- **Managed vs unmanaged** (1.x): устройство без SNMP-доступа = `unmanaged` (виден как endpoint из ARP/FDB, без деталей) — честно, не «пустое».

### 3.9 API (`server/api.py` доп. роуты, read-only)
- `GET /api/v1/netdisco/devices` — инвентарь (+ фильтры type/site/days).
- `GET /api/v1/netdisco/devices/{id}` — одно устройство + интерфейсы + линки + reading-серия.
- `GET /api/v1/topology/graph` — узлы+рёбра для рендера (из кэша §3.15).
- `GET /api/v1/topology/changes?days=N` — журнал изменений.
- `POST /api/v1/discovery/poll` — форс-цикл (bounded, `_poll_lock`-guard, как `/printers/poll`).
- Все: параметризованный SQL, clamp days, JSON-envelope-формат проекта.

### 3.10 Web UI (`server/web/templates/topology.html` + route)
- Страница «Топология»: SSR-таблица инвентаря (доступная/текстовая форма — как netmap) + canvas-граф поверх (переиспользуем движок `netmap.html`). RU-prose, Latin tech-термины. autoescape ON, agent-строки через `srpEsc`/`tojson`.
- Карточка устройства: тип/вендор/интерфейсы/линки/статус/история. Журнал изменений.

### 3.11 Map Renderer (переиспользование `netmap.html`-движка)
- Тот же canvas force-граф (mulberry32-детерминизм, пружины/отталкивание, темы, reduced-motion). Изменение: рёбра = настоящие L2/L3-линки (цвет по confidence/типу), узлы по `type` (router=ромб, switch=квадрат, AP=треугольник, agent=круг, endpoint=точка). Опц.: пульсы по линкам деградации (D-style как сейчас).

### 3.12 History (`net_device_readings`, `net_topology_snapshots`)
- Append-only (паттерн W0.1): per-device readings + полные снимки графа (для diff/истории/«как было»). Retention-cap per-device (как `_retain_*`).

### 3.13 Change Detection (`netdisco/changes.py`)
- `diff(prev_snapshot, curr_snapshot) -> list[TopologyDelta]`: устройство appeared/disappeared, линк added/removed/changed, интерфейс up→down, IP/имя/прошивка сменились, тип переклассифицирован.
- **Исчезнувшие устройства:** не удаляем сразу (urok device-ghost-cleanup) — помечаем `last_seen`-stale → `missing` (N циклов) → eligible for purge (sweep + ручной ✕ в UI). Прямой ответ на «удаление исчезнувших».
- Identity-стабильность (§4.1) гарантирует, что DHCP-смена IP ≠ «исчез+появился».

### 3.14 Background Tasks (`main.py` lifespan)
- Петли §3.6 как `asyncio.create_task`, отменяются на shutdown (паттерн `_printer_poll_loop`). Все блокирующие операции через `asyncio.to_thread`. Bounded pools внутри.

### 3.15 Caching (`netdisco/cache.py`)
- TTL-кэш собранного графа (`build_graph` дорого при больших флотах): `(value, expires_at)`, read-through, инвалидация по TTL (30–60с) и по факту нового topology-снимка. Источник истины — БД. Потокобезопасно (`threading.Lock`).

### 3.16 Telemetry (`netdisco/metrics.py` + logging)
- Внутренние счётчики цикла: candidates_found, hosts_probed, probes_sent, snmp_ok/timeout, links_inferred, deltas, cycle_duration. Лог через `logging.getLogger("srp.netdisco")` (как `srp.printers`). Экспонируется в `/api/v1/netdisco/stats` (наблюдаемость самого сканера — SRP ценит observability). Никаких внешних APM.

### 3.17 Configuration (`netdisco/config.py`)
- `NetdiscoConfig` (frozen dataclass, зеркало `PrinterConfig`): `enabled=False` (OFF by default), интервалы (4 типа поллов, clamp снизу), jitter, `active_scan=False` (отдельный stop-gate, RFC1918-CIDR-filtered), `scan_cidrs`, `scan_max_hosts`, `scan_ports`, parallelism caps, probe-rate cap, snmp credential-ref (см. §3.18), include/exclude CIDR, OUI allow/deny.
- Загрузка из `client/config.json`-аналога серверного конфига (`ServerConfig`), валидация/clamp на входе (как `load_printer_config`).

### 3.18 Credentials (`netdisco/credentials.py`) — SAFETY
- **Требование брифа:** секреты (SNMP community v2c, в будущем SNMPv3 user/pass) — через безопасное хранилище ОС, не plaintext.
- **Дизайн:** `CredentialStore`-абстракция:
  - **Windows:** DPAPI (`win32crypt`/`CryptProtectData`) **или** Credential Manager. Но сервер тоже должен оставаться без тяжёлых deps → используем stdlib `ctypes`-обёртку над `CryptProtectData`/`CryptUnprotectData` (DPAPI, machine-scope) для шифрования секрет-файла. Никаких сторонних пакетов.
  - **Fallback:** только для community=`public` (read-only, не секрет) допускается plaintext в конфиге.
  - Секрет на диске — только в DPAPI-зашифрованном виде; в логи/дашборд/ответы API community **не попадает** никогда.
- **SNMPv3** (COULD): структура `CredentialRef` готова к user/authKey/privKey, но v1 — community v2c. Приватные ключи устройств не читаем (инвариант), храним только свои SNMP-креды.
- **Альтернатива (отклонена):** community в `config.json` plaintext (как сейчас у принтеров `public`). Для дефолтного `public` ок, но для непубличной community нарушает бриф → DPAPI-store.

---

## 4. Особое внимание: алгоритмы (детально)

### 4.1 Identity сетевого устройства (`netdisco/identity.py`)
Зеркало `printer_identity`, расширенное приоритетом:
```
chassis_id (LLDP lldpLocChassisId / SNMP-engineID)   # стабильнее всего
  > serial (entPhysicalSerialNum)
  > mac (нормализованный, базовый/наименьший MAC устройства)
  > ip
  → "nd-<scheme>-<value>"; ничего нет → "nd-unknown"
```
- MAC нормализуется (`oui.normalize_mac`): один адаптер → один id при любом регистре/разделителях.
- Устойчивость к DHCP: chassis/serial/MAC переживают смену IP → нет ложных «исчез+появился».
- **Слияние improvement:** если позже узнан более «сильный» идентификатор (был ip-only, стал mac), запись *мигрирует* под новый id с переносом истории (как clone-safe device_id) — детерминированно, без дублей.

### 4.2 Классификация устройства (`netdisco/classify.py`)
Определительные сигналы (по убыванию надёжности), результат `type ∈ {router, switch, ap, server, printer, agent, endpoint, unknown}`:
1. **agent** — MAC устройства = MAC известного SRP-агента (identity-layer netmap). Высший приоритет, 100% (это «наша» машина).
2. **router** — `ipForwarding=1` (1.3.6.1.2.1.4.1) ИЛИ непустая `ipCidrRouteTable` с >1 сетью ИЛИ sysServices bit L3(4) И есть routing. (NetXMS: sysServices/ipForwarding.)
3. **switch** — наличие bridge-MIB (`dot1dBaseBridgeAddress` 1.3.6.1.2.1.17.1.1) И непустая FDB И НЕ router. sysServices bit L2(2).
4. **ap** — bridge-MIB + беспроводные OID (IEEE 802.11 MIB `dot11`) ИЛИ вендор-AP sysObjectID (вендор-драйвер). Иначе классифицируется как switch (честная деградация).
5. **printer** — переиспользуем `printers/classify.is_printer` (Printer-MIB/hrDevicePrinter).
6. **server/endpoint** — hrDeviceType host-resources (general-purpose comp) ИЛИ нет SNMP, но виден в FDB/ARP. SNMP-немой = `endpoint`/`unmanaged`.
7. **unknown** — улик не хватает. **UNKNOWN > ложная классификация** (SRP-инвариант): vendor-enterprise OID сам по себе НЕ тип (как в принтерах: «HP делает и не-принтеры»).
- sysServices (1.3.6.1.2.1.1.7) — битовая маска уровней; numeric, language-independent (SRP-инвариант).

### 4.3 L2-линк-вывод из FDB (`netdisco/l2.py`) — нестандартный алгоритм
Цель: связи к немым хостам без LLDP. Вход: для свитча S — `port → set(MAC)` из FDB; `port → ifIndex`; множество известных infra-MAC.
```
для каждого порта p свитча S:
  macs = FDB[p] минус мультикаст/широковещательные минус own-MAC свитча
  если |macs| == 0: пропустить (пустой/чистый порт)
  classify_port:
    если macs ∩ infra_macs ≠ ∅ ИЛИ |macs| > UPLINK_MAC_THRESHOLD (напр. 4):
        порт = UPLINK/TRUNK  → кандидат на связь свитч↔свитч (разрешается через STP/LLDP)
    иначе если |macs| == 1:
        m = единственный MAC → EDGE-линк S:p ↔ host(m)   (сильная улика, confidence HIGH)
    иначе (2..threshold не-инфра MAC):
        порт = AMBIGUOUS (хаб/неуправляемый свитч за портом) → улики LOW per host
```
- **Разрешение uplink↔uplink:** если порт обоих свитчей видит MAC друг друга (или STP designated/root указывает направление) → одна связь S1↔S2. STP `dot1dStpPortDesignatedBridge` развязывает «кто выше».
- **Примирение с LLDP/CDP:** если для S:p есть LLDP-сосед — он побеждает FDB-вывод (Fusion §4.4).
- **Почему сильно:** даёт топологию на дешёвых свитчах без LLDP и к немым хостам; именно то, чего нет в наивных мониторингах.
- **Минус/защита:** trunk'и шумят → порог `UPLINK_MAC_THRESHOLD` + пометка confidence; ambiguous-порты не плодят ложные edge-линки (UNKNOWN-first).

### 4.4 Data Fusion (`netdisco/fusion.py`)
```
вход: list[LinkEvidence] (из всех источников, возможно за несколько циклов)
1. нормализовать endpoints → стабильные node-id (§4.1); слить узлы по chassis-id.
2. сгруппировать улики по frozenset({node_a, node_b}).
3. для каждой группы:
     winner = max(улики, key=(SOURCE_PRIORITY[source], freshness))
     confidence = f(source победителя, согласованность, свежесть)
     если конфликт топологии (один порт ↔ два разных соседа из равных источников):
         confidence = LOW, пометить ambiguous (не выкидывать — показать обе)
4. вернуть list[ResolvedLink(a,b,confidence,via_source,observed_at)]
SOURCE_PRIORITY = {lldp:5, cdp:4, fdb_edge:3, route:3, arp:2, fdb_ambiguous:1}
```
- Узловое слияние: LLDP chassis-id «склеивает» один узел, видимый под разными IP/портами.
- Детерминизм (для тестов): tie-break по (source_priority, observed_at, sorted node-ids).

### 4.5 Обновление топологии и удаление исчезнувших (`netdisco/reconcile.py`)
```
каждый topology-цикл:
  curr = Fusion(EvidenceCollector(all_known_devices))
  persist net_topology_snapshots(curr)               # append-only история
  upsert net_devices (COALESCE identity, advance last_seen)   # как store_printer_reading
  replace net_links для участвовавших узлов
  deltas = changes.diff(prev_snapshot, curr)          # §3.13
  для каждого device: если last_seen старше STALE_CYCLES*interval → status=missing
                      если старше PURGE_AGE → eligible_purge (sweep/manual ✕)
```
- Никогда не удаляем по одному промаху (ghost-cleanup lesson). «Исчез» = устойчивое отсутствие через N циклов.

### 4.6 Агрегация из разных источников (общий принцип)
Каждое свойство устройства (vendor, type, hostname, links) может прийти из ≥2 источников. Правило слияния = **COALESCE по приоритету источника + не затирать known транзиентным unknown** (точно как `store_printer_reading` COALESCE identity, latest-wins status). Это и есть «агрегация информации из разных источников».

---

## 5. Контракты и модель данных

### 5.1 Изменения контракта (`shared/schema.py`) — ТОЛЬКО аддитивные-опциональные
v1 server-only **не требует** изменений контракта (работает на существующих `network_adapters/neighbors`). Опц. агент-side enrich (за горизонт ядра, отдельная фаза) добавляет в `HistoricalPayload`:
```python
# Аддитивно-опционально, extra="allow", БЕЗ bump CONTRACT_VERSION (правило §5 SRP):
local_lldp: list[LldpNeighborHint] = Field(default_factory=list, max_length=NET_LLDP_MAX)  # 64
ip_routes:  list[RouteHint]        = Field(default_factory=list, max_length=NET_ROUTES_MAX) # 128
```
где `LldpNeighborHint{local_if, chassis_id, port_id, sys_name}`, `RouteHint{dest_cidr, next_hop, if_index}` — всё RFC1918-фильтровано в агенте, numeric/Latin only, WinPS5.1 (`Get-NetNeighbor`/`Get-NetRoute`/LLDP при наличии). Каждое поле — Optional, старый сервер игнорирует (`extra="allow"`), старый агент не шлёт → None. **Без bump** (additive-optional, правило подтверждено в ledger).

### 5.2 Новые таблицы БД (`server/db.py` `_SCHEMA` дополнение)
Зеркало паттерна `printers`/`printer_readings` (COALESCE-инвентарь + append-only). PK устойчив (§4.1).
```sql
CREATE TABLE IF NOT EXISTS net_devices (
  device_nid    TEXT PRIMARY KEY,   -- §4.1 identity (nd-...)
  ip            TEXT,
  hostname      TEXT,
  mac           TEXT,
  vendor        TEXT,
  dev_type      TEXT,               -- router/switch/ap/agent/printer/endpoint/unknown
  sys_object_id TEXT,
  model         TEXT,
  serial        TEXT,
  site_code     TEXT,
  status        TEXT,               -- up/down/unreachable/missing
  first_seen    TEXT,
  last_seen     TEXT
);
CREATE TABLE IF NOT EXISTS net_interfaces (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_nid  TEXT,
  if_index    INTEGER,
  name        TEXT,
  if_type     INTEGER,
  speed_mbps  REAL,
  oper_up     INTEGER,              -- 0/1/NULL
  phys_mac    TEXT,
  last_seen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_netif_device ON net_interfaces(device_nid);
CREATE TABLE IF NOT EXISTS net_links (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  a_nid         TEXT,
  b_nid         TEXT,
  a_if          INTEGER,            -- ifIndex со стороны A (NULL если неизвестен)
  b_if          INTEGER,
  link_kind     TEXT,               -- l2-edge/l2-trunk/l3-route/wifi
  via_source    TEXT,               -- lldp/cdp/fdb_edge/route/arp
  confidence    TEXT,               -- high/medium/low
  first_seen    TEXT,
  last_seen     TEXT
);
CREATE INDEX IF NOT EXISTS idx_netlink_a ON net_links(a_nid);
CREATE INDEX IF NOT EXISTS idx_netlink_b ON net_links(b_nid);
CREATE TABLE IF NOT EXISTS net_device_readings (   -- append-only, как printer_readings
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_nid  TEXT,
  received_at TEXT,
  status      TEXT,
  detail      TEXT                  -- JSON: интерфейсы/улики/метрики цикла
);
CREATE INDEX IF NOT EXISTS idx_netread_device ON net_device_readings(device_nid, id);
CREATE TABLE IF NOT EXISTS net_topology_snapshots (  -- append-only граф для истории/diff
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT,
  node_count  INTEGER,
  link_count  INTEGER,
  graph       TEXT                  -- JSON {nodes:[...], links:[...]}
);
CREATE INDEX IF NOT EXISTS idx_nettopo_ts ON net_topology_snapshots(id);
CREATE TABLE IF NOT EXISTS net_changes (    -- журнал изменений (§3.13)
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT,
  device_nid  TEXT,
  kind        TEXT,                 -- appeared/disappeared/link_added/link_removed/iface_down/reclassified/...
  detail      TEXT                  -- JSON
);
CREATE INDEX IF NOT EXISTS idx_netchg_ts ON net_changes(id);
```
- Все новые таблицы `IF NOT EXISTS` → миграция бесплатна (как принтерные добавились).
- Retention prune per-device (как `_retain_*`), чтобы append-only не рос безгранично.

### 5.3 Внутренние модели (`netdisco/models.py`, frozen dataclasses)
`NetDevice`, `NetInterface`, `NetLink`, `LinkEvidence`, `ResolvedLink`, `DeviceReading`, `TopologySnapshot`, `TopologyDelta`, `Graph`. Все frozen, Optional-поля = None при UNKNOWN (никогда выдуманный 0).

---

## 6. Cross-cutting concerns

### 6.1 Безопасность / SAFETY (бриф)
- **Прозрачность:** только штатные ОС-механизмы (UDP SNMP GET, TCP-connect, ICMP-эквивалент). Никаких техник сокрытия/обхода/payload'ов сверх read-SNMP-GET и TCP-connect. Сканер логируется (telemetry §3.16) — администратор видит всё.
- **Read-only:** SNMP **только** GET/GETNEXT/walk; **никогда SET** (инвариант `snmp.py`). Это движок наблюдения, не управления.
- **RFC1918-only:** двойная проверка (CIDR + каждый хост), как в `scan.py`/`discovery.py`. Публичный адрес не может быть опрошен.
- **Rate-limit/jitter/adaptive/bounded** (§3.6): token-bucket, cap parallelism, короткие таймауты, OFF-by-default, kill-switch `max_hosts=0`, clamp интервалов снизу, exponential backoff на мёртвых.
- **Anti-DoS внутрь сервера:** `_poll_lock` на тяжёлые fan-out (как принтеры); force-poll bounded.
- **Anti-spoof:** переиспользуем `snmp._transact` (request-id + source-IP match) — отбрасывает подделанные UDP-датаграммы.
- **Агент** не получает новых полномочий: остаётся фоновой службой, без user-interaction, stdlib, RFC1918-only outbound, без приватных ключей.

### 6.2 Credentials — §3.18 (DPAPI-store, plaintext только для `public`).

### 6.3 Testing (см. план — каждая фаза с TDD)
- **Unit:** чистые модули (identity/classify/l2/fusion/graph/filter/changes) — детерминированные, инъекция входов (как `scan.py` injectable host_check). Цель ≥80% (gate).
- **Integration:** db store/get round-trip; ingest→persist; API-роуты (FastAPI TestClient).
- **Сетевые модули** (snmp/scan): инъекция фейкового `host_check`/`probe`/socket — **никогда реальная сеть в тестах** (паттерн `printers/scan.py`).
- **Security-review** (subagent, Opus) обязателен для: scan/probe (ingest/SQL/network surface), credentials, любой агент-PowerShell. `code-reviewer` (Sonnet) для остального.
- **Property/fuzz:** SNMP-парсер уже fuzz-устойчив (`_parse_message` любой мусор→{}); L2/fusion — property-тест «улики в любом порядке → один детерминированный граф».

### 6.4 Deployment
- OFF by default (`netdisco_enabled=False`, `active_scan=False`) — включается явно в серверном конфиге (как `printer_poll_enabled`). Zero-config регресс: выключено = система ведёт себя как сегодня.
- Миграция БД автоматическая (`IF NOT EXISTS`). Откат = выключить флаг (данные остаются, не мешают).
- `python smoke.py` расширяется проверкой netdisco-роутов при enabled.

### 6.5 Observability/Telemetry — §3.16 (внутренние счётчики, `/stats`, logging).

---

## 7. Обнаруженные недостатки SRP + минимально-инвазивные правки

| # | Недостаток | Влияние | Правка (минимально-инвазивная) | Миграция |
|---|---|---|---|---|
| A | `netmap` эфемерен (пересчёт на каждый GET из historical) | нет истории/инвентаря/change-detection; O(fleet×lists) на просмотр | Новый персистентный `net_*` слой; netmap остаётся как есть | Аддитивные таблицы, zero-regression |
| B | Топология знает только agent-ARP (нет инфра-SNMP) | слепота к свитчам/роутерам/немым хостам | Добавить passive SNMP harvest + active scan (за флагом) | OFF by default |
| C | Сетевые данные «прибиты» к `historical` payload | агент-side network завязан на день-1 scan | v1 не трогаем (server-side из snapshots); enrich — аддитивно | additive-optional, без bump |
| D | community хранится plaintext (`public`) | для непубличной community небезопасно | `CredentialStore` DPAPI (§3.18); `public` остаётся допустимым plaintext | новый модуль, opt-in |
| E | Нет стабильного network-identity (netmap кеит по gateway/MAC ad-hoc) | DHCP-смена IP → ложные дельты | `netdisco/identity.py` (chassis>serial>mac>ip) + миграция-слияние | детерминированно, как clone-safe device_id |

Все правки **аддитивны** и **за флагом**; ни одна не меняет существующее поведение при выключенном netdisco. Принцип: zero-regression, opt-in, обоснование каждой — выше.

---

## 8. Почему именно эта архитектура лучше для SRP (резюме обоснования)

- **Переиспользует доказанное на проде** (stdlib-SNMP, bounded-scan, anti-DoS, ghost-handling, COALESCE-storage, canvas-граф) — минимум нового кода, минимум риска, минимум стоимости сопровождения.
- **Не нарушает ни один инвариант SRP** (тонкий агент, контракт, trust-модель, RFC1918, no-private-keys, autoescape, параметризованный SQL).
- **Берёт сильнейшие идеи NetXMS** (poll-separation, active+passive, FDB-вывод, reachability-корреляция, change-detection) — но **без** его тяжёлого каркаса (NXSL, object-store, module-loader, zones), который противоречит SRP-минимализму.
- **Server-centric** масштабируется лучше agent-centric (один контролируемый сканер vs N×шторм) и сохраняет тонкость агента.
- **Evidence-граф + UNKNOWN-first** идеально совпадает с философией SRP (collector⊥semantic, объясни-почему, не выдумывай) — органичное, а не чужеродное расширение.
- **Фазируемо и независимо** (12 фаз, §план): каждая фаза — рабочая функциональность, OFF-by-default, zero-regression, отдельный commit/review/gate.

→ План реализации: [`docs/superpowers/plans/2026-06-20-network-discovery-plan.md`](../plans/2026-06-20-network-discovery-plan.md)
