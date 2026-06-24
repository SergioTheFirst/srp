# План трансформации: «Топология» + «Карта сети» → единая интерактивная «Карта сети»

> Статус: ЧЕРНОВИК ПЛАНА v2 (2026-06-24). Анализ завершён, реализация НЕ начата. Ждёт ревью владельца.
> Эффорт: R4. Каждая фаза: TDD → gate → subagent-review → авто merge+push (`[[auto-merge-push-no-ask]]`).
> Решения владельца (2026-06-24): идентичность = **FK-связка** (не слияние таблиц); добавить **панель управления картой** (кнопки/фильтры, напр. «скрыть ARP-only»); цель — **гениальная интерактивная карта**; собирать данные с активного оборудования **любым безопасным агентлесс-способом**; NetFlow (Ф9) = зависимость `netflow` PyPI; **выполнение каждой фазы фиксировать в `.claude/memory/`**.
> Источник анализа: 5 Explore-агентов (подсистемы) + 2 research-агента (методы сбора) + верификация db.py:200-301, dashboard.py:278-417.
> Связанное: `2026-06-20-network-discovery-plan.md`, память `[[project-netdisco]]`, `[[printer-dashboard-reconcile]]`, `[[printview-ip-resolution]]`.

---

## A. Контекст: что есть (две подсистемы)

**«Топология»** (`/topology`, nav-6) = подсистема **netdisco** (`server/netdisco/`, 20 модулей, P1-P12 в `origin/main`): ПЕРСИСТЕНТНАЯ модель (`net_devices`/`net_interfaces`/`net_links`/`net_device_readings`/`net_topology_snapshots`/`net_changes`), реальные L2/L3-связи (SNMP/LLDP/CDP/FDB) + слияние улик с бэндами доверия, корреляция достижимости (DOWN-первопричина vs UNREACHABLE-симптом), журнал изменений, ghost-lifecycle, история снимков, 5 фоновых циклов под одним `_poll_lock`, граф через `GraphCache` (TTL 45с). UI: рукописный canvas force-граф.

**«Карта сети»** (`/netmap`, nav-5) = аналитический **netmap** (`server/analytics/netmap.py`): ЭФЕМЕРНАЯ (read-only, на каждый запрос, без БД/кэша), кластеры по шлюзам, субсеть-аномалия (≥2 репортёра, ≥60% с ≥20% потерь), ICMP-качество ребра агент→шлюз, анимации, цвет агента по потерям. ARP-соседи кроме шлюза отбрасываются. Принтеры подмешиваются в маршруте по /24. UI: canvas force-граф (почти КОПИЯ физики топологии).

**Проблема:** две независимые модели из РАЗНЫХ данных; физ.ПК = до 3 несвязанных записей (`devices`/`net_devices`/`printers`) без FK; общий ключ — нормализованный MAC — нигде не персистится; «Неизвестное» там, где данные уже есть.

---

## B0. Видение: гениальная интерактивная карта

Единая «Карта сети» — это **операторский центр сети**, а не статичная картинка:
- **Один граф** с реальными связями (проводными/беспроводными/L3), статусами, качеством, группами — собранный автоматически из самого достоверного источника.
- **Управляемость:** оператор скрывает/показывает слои и классы узлов (кнопка «скрыть ARP-only» и десятки других фильтров), переключает раскладку, изолирует узел, подсвечивает путь и первопричину сбоя, листает историю снимков.
- **Глубокий агентлесс-сбор:** карта вытягивает максимум из активного оборудования штатными протоколами (SNMP-углубление, пассивные мультикасты, опциональные API контроллеров) — безопасно, RFC1918, read-only, под caps.
- **Единая карточка:** клик → ровно одна каноническая карточка устройства со всеми фактами SRP.
- **Меньше «Неизвестного»:** любые сведения (MAC агента, имя, reverse-DNS, mDNS/SSDP-тип, OUI) автоматически идентифицируют узел.

---

## B. Feature Matrix — Часть 1: объединение двух разделов

Решения: **KEEP** · **MERGE** · **DELETE** дубль · **REWORK** · **NEW**.

| # | Возможность | Где сейчас | Дубль | Решение |
|---|---|---|---|---|
| 1 | Canvas force-граф (физика/зум/пан/пин) | `topology.html` ≈ `netmap.html` | ПОЛНЫЙ | **MERGE** один движок `_netgraph.html` |
| 2 | Реальные L2/L3-связи (LLDP/CDP/FDB) | `netdisco/evidence,l2,fusion`,`net_links` | — | **KEEP** (хребет) |
| 3 | Звёздные связи агент→шлюз | `analytics/netmap.py` | — | **MERGE** в модель как `link_kind=agent-uplink` |
| 4 | Формула субсети /24 | `netmap._subnet_hint`+`dashboard._printer_subnet` | ДА | **MERGE**+**DELETE** дубль |
| 5 | Субсеть-аномалия | `netmap._finalize` | — | **KEEP** (чистый helper-оверлей) |
| 6 | ICMP-качество ребра | `netmap`+`netmap.html` | — | **KEEP** (оверлей) |
| 7-8 | Пакет-анимация, кольца-аномалии | `netmap.html` | — | **KEEP** (порт в общий движок) |
| 9 | Классификация типа | `netdisco/classify.py` | — | **KEEP** |
| 10 | Глифы по типу | оба canvas | частично | **MERGE** |
| 11 | Достижимость + корреляция DOWN/UNREACHABLE | `netdisco/correlation,reconcile` | — | **KEEP** |
| 12-14 | Журнал изменений, ghost-lifecycle, история снимков | `netdisco`,`net_changes`,`net_topology_snapshots` | — | **KEEP** |
| 15 | Реестр устройств | `net_devices` | — | **REWORK** (+FK agent/printer) |
| 16-17 | Интерфейсы, ARP/route-harvest | `net_interfaces`,`harvest.py` | — | **KEEP** |
| 18-19 | Активный скан, SNMP-креды DPAPI | `*/scan.py`,`credentials.py` | частично | **KEEP** |
| 20 | TTL-кэш графа | `cache.py` | — | **KEEP** (сменить loader) |
| 21-22 | Признак проводной/беспроводной; Wi-Fi/Ethernet | netmap пунктир Wi-Fi; иначе НЕТ | — | **NEW**+**MERGE** → атрибут `medium` |
| 23 | Принтеры как узлы | `dashboard._attach_printers` | — | **REWORK** (по FK/MAC, не /24) |
| 24 | OUI-vendor | `analytics/oui.py` | 2 места | **KEEP** (один источник) |
| 25 | Слой агент-MAC→device_id | `netmap._agent_macs`+`inventory._agent_macs` | ДА | **MERGE**+**DELETE**+**NEW** (персист FK) |
| 26 | Карточка устройства | 4 разных | 4 карточки | **REWORK** → одна каноническая |
| 27 | Кнопка «собрать сейчас» | `topology.html` | — | **KEEP** |
| 28-29 | Фильтр/поиск, зум/пан/пин/легенда/статус | оба | ПОЛНЫЙ | **MERGE** |
| 30 | Субсеть-заметка на карточке агента | `dashboard.subnet_context_for` | — | **KEEP** |
| 33-34 | XSS-контур, RU/Latin-локализация | `base.html`,`dashboard.py` | — | **KEEP** |

**Вывод:** netdisco = ХРЕБЕТ (персистентность, реальные связи, идентичность, lifecycle); netmap = ОВЕРЛЕИ (качество/аномалия/кластеры/анимации) → read-side обогащение единого ассемблера, НЕ вторая модель. Дубли (#1,#4,#25,#28,#29) схлопываются.

## B2. Feature Matrix — Часть 2: интерактив + сбор данных (всё NEW)

| # | Возможность | Источник данных | Фаза | Решение |
|---|---|---|---|---|
| 40 | Фильтры: источник (ARP-only/SNMP/agent/scan/LLDP/reverse-DNS/adapter) | provenance узла | 5 | **NEW** (вкл. «скрыть ARP-only») |
| 41 | Фильтры: тип/статус/подсеть/VLAN/medium/confidence/agent-presence | атрибуты узла/ребра | 5 | **NEW** |
| 42 | Фильтры: «только неизвестные», «изменено за N», «деградированные» | net_changes/качество | 5 | **NEW** |
| 43 | Тоггл слоёв: L2 / L3 / agent-uplink / wireless / flow / качество / группы / метки | атрибут ребра | 5 | **NEW** |
| 44 | Декластер-пресеты: только инфра / скрыть конечные / скрыть ARP-only | тип+provenance | 5 | **NEW** |
| 45 | Раскладка: субсеть / VLAN / тип / свободно + закрепить-всё/сброс + сохранение позиций | localStorage по nid | 5 | **NEW** |
| 46 | Боковая панель на клик (факты + ссылка на каноническую карточку) | граф+net_devices | 5 | **NEW** (держит контекст карты) |
| 47 | Изоляция узла (N-hop), подсветка пути (BFS), подсветка первопричины (DOWN+UNREACHABLE) | `graph.py`,`correlation.py` | 5 | **NEW** (данные уже есть) |
| 48 | Машина времени по снимкам + «показать изменения» (diff) | `net_topology_snapshots` | 5 | **NEW** (данные есть, UI не использовал) |
| 49 | Сохранить вид; экспорт PNG/CSV/JSON | клиент | 5 | **NEW** |
| 50 | LLDP port-id + mgmt-addr (порт↔порт рёбра, seed по mgmt-IP) | SNMP LLDP-MIB | 7 | **NEW** (T1, топ-приоритет) |
| 51 | dot1q VLAN-FDB (Q-BRIDGE) | SNMP Q-BRIDGE-MIB | 7 | **NEW** (T1; чинит dot1d на VLAN-свитчах) |
| 52 | ifXTable: ifName/ifAlias (метки портов = аплинки) | SNMP IF-MIB | 7 | **NEW** (T1) |
| 53 | **Беспроводные рёбра** client→AP (AIRESPACE/Aruba/MikroTik) | SNMP wireless-MIB | 7 | **NEW** (T1; реальные wireless-связи) |
| 54 | LLDP-MED (класс: телефон/AP), ENTITY model, dot1dStp (аплинк-направление), PoE, HOST-RESOURCES (серверы), ipNetToPhysical (IPv6 ARP) | SNMP | 7 | **NEW** (T1) |
| 55 | Reverse-DNS PTR (имя для безымянных) | DNS | 8 | **NEW** (T2, тривиально) |
| 56 | mDNS/DNS-SD, SSDP/UPnP, NetBIOS, WS-Discovery (тип/имя устройства) | мультикаст-сокеты | 8 | **NEW** (T2; де-аноним «Неизвестного») |
| 57 | TLS-cert CN/SAN + HTTP-баннер на открытых 443/80 | stdlib socket/ssl | 8 | **NEW** (T2; расширить принтерный probe) |
| 58 | Де-анонимизация из имеющихся данных (MAC агента, OUI, printer_ip_map) | БД | 8 | **NEW** (T2) |
| 59 | Адаптер-фреймворк (NetworkAdapter ABC + merge-by-MAC) | — | 9 | **NEW** (T3, опц.) |
| 60 | Адаптеры: MikroTik REST · UniFi · NetFlow/IPFIX · Redfish | urllib/socket | 9 | **NEW** (T3, opt-in кредами) |
| 61 | Адаптеры documented: Cisco DNA · Meraki · OPNsense/dhcpd · NETCONF/gNMI · SSH-CLI | — | 9 | **DOCUMENT** (будущее) |

Детали MIB/OID/портов — Приложение G.

---

## C. Архитектурные решения (каждое оправдано)

- **C1. Единая модель = persistent netdisco-граф + оверлеи (НЕ вторая модель).** `build_netmap` распадается на чистые helpers (`quality_overlay`,`subnet_anomaly`), которые потребляет ОДИН ассемблер. Убирает «две модели топологии», ноль новых хранилищ.
- **C2. Идентичность = MAC-хребет (FK, не слияние).** `net_devices` +nullable `device_id`/`printer_id` (аддитивно). Связка по нормализованному MAC (резерв IP). Один узел карты + одна каноническая карточка БЕЗ переписывания скоринга/печати. Три таблицы остаются (разные домены). Контракт провода не тронут → нет bump `CONTRACT_VERSION`. **(выбор владельца 2026-06-24)**
- **C3. Одна каноническая карточка.** Узел несёт один `card_url` по приоритету `agent → printer → net-infra`. Карточки агента/принтера обогащаются секцией «Сеть» из `net_devices` по FK. `/netdisco/device/{nid}` связанного → 302-redirect; самостоятельна только для чистой инфры.
- **C4. Один canvas-движок** (`_netgraph.html` на базе topology) + порт визуалов netmap (кластеры/качество/анимации/пунктир-wireless). Узел/ребро несут: `provenance[]`, `medium` (wired/wireless/l3/flow), `a_port`/`b_port`, `vlan`, `subtype` (phone/server/iot) — расширяемо.
- **C5. Имя/маршрут.** Единая страница = «Карта сети» (`/netmap`). «Топология» удаляется в Фазе 10. Новых операторских имён нет.
- **C6. Панель управления (Фаза 5).** Фильтры/слои/раскладки/изоляция/путь/первопричина/машина-времени/сохранение/экспорт — клиентские, поверх единого графа; ноль новых серверных моделей (данные уже в графе/снимках).
- **C7. Сбор — тремя тирами (Приложение G).** T1 = «выжать больше из SNMP, что уже умеем» (low-risk, та же BER-машина GET/GETNEXT, RFC1918, под caps). T2 = новые пассивные/мультикаст stdlib-сокеты (low-medium risk). T3 = опциональные адаптеры на кредах оператора (urllib, изоляция, никогда не перетирают валидированный SNMP). Все безопасны: read-only, RFC1918/link-local, fail-closed, под `_poll_lock`/caps.
- **C8. Приоритет источников.** Самый достоверный выигрывает: agent/SNMP > adapter > reverse-DNS/mDNS > OUI-догадка. Адаптеры/пассив ОБОГАЩАЮТ и связывают, не переопределяют валидированное.
- **C9. Принципы соблюдены:** максимум переиспользования (хребет+оверлеи+GraphCache+srpEsc+net_*_ru+oui+SNMP-стек+no-redirect-opener+DPAPI-cred); ноль дублей хранилища; одна модель; контракт агента не тронут; OFF-в-коде включаем в `server/config.json` (`[[no-disabled-by-default]]`); адаптеры опциональны по необходимости (нужны внешние креды).

---

## D. Фазы (атомарные задачи; после каждой — Phase Summary; затем авто merge+push)

Зависимости: 1→2→3→4→5; 6 после 4; 7,8 после 2 (обогащают модель, питают стиль 4/5); 9 после 7,8; 10 — последняя.

### Фаза 1 — Идентификационный хребет (MAC-FK) — R4 + security-review
**Цель.** Одно физ.устройство ↔ один узел: `net_devices` знает агента и/или принтер.
**Задачи.** T1 один нормализатор MAC (принтеры → `oui.normalize_mac`, тест-паритет) · T2 аддитивная миграция `net_devices +device_id +printer_id` (идемпотентно, `PRAGMA table_info`) +индексы `mac`,`device_id` · T3 один helper `agent_mac_index(snapshots)`, удалить дубль `inventory._agent_macs` · T4 `reconcile.link_identities()` по нормализованному MAC (резерв IP только при отсутствии MAC; точное совпадение, no-false-positive) · T5 вызов в конце inventory-цикла + reader `get_net_device_links(nid)`.
**Изм.файлы.** `db.py`,`netdisco/reconcile.py`,`inventory.py`,`scheduler.py`,`printers/models.py`,`analytics/netmap.py`. **Новые.** нет. **Удаляемые.** дубль-функция `inventory._agent_macs`.
**Модель данных.** `net_devices` +2 nullable колонки +2 индекса. Контракт провода не тронут. **API.** нет. **Сервер.** связка идентичности. **Dashboard/виз.** нет.
**Завершение.** FK заполняются для agent/printer-узлов на фикстуре; `make check` green; smoke OK. **Приёмка.** смешанная фикстура (agent+ARP+printer, общий MAC) → 1 строка с `device_id` И `printer_id`; нет ложных связок; MAC-нормализация идентична везде. **Риски.** ложная IP-связка (DHCP) → MAC-only по умолчанию; миграция боевой БД → идемпотентность. **Зависимости.** нет.
**Phase Summary:** ✅ РЕАЛИЗОВАНО (TDD, R4). `netmap.agent_mac_index` (публичный единый индекс agent-MAC; дубль `inventory._agent_macs` удалён, scheduler/inventory берут отсюда). `identity.link_identities(net_devices, agent_macs, printers)` — join по `oui.normalize_mac` (один нормализатор обе стороны), IP-резерв ТОЛЬКО для MAC-less строк и ТОЛЬКО приватный (`_canon_ip` канонизирует+`is_private`), no-false-positive на mismatch, `nd-unknown` пропускается. `db`: `net_devices +device_id +printer_id` (аддитивно nullable, идемпотентная legacy-миграция через `_ADD_COLUMNS`+`PRAGMA`, backfill-guard `table in _BACKFILL`), индексы `idx_netdev_mac`/`idx_netdev_device_id` создаются ПОСЛЕ миграции колонок (legacy-ordering); `set_net_device_links` (COALESCE-preserve — транзиентный промах не стирает FK), `get_net_device_links`. Связка в конце `run_inventory_cycle` под `_poll_lock`, best-effort (`try/except`+`_log.exception`, persisted-инвентарь цел). **Cleanup-инвариант:** `delete_device`/purge агента → `net_devices.device_id=NULL` (узел остаётся, ссылка гаснет, нет висячих указателей); `delete_unconfirmed_arp_printers` → `printer_id=NULL` симметрично. `net_devices` НЕ в `_DEVICE_TABLES` (узел keyed-by-MAC, не agent-owned) — guard-тест обновлён с явным исключением. Контракт агента не тронут (нет bump). Гейт: ruff/mypy(106)/bandit/smoke green, cov ~92.8%. Тесты: `test_netmap_identity.py`(10) + scheduler-link(1) + cleanup-null(1) + printer-null(1). Security-review Opus = **APPROVE-WITH-NITS**, 0 crit, 0 false-link/data-loss; применены H1(resilience try/except), M1(приватный IP-гейт), M2(nd-unknown guard), L2(.get); L1(двойной вызов index — negligible) и L3(индексы в init_db, не в _SCHEMA — обосновано legacy-ordering) приняты как есть. Память `[[netmap-identity-spine]]`.

### Фаза 2 — Единый ассемблер (read-side оверлеи) — R3 + code-review
**Цель.** Одна модель = netdisco-узлы/связи + идентичность + оверлеи netmap + расширяемые атрибуты.
**Задачи.** T1 рефактор `netmap.py` → чистые `quality_overlay()`,`subnet_anomaly()` (`build_netmap` временно остаётся) · T2 один `subnet_hint()` (удалить дубль) · T3 `netdisco/unified.py::build_network_map(...)` → суперсет-граф: узлы из `net_devices` (+`device_id`/`printer_id`/`card_url`/`status`/`vendor`/`subnet`/`provenance`/`subtype`), рёбра из `net_links` + agent-uplink с `medium`+качеством · T4 вывод `medium` (agent-uplink по kind адаптера; SNMP-линк с `dev_type=ap`→wireless; иначе wired) · T5 тесты (слияние, оверлей, wireless-флаг, нет дублей).
**Изм.файлы.** `netmap.py`,`dashboard.py`. **Новые.** `netdisco/unified.py`. **Удаляемые.** дубль subnet-функция.
**Модель данных.** опц. `net_links +medium`/`a_port`/`b_port`/`vlan` (аддитивно) когда T1/Фаза7 начнут писать; по умолчанию read-side. **API.** нет. **Сервер.** единый ассемблер; netmap → поставщик оверлеев. **Dashboard/виз.** нет.
**Завершение.** суперсет-граф покрывает узлы обеих старых моделей; `make check` green. **Приёмка.** фикстура (router+switch+ap+2 agent+printer+ARP) → реальные L2 И agent-uplink с качеством; wireless помечен `medium=wireless`; субсеть-аномалия как поле; нет двойных узлов. **Риски.** расхождение ключей → канонический ключ = `device_nid`. **Зависимости.** Фаза 1.
**Phase Summary:** _(заполнить)_

### Фаза 3 — Единый API + кэш — R3 + code-review
**Цель.** Один источник графа; переиспользовать `GraphCache`.
**Задачи.** T1 `GET /api/v1/network-map/graph` → ассемблер; loader `GraphCache` → ассемблер (TTL 45с) · T2 `/api/v1/netmap`+`/api/v1/topology/graph` → deprecated-алиасы · T3 сохранить `/topology/poll`,`/discovery/poll`,`/topology/changes` (+ алиасы `/network-map/*`) · T4 тесты (форма, кэш-поведение, invalidate после poll) · T5 поднять init кэша в startup (снять P11 LOW).
**Изм.файлы.** `api.py`,`netdisco/cache.py`,`main.py`. **Новые/Удаляемые.** нет (старые → алиасы до Ф10). **API.** +`/network-map/graph`; старые → алиасы. **Сервер.** loader=ассемблер. **Dashboard/виз.** нет.
**Завершение.** endpoint отдаёт суперсет из кэша; `make check` green. **Приёмка.** узлы+рёбра+оверлеи; повтор в TTL не пересобирает (мок-счётчик); XSS-инвариант держится; алиасы = тот же граф. **Риски.** стоимость сборки → кэш + read-side caps. **Зависимости.** Фаза 2.
**Phase Summary:** _(заполнить)_

### Фаза 4 — Единый canvas (подложка отрисовки) — R3 + code-review + XSS
**Цель.** Один canvas со всеми визуалами обеих страниц.
**Задачи.** T1 общий движок `_netgraph.html` (физика/зум/пан/пин/поиск/тема/`textContent`-tooltip) на базе topology · T2 порт визуалов netmap (кольца кластеров, цвет-качество, пакет-анимация, кольца-аномалии, **пунктир `medium=wireless`**) · T3 глифы ВСЕХ типов (router/switch/ap/agent/printer/server/phone/endpoint/unknown) + статус-цвет + agent-health через `device_id` · T4 легенда покрывает все типы+оба medium; SSR-таблица+журнал+кнопка «собрать сейчас» · T5 `/netmap` отдаёт единый граф (ассемблер+кэш) · T6 web-тест (SSR без JS, остров, XSS-pin `<img onerror>` инертен, reduced-motion).
**Изм.файлы.** `netmap.html`,`dashboard.py` (`/netmap`). **Новые.** `_netgraph.html`. **Удаляемые.** нет (topology.html — в Ф10). **Dashboard.** `/netmap`→ассемблер. **Виз.** единый canvas, wireless-стиль, все типы.
**Завершение.** `/netmap` рисует реальные L2/L3 И качество/кластеры/аномалии; `make check` green; headless-E2E. **Приёмка.** все типы узлов (вкл. инфру/принтеры), wireless отдельным стилем, поиск/зум/пан/пин/статусы/журнал/кнопка; XSS инертен; reduced-motion. **Риски.** регресс физики → общий include + скриншот-регрессия 1024/1440; перф → пресимуляция тиков. **Зависимости.** Фаза 3.
**Phase Summary:** _(заполнить)_

### Фаза 5 — Интерактивная панель управления («гениальная карта») — R3 + code-review + XSS
**Цель.** Карта как операторский центр: фильтры, слои, кнопки, навигация по графу.
**Задачи — CORE.** T1 движок фильтров (предикат узел/ребро → dim/hide): источник/тип/статус/подсеть/VLAN/medium/confidence/agent-presence · T2 кнопка **«скрыть ARP-only»** + пресеты (только инфра / скрыть конечные) · T3 тоггл слоёв (L2/L3/agent-uplink/wireless/flow/качество/группы/метки портов) · T4 раскладка (субсеть/VLAN/тип/свободно) + закрепить-всё/сброс + персист позиций (localStorage по nid) · T5 боковая панель на клик (факты + ссылка на каноническую карточку, держит контекст) + tooltip ребра (via_source/confidence/medium/порты/качество) + подсветка соседей · T6 сохранить вид (фильтры+раскладка → localStorage) + экспорт PNG/CSV/JSON · T7 web-тест (фильтры применяются, ARP-only скрыт, слои тогглятся, XSS-safe, reduced-motion).
**Задачи — ADVANCED (опц., можно отложить, без оверинжиниринга).** T8 изоляция узла (N-hop) · T9 подсветка пути (2 узла, `graph.py` BFS) · T10 подсветка первопричины (DOWN+UNREACHABLE-поддерево, `correlation.py`) · T11 машина времени по `net_topology_snapshots` + «показать изменения» (diff) · T12 миникарта + горячие клавиши.
**Изм.файлы.** `_netgraph.html`,`netmap.html`,`dashboard.py` (контекст: provenance/типы/снимки для слайдера). **Новые.** нет (всё клиентское поверх графа). **Модель/API.** нет (ADVANCED-машина-времени читает существующие снимки — опц. endpoint `/network-map/graph?at=<snapshot_id>`). **Dashboard.** контекст фильтров/снимков. **Виз.** панель фильтров/слоёв/кнопок, боковая панель, подсветки.
**Завершение.** оператор фильтрует/тогглит/изолирует; «скрыть ARP-only» работает; `make check` green; E2E. **Приёмка.** ≥ фильтры(источник/тип/статус/подсеть/medium) + слои + «скрыть ARP-only» + раскладки + боковая панель + сохранить-вид + экспорт; XSS инертен. ADVANCED — по готовности. **Риски.** перегруз UI → группировать панель, дефолты разумные; перф фильтров на большом графе → индексировать узлы. **Зависимости.** Фаза 4.
**Phase Summary:** _(заполнить)_

### Фаза 6 — Единая каноническая карточка — R3 + code-review
**Цель.** Клик → ровно одна карточка; никаких двух разных карточек одного устройства.
**Задачи.** T1 резолвер `card_url(node)` (agent→printer→net-infra; узел несёт готовый url из ассемблера) · T2 секция «Сеть» в `device.html` (интерфейсы/связи/соседи/достижимость/журнал по `device_id`, переиспользовать рендер `net_device.html`) · T3 секция «Сеть» в `printer_detail.html` по `printer_id` · T4 `/netdisco/device/{nid}` связанного → 302-redirect на каноническую; иначе как раньше · T5 тесты (приоритет, redirect, секции, нет дубль-карточки).
**Изм.файлы.** `dashboard.py`,`device.html`,`printer_detail.html`,`net_device.html`. **Новые.** `_net_section.html` (общий partial). **Удаляемые.** нет (`net_device.html` = карточка чистой инфры). **Dashboard.** резолвер+redirect+обогащённые карточки. **Виз.** секция «Сеть».
**Завершение.** клик по agent/printer/infra → корректная единственная карточка; `make check` green. **Приёмка.** связанное устройство = один URL карточки; `/netdisco/device` связанного редиректит; секция «Сеть» рендерится; чистая инфра → net-карточка. **Риски.** цикл-редирект → только net→canonical; раздувание → секция сворачиваемая. **Зависимости.** Фазы 1,2,4.
**Phase Summary:** _(заполнить)_

### Фаза 7 — Tier-1: углубление SNMP (точность топологии) — R4 + security-review
**Цель.** Максимум топологии/идентичности тем же SNMP-стеком. Реальные беспроводные рёбра.
**Задачи (атомарны, по сигналу; OID — Прил.G).** T1 LLDP port-id (`lldpRemPortId`/`lldpLocPortId`) → рёбра порт↔порт · T2 LLDP mgmt-addr (`lldpRemManAddr`) → neighbor→mgmt-IP (seed-расширение) · T3 LLDP-MED (`lldpXMedRemDeviceClass`) → класс телефон/AP · T4 dot1q VLAN-FDB (`dot1qTpFdbPort`) → L2 с VLAN (чинит dot1d) · T5 ifXTable (`ifName`/`ifAlias`) → метки портов/аплинков · T6 **wireless client-assoc** (AIRESPACE/Aruba-WLSX/MikroTik по `sysObjectID`-OUI WLC) → рёбра client→AP `medium=wireless` · T7 dot1dStp (роль/состояние портов) → направление аплинка для не-LLDP свитчей; ENTITY model (`entPhysicalModelName`) → точный тип/иконка; PoE (`pethPsePortDetectionStatus`) → подтверждение AP/телефона; HOST-RESOURCES (`hrSWRun`) → серверы; `ipNetToPhysical` → IPv6 ARP · T8 интеграция улик в `fusion.py` (port-id/STP — выше FDB-уверенность; провенанс) + тесты per-сигнал.
**Изм.файлы.** `netdisco/oids.py`,`evidence.py`,`snmp_probe.py`,`harvest.py`,`l2.py`,`fusion.py`,`classify.py`,`models.py`,`unified.py`,`config.json` (новые walks ON; floor-caps). **Новые.** опц. `netdisco/wireless.py` (контроллер-walks). **Модель данных.** аддитивно: `net_links.a_port/b_port/vlan/medium`; `net_devices.subtype`; `net_interfaces.if_alias`. **API.** через граф. **Сервер.** новые SNMP-walks в classify/topology-циклах под существующими caps/`_poll_lock`/RFC1918. **Виз.** wireless-рёбра, метки портов, иконки телефон/AP/сервер.
**Завершение.** карта показывает порт↔порт рёбра, VLAN, реальные wireless-связи; «AP/телефон/сервер» классифицированы; `make check` green. **Приёмка.** на SNMP-фикстуре: directed port-labeled L2, dot1q-FDB размещает клиентов по VLAN, client→AP рёбра помечены wireless; модель-имя из ENTITY; нет роста стоимости цикла выше caps. **Риски.** большие walks (FDB/hrSWRun) → row-caps (dot1q как dot1d-cap, hrSWRun 200, фильтр running); вендор-различия wireless-MIB → walk только подтверждённых WLC по OUI, fail-closed. **Зависимости.** Фаза 2 (ассемблер потребляет новые атрибуты).
**Phase Summary:** _(заполнить)_

### Фаза 8 — Tier-2: пассивная идентификация (убрать «Неизвестное») — R4 + security-review
**Цель.** Де-анонимизировать узлы штатными пассивными механизмами.
**Задачи.** T1 де-аноним из имеющихся данных (кросс-MAC agent-hostname/OUI/`printer_ip_map`) → «unknown» получает имя/тип, если совпало · T2 reverse-DNS PTR (`socket.gethostbyaddr`, пул+кэш+RFC1918-гейт+cap, имя низкого приоритета) · T3 mDNS/DNS-SD (UDP 5353, мультикаст, `socket`+парс) → тип сервиса (`_ipp`/`_airplay`/`_googlecast`/`_smb`/`_workstation`) · T4 SSDP/UPnP (1900) → `SERVER`/`NT`-тип + `LOCATION`-XML (модель) · T5 NetBIOS (137) → Windows-имя; WS-Discovery (3702) → Windows/WSD-принтеры · T6 расширить TLS-cert (CN/SAN) + HTTP-баннер на открытых 443/80 (переиспользовать принтерный no-redirect-opener) · T7 тесты (де-аноним, rate-cap/RFC1918/link-local-гейт, приоритет имён).
**Изм.файлы.** `netdisco/unified.py`/`reconcile.py`,`config.json` (пассив ON per-протокол). **Новые.** `netdisco/passive.py` (мультикаст-листенеры+парсеры), `netdisco/naming.py` (reverse-DNS). **Модель данных.** имя/тип пишутся в `net_devices.hostname`/`subtype` низким приоритетом (не перетирают SNMP/agent). **API.** через граф. **Сервер.** обогащение под caps/fail-closed; мультикаст link-local. **Виз.** меньше «Неизвестное», иконки по типу сервиса.
**Завершение.** «Неизвестное» исчезает где есть сведения; пассив под caps; `make check` green. **Приёмка.** узел с известным MAC-агента/mDNS-типом ≠ «Неизвестное»; reverse-DNS/мультикаст не выходят за RFC1918/link-local и cap; приоритет agent/SNMP > пассив. **Риски.** мультикаст не пересекает VLAN → честно «per-segment», агент-сторона как будущий relay; reverse-DNS-латентность → таймаут+кэш+пул, fail-closed; p0f passive — SKIP (Windows raw-socket заблокирован). **Зависимости.** Фазы 1,2.
**Phase Summary:** _(заполнить)_

### Фаза 9 — Tier-3: опциональные адаптеры активного оборудования — R4 + security-review
**Цель.** Готовая топология/идентичность из контроллеров/флоу по желанию оператора (его креды).
**Задачи.** T1 фреймворк: `NetworkAdapter` ABC (`collect()→AdapterResult{nodes,links,identity_map,errors}`, read-only, ≤30с, не бросает) + `adapter_merge.py` (dedup по MAC→резерв IP, провенанс, цель = `net_*`) + хранение кредов через DPAPI (`netdisco/credentials.py`-паттерн) + конфиг `optional_adapters[]` · T2 MikroTik RouterOS REST (urllib+Basic: `/ip/arp`,`/dhcp-server/lease`,`/interface/bridge/host`,`/ip/neighbor`) · T3 UniFi Controller (urllib+cookie: devices+clients+`/topology` LLDP-граф) · T4 Redfish BMC (urllib+session: серийник/модель/OOB-MAC серверов) · T5 NetFlow v9/IPFIX-коллектор (stdlib UDP-листенер 2055; парс шаблонов; flow-рёбра; **`netflow` PyPI — одобрено владельцем 2026-06-24, единственная не-stdlib зависимость плана, изолирована в адаптере flow; шаблоны кэшируются персистентно**) · T6 documented-only: Cisco DNA/Meraki/OPNsense+dhcpd/NETCONF/gNMI/SSH-CLI (в Прил.G, не код) · T7 тесты (мерж-by-MAC, изоляция сбоя адаптера, провенанс, креды не в логи/API).
**Изм.файлы.** `config.json` (`optional_adapters`),`main.py` (цикл адаптеров),`unified.py` (потребляет merge). **Новые.** `netdisco/adapters/__init__.py`,`base.py`,`mikrotik.py`,`unifi.py`,`redfish.py`,`flow.py`,`adapter_merge.py`. **Модель данных.** провенанс уже в узле; адаптерные узлы/рёбра в `net_*` с `provenance=adapter`. **API.** через граф. **Сервер.** опциональный цикл (свой интервал для flow); адаптеры изолированы, никогда не перетирают валидированный SNMP. **Виз.** провенанс-фильтр (#40) включает adapter-источники; flow-рёбра отдельным слоём.
**Завершение.** ≥2 адаптера (MikroTik+UniFi) + Redfish сливаются в единую модель; `make check` green. **Приёмка.** на мок-адаптерах узлы дедуплицируются по MAC, сбой одного не блокирует другие, креды отсутствуют в логах/API/дашборде; адаптер-данные не переопределяют SNMP-идентичность. **Риски.** креды/cloud-выход (Meraki) → DPAPI-хранилище + явный флаг `requires_cloud`; зависимость `netflow` (PyPI, чистый Python) для NetFlow — одобрена владельцем 2026-06-24; единственная не-stdlib зависимость, изолирована в адаптере flow; вендор-хрупкость → DOCUMENT-ONLY для SSH/NETCONF/gNMI. **Зависимости.** Фазы 7,8 (merge-слой).
**Phase Summary:** _(заполнить)_

### Фаза 10 — Демонтаж «Топологии» + документация — R2 + финальный review
**Цель.** Раздел «Топология» исключён; весь функционал в «Карте сети».
**Задачи.** T1 убрать nav-ссылку «топология» · T2 удалить маршрут `/topology` + `topology.html` · T3 `/topology`→301 на `/netmap`; решение владельца по старым API-алиасам (оставить/удалить) · T4 верификация: каждая строка матриц (Часть1 KEEP/MERGE/NEW + Часть2) присутствует в `/netmap` (ручной + E2E) · T5 обновить `CHANGELOG`,`CONTINUITY`,`cctodo`, память (`[[network-map-unified]]` + правки `[[project-netdisco]]`).
**Изм.файлы.** `base.html`,`dashboard.py`,`api.py`,`CHANGELOG.md`,`CONTINUITY.md`,`cctodo.md`,`.claude/memory/*`. **Новые.** `.claude/memory/network-map-unified.md`. **Удаляемые.** `topology.html`; маршрут `/topology`; nav-ссылка; (опц.) старые endpoints. **Dashboard.** nav без «топологии»; `/topology`→301. **Виз.** одна точка входа.
**Завершение.** «Топология» недоступна; все фичи матриц в «Карте сети»; ПОЛНЫЙ gate green; smoke+E2E. **Приёмка.** nav без «топологии»; `/topology` редиректит; чек-лист матриц 100%; `test_topology_web.py` мигрирован в `test_netmap_web.py`; покрытие ≥80%. **Риски.** утрата фичи → Ф10 только после ручной+E2E верификации каждой строки; внешние API-потребители → решение по алиасам у владельца. **Зависимости.** Фазы 1-9 смёржены.
**Phase Summary:** _(заполнить)_

---

## E. Сквозные риски и зависимости
- **R-Идентичность (выс):** ложные MAC/IP-связки → MAC-only по умолчанию, IP только явный резерв (Фаза 1 + security-review).
- **R-Перф (сред):** ассемблер + новые walks + фильтры на большом LAN → GraphCache + read-side caps + per-walk row-caps + индексация узлов клиента.
- **R-Виз-регресс (сред):** слияние canvas → общий include + скриншот-регрессия 1024/1440 + reduced-motion.
- **R-Безопасность (выс, Фазы 7-9):** новые SNMP-walks/мультикаст-листенеры/внешние креды → RFC1918/link-local-гейт, caps, fail-closed, DPAPI-креды, no-redirect-opener, security-review ОБЯЗАТЕЛЕН.
- **R-Зависимости (низ):** только NetFlow тянет `netflow` (PyPI, чистый Python) — одобрено владельцем 2026-06-24; изолировано в адаптере. Остальное — stdlib (urllib/socket/ssl). Контракт агента не тронут → нет bump `CONTRACT_VERSION`.
- **Порядок:** 1→2→3→4→5; 6 после 4; 7,8 после 2; 9 после 7,8; 10 последняя. R4+security-review: Фазы 1,7,8,9. R3+code-review: 2-6. R2: 10.

## F. Глобальная приёмка
1. В nav один раздел «Карта сети»; «Топология» удалена; `/topology`→301.
2. Каждая строка матриц (Часть1 KEEP/MERGE/NEW + Часть2) присутствует и работает в `/netmap` (E2E).
3. Одно физ.устройство = один узел + одна каноническая карточка; `/netdisco/device` связанного редиректит.
4. На одной карте: реальные L2/L3 + agent-uplink + **реальные wireless-рёбра** + качество + субсеть/VLAN-кластеры + журнал.
5. Панель управления: фильтры (вкл. «скрыть ARP-only») + слои + раскладки + боковая панель + сохранить-вид + экспорт.
6. «Неизвестное» отсутствует где есть сведения (кросс-MAC/reverse-DNS/mDNS/SSDP/OUI).
7. Углублённый агентлесс-сбор (T1 SNMP + T2 пассив) активен и безопасен; T3-адаптеры доступны по кредам.
8. Одна модель топологии; нет дублей хранилища; контракт агента не тронут.
9. ПОЛНЫЙ gate green + smoke + E2E; security-review APPROVE на Фазах 1,7,8,9.

---

## G. Приложение: каталог источников сбора (по research-агентам)

### G1. Tier-1 — углубление SNMP (та же BER-машина, GET/GETNEXT, RFC1918, под caps) → Фаза 7
| Сигнал | MIB / ключевой OID | Даёт | Вердикт |
|---|---|---|---|
| LLDP port-id | LLDP-MIB `lldpRemPortId` 1.0.8802.1.1.2.1.4.1.1.7 / `lldpLocPortId` | directed рёбра порт↔порт | ADOPT (топ-1) |
| LLDP mgmt-addr | `lldpRemManAddrTable` 1.0.8802.1.1.2.1.4.2 | neighbor→mgmt-IP (seed) | ADOPT (топ-2) |
| dot1q VLAN-FDB | Q-BRIDGE-MIB `dot1qTpFdbPort` 1.3.6.1.2.1.17.7.1.2.2.1.2; `dot1qPvid` | L2 c VLAN (чинит dot1d) | ADOPT (топ-3) |
| ifXTable | IF-MIB `ifName` ...31.1.1.1.1 / `ifAlias` ...31.1.1.1.18 | метки портов/аплинков | ADOPT (топ-4) |
| wireless client-assoc | AIRESPACE `bsnMobileStation*` 1.3.6.1.4.1.14179.2.1.4.1; Aruba `wlsxUserTable` 14823; MikroTik `mtxrWlRtab*` 14988 | **рёбра client→AP (wireless)** | ADOPT (топ-5) |
| reverse-DNS | (DNS, не SNMP) | имя узла | ADOPT (топ-6, в Фазе 8) |
| ENTITY model | ENTITY-MIB `entPhysicalModelName` 1.3.6.1.2.1.47.1.1.1.1.13 | модель/иконка/стек | ADOPT (топ-7) |
| LLDP-MED | LLDP-EXT-MED `lldpXMedRemDeviceClass` | класс телефон/AP | ADOPT (топ-8) |
| dot1dStp | BRIDGE-MIB `dot1dStpPortState/DesignatedBridge` + RSTP role | направление аплинка (не-LLDP) | ADOPT |
| PoE | POWER-ETHERNET-MIB `pethPsePortDetectionStatus` 1.3.6.1.2.1.105.1.1.1 | подтверждение AP/телефона | ADOPT |
| HOST-RESOURCES | `hrSWRunTable` 1.3.6.1.2.1.25.4.2.1; `hrStorage` | серверная роль | ADOPT (cap 200, filter running) |
| IPv6 ARP | IP-MIB `ipNetToPhysicalPhysAddress` 1.3.6.1.2.1.4.35.1.4 | IPv6 neighbor→MAC | ADOPT |

### G2. Tier-2 — пассив/мультикаст (stdlib socket/ssl, link-local/RFC1918) → Фаза 8
| Сигнал | Протокол/порт | Даёт | Вердикт |
|---|---|---|---|
| reverse-DNS PTR | DNS 53 | имя (`socket.gethostbyaddr`) | ADOPT (тривиально) |
| mDNS/DNS-SD | UDP 5353 mcast | тип сервиса (Apple/Google/Linux/принтер) | ADOPT |
| SSDP/UPnP | UDP 1900 mcast | тип/OS + LOCATION-XML (модель) | ADOPT |
| NetBIOS | UDP 137 | Windows-имя/роль | ADOPT |
| WS-Discovery | UDP 3702 mcast | Windows-хосты + WSD-принтеры | ADOPT |
| TLS-cert CN/SAN | TCP 443 | hostname/модель (расширить принтерный) | ADOPT |
| HTTP-баннер | TCP 80 | `Server`/title (расширить) | ADOPT |
| DHCP fingerprint (opt55) | UDP 67 | OS-отпечаток | OPTIONAL (нужен DHCP-сервер/capture) |
| p0f passive TCP | raw socket | OS-отпечаток | SKIP (Windows raw-socket заблокирован) |

### G3. Tier-3 — опциональные адаптеры (креды оператора; urllib/socket; merge-by-MAC) → Фаза 9
| Адаптер | Auth / on-prem | Даёт | Вердикт |
|---|---|---|---|
| MikroTik RouterOS REST | Basic / on-prem | ARP+DHCP-lease+bridge-host+neighbor | BUILD (приоритет-1) |
| UniFi Controller | cookie / on-prem | devices+clients+LLDP-`/topology` | BUILD (приоритет-2) |
| NetFlow v9/IPFIX | none (UDP 2055) / on-prem | flow-рёбра (трафик) | BUILD (приоритет-3; dep `netflow` PyPI — одобрено владельцем) |
| Redfish BMC | session / on-prem | серверный серийник/модель/OOB-MAC | BUILD (приоритет-4) |
| OPNsense+dhcpd lease | apikey / file | авторитетный IP↔MAC↔hostname | BUILD (5) |
| Cisco Catalyst Center | JWT / on-prem | готовый physical+L3-граф | BUILD (5, niche) |
| Meraki Dashboard | apikey / CLOUD | лучший topology-API (`/topology/linkLayer`) | BUILD (флаг `requires_cloud`) |
| Aruba Central / pfSense / Win-DHCP / NETCONF / gNMI / sFlow / SSH-CLI / IPMI | разное | topology/identity | DOCUMENT-ONLY / SKIP (creds/cloud/dep/хрупкость) |

**Общий интерфейс адаптера:** `AdapterConfig{adapter_type,endpoint,credential,tls_verify,site_id}` → `NetworkAdapter.collect() → AdapterResult{nodes[],links[],identity_map(mac→node_id),errors[]}`; merge-слой дедуплицирует по MAC (резерв IP), пишет провенанс, цель = существующие `net_*`; адаптер read-only, изолирован, ≤30с, не перетирает валидированный SNMP.

---

_Конец плана v2. Готов к пофазной реализации без дополнительного архитектурного проектирования._
