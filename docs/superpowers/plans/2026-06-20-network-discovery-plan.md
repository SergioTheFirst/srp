# PLAN: SRP Network Discovery — roadmap (13 фаз, атомарные задачи)

> Спецификация: [`../specs/2026-06-20-network-discovery-rfc.md`](../specs/2026-06-20-network-discovery-rfc.md)
> Правила: каждая фаза = своя ветка → TDD (RED→GREEN) → gate green → subagent-review → `merge --no-ff` → push (по команде/авто-важное) → Phase Summary → обновить CONTINUITY.md.
> Инварианты на каждой фазе: OFF-by-default, zero-regression, contract additive-only (без bump), агент stdlib/RFC1918, SNMP read-only, RFC1918-only, bounded/rate-limited. Gate (§6 CLAUDE.md): ruff+mypy+bandit+pytest cov≥80% + smoke + CHANGELOG-строка.
> «Атомарно» = 1 изменение = 1 коммит. Порядок коммитов внутри фазы указан.
> **Зависимости:** фазы независимо мержатся, но строят на персистентном состоянии предыдущих (указано в «Зависит от»). «Независимость» = self-contained, отдельный gate/review/merge.

Прогресс-легенда: ⬜ не начата · 🟦 в работе · ✅ done.

---

## Phase Summary (шаблон — заполнять в КОНЦЕ каждой фазы, дописывать в CONTINUITY.md «Done»)
```
### NETDISCO Phase N — <название> [<merge-hash>]
- Реализовано: <что именно работает теперь>
- Архрешения: <принятые решения + почему>
- Изменилось: <файлы/таблицы/API/контракт>
- Осталось: <что НЕ вошло, перенесено>
- Риски: <оставшиеся>
- Для след. фазы помнить: <самодостаточный контекст: имена функций, сигнатуры, инварианты>
- Gate: ruff/mypy/bandit ✓, cov X%, smoke OK; review: <verdict>
```

---

## Phase 1 — Identity + Models (чистый фундамент) ✅ [merge bc54833]
**Цель:** стабильная идентичность сетевого устройства и неизменяемые модели — то, на чём строится всё. Чисто, без БД/сети/wiring.
**Зависит от:** ничего.
**Задачи (атомарные):**
1. `netdisco/__init__.py` (пустой пакет-маркер).
2. `netdisco/identity.py`: `device_nid(*, chassis_id, serial, mac, ip) -> str` (chassis>serial>mac>ip, нормализация MAC через `analytics.oui.normalize_mac`, `nd-unknown` fallback) + `merge_identity(old, new)` (выбор сильнейшего id).
3. `netdisco/models.py`: frozen dataclasses `NetDevice, NetInterface, NetLink, LinkEvidence, ResolvedLink, DeviceReading, TopologySnapshot, TopologyDelta, Graph`. Все Optional→None при UNKNOWN.
**Изменяемые модули:** нет. **Новые:** `server/netdisco/{__init__,identity,models}.py`.
**Контракты:** нет (внутренние модели). **Структура данных:** §5.3 RFC.
**API:** нет.
**Алгоритмы:** §4.1 RFC (precedence + нормализация + миграция-слияние).
**Риски:** MAC-нормализация рассинхрон с `printer_identity` → переиспользовать `oui.normalize_mac` (один источник). LOW.
**Критерии завершения:** оба модуля + тесты, gate green.
**Критерии приёмки:** `device_nid` детерминирован, устойчив к смене IP при том же MAC; разные MAC→разные id; пустой вход→`nd-unknown`. Модели frozen (mutation→FrozenInstanceError).
**План тестирования:** `tests/netdisco/test_identity.py` (precedence, нормализация, DHCP-стабильность, merge), `tests/netdisco/test_models.py` (frozen, defaults None).
**Файлы:** 3 новых модуля + 2 теста + `tests/netdisco/__init__.py`.
**Порядок коммитов:** (1) пакет+models+test_models → (2) identity+test_identity.

## Phase 2 — Topology storage (БД-слой) ✅ [merge 75942f3]
**Цель:** персистентные таблицы + store/get-функции (зеркало `printers`/`printer_readings`).
**Зависит от:** P1 (модели).
**Задачи:**
1. `db.py` `_SCHEMA` += 6 таблиц (§5.2): `net_devices, net_interfaces, net_links, net_device_readings, net_topology_snapshots, net_changes` (+индексы). Все `IF NOT EXISTS`.
2. `db.py` `upsert_net_device(dev)` (COALESCE identity, advance last_seen, set first_seen on insert) + `store_net_device_reading(nid, detail, received_at)` (append-only + retain prune).
3. `db.py` `replace_net_links(nid, links)` (заменить рёбра участников цикла) + `store_topology_snapshot(graph)` + `store_net_change(change)`.
4. `db.py` `get_net_devices(...)`, `get_net_device(nid)`, `get_net_links()`, `get_latest_topology_snapshot()`, `get_net_changes(days)` (latest-by-id паттерн).
**Изменяемые модули:** `server/db.py` (только добавления). **Новые:** нет.
**Контракты:** нет. **Структура данных:** §5.2.
**API:** нет.
**Алгоритмы:** COALESCE-merge (§4.6), retention prune (как `_retain_*`).
**Риски:** миграция на старой БД — `IF NOT EXISTS` безопасно (как принтерные таблицы добавились). Параметризованный SQL (инвариант). LOW.
**Критерии завершения:** функции + тесты round-trip, gate.
**Критерии приёмки:** store→get round-trip; COALESCE не затирает known транзиентным None; миграция на pre-netdisco БД не падает; append-only растёт, prune держит cap.
**План тестирования:** `tests/netdisco/test_db_netdisco.py` (round-trip, COALESCE, миграция idempotent, prune, latest-by-id).
**Файлы:** `db.py` (edit) + 1 тест.
**Порядок коммитов:** (1) schema+migration test → (2) upsert/store funcs+tests → (3) get funcs+tests.

## Phase 3 — Persistent inventory из существующих данных агента (первая видимая ценность) ✅ [merge ee08e23]
**Цель:** построить персистентный сетевой инвентарь из уже собираемых agent-ARP/adapter данных. БЕЗ новых сетевых probe, БЕЗ изменений агента/контракта.
**Зависит от:** P1, P2.
**Задачи:**
1. `netdisco/inventory.py`: `build_inventory(snapshots) -> list[NetDevice]` — из `db.get_network_snapshots()`: агенты (по их adapter-MAC, type=`agent`) + agentless ARP-соседи (type=`endpoint`/`unknown`, vendor через `oui.vendor_for_mac`), identity через `device_nid`, dedup. Переиспользует identity-layer netmap (`_agent_macs`).
2. `netdisco/inventory.py`: `persist_inventory(devices, store=db.upsert_net_device)` (COALESCE, advance last_seen).
3. `api.py`: `GET /api/v1/netdisco/devices` (read-only, `db.get_net_devices`, фильтры `type`/`site`/`days`, clamp).
**Изменяемые модули:** `server/api.py`. **Новые:** `server/netdisco/inventory.py`.
**Контракты:** нет. **Структура данных:** `net_devices`.
**API:** `GET /api/v1/netdisco/devices`.
**Алгоритмы:** §4.6 агрегация; identity §4.1.
**Риски:** дубли agent vs ARP-self → identity-layer (MAC агента = агент, не unknown) как в netmap. LOW.
**Критерии завершения:** билдер+persist+API+тесты, gate.
**Критерии приёмки:** агент виден как `agent`; ARP-сосед без агента — `endpoint`/`unknown` с OUI-вендором; смена IP агента не плодит дубль; пустой флот → пустой инвентарь (не падение).
**План тестирования:** `tests/netdisco/test_inventory.py` (agent/endpoint классификация, dedup, OUI, пустой), `tests/netdisco/test_netdisco_api.py` (роут, фильтры, clamp, XSS-safe JSON).
**Файлы:** `inventory.py` + 2 теста + `api.py` (edit).
**Порядок коммитов:** (1) build_inventory+test → (2) persist+test → (3) API+test.

## Phase 4 — Config + Scheduler + background loop (OFF by default) ⬜
**Цель:** netdisco сам обновляет инвентарь по интервалу; каркас poll-type-петель; force-poll кнопка. Без новых probe (пока только rebuild из snapshots).
**Зависит от:** P3.
**Задачи:**
1. `netdisco/config.py`: `NetdiscoConfig` (frozen, `enabled=False`, интервалы 4-х поллов с clamp снизу 60с, `jitter_sec`, `active_scan=False`, `scan_cidrs/max_hosts/ports`, parallelism/rate caps, include/exclude CIDR, OUI allow/deny) + `load_netdisco_config(mapping)` (clamp/filter, RFC1918-CIDR).
2. `config.py` (`ServerConfig`): `netdisco_config()` + `netdisco_enabled`.
3. `netdisco/scheduler.py`: `run_inventory_cycle(...)` (build+persist, `_poll_lock` anti-DoS, injectable deps) + `poll_now(cfg)`.
4. `main.py` lifespan: `_netdisco_inventory_loop` (`asyncio.create_task`, `to_thread`, jitter, отмена на shutdown) + `app.state.netdisco_config`; гейт `cfg.netdisco_enabled`.
5. `api.py`: `POST /api/v1/discovery/poll` (force, bounded, lock-guard, как `/printers/poll`).
**Изменяемые модули:** `server/{main,api,config}.py`. **Новые:** `netdisco/{config,scheduler}.py`.
**API:** `POST /api/v1/discovery/poll`.
**Алгоритмы:** poll-separation §3.6, jitter/clamp/lock.
**Риски:** двойной цикл (loop+кнопка) → `_poll_lock` busy-return. Loop не должен падать (try/except + log, как `_run_printer_poll`). LOW-MED.
**Критерии завершения:** конфиг+scheduler+loop+API+тесты, gate; smoke с enabled.
**Критерии приёмки:** OFF→никаких циклов (регресс=0); ON→инвентарь обновляется; force-poll занят→`busy`; интервал<60→clamp.
**План тестирования:** `tests/netdisco/test_config.py` (clamp/filter/defaults), `tests/netdisco/test_scheduler.py` (cycle, lock busy, injectable), `test_netdisco_api.py` += force-poll.
**Файлы:** 2 новых + 3 edit + 2 теста.
**Порядок коммитов:** (1) config+test → (2) scheduler+test → (3) ServerConfig+main loop → (4) API force-poll+test.

## Phase 5 — Generalized active scan (за флагом active_scan) ⬜
**Цель:** активный bounded-скан сегмента → IP-кандидаты «живых» хостов; влить в discovery. Все safety-рейлы `scan.py`.
**Зависит от:** P4. **Security-review: ОБЯЗАТЕЛЕН (Opus).**
**Задачи:**
1. `netdisco/scan.py`: обобщить `printers/scan.py` — `scan(cfg)`/`expand_cidrs`/`host_is_alive(ip)` (TCP-connect к `cfg.scan_ports` ∪ SNMP-ping sysObjectID), RFC1918-double-check, cap hosts/workers, kill-switch, injectable `host_check`.
2. `netdisco/discovery.py`: `gather_candidates(snapshots, static_ips, scan_ips) -> list[Candidate]` (обобщённый `printers/discovery.merge`, identity-precedence).
3. `scheduler.py`: `run_discovery_cycle` (scan если `active_scan` + merge → upsert новых `endpoint`-устройств как `discovered`); петля `_netdisco_discovery_loop` в `main.py`.
**Изменяемые модули:** `main.py`, `scheduler.py`. **Новые:** `netdisco/{scan,discovery}.py`.
**Алгоритмы:** §3.1 (active+merge), safety §6.1.
**Риски:** скан = EDR-триггер → OFF + stop-gate + RFC1918 + bounded; **security-review**. MED (управляемо рейлами).
**Критерии завершения:** scan+discovery+cycle+тесты (инъекция, без реальной сети), gate, security APPROVE.
**Критерии приёмки:** `active_scan=False`→[]; публичный CIDR не сканируется; `max_hosts=0`→kill; найденные IP→персистятся как кандидаты; один битый хост не валит цикл.
**План тестирования:** `tests/netdisco/test_scan.py` (RFC1918-гард, cap, kill-switch, injectable), `test_discovery.py` (merge/dedup), `test_scheduler.py` += discovery cycle. Реальная сеть НЕ трогается.
**Файлы:** 2 новых + 2 edit + 2 теста.
**Порядок коммитов:** (1) scan+test → (2) discovery merge+test → (3) cycle+loop+test.

## Phase 6 — SNMP device profile + classification ⬜
**Цель:** опросить кандидатов по SNMP (reuse `printers/snmp`), определить тип (router/switch/ap/printer/endpoint) + вендор/модель/интерфейсы.
**Зависит от:** P5 (кандидаты). **Security-review: ОБЯЗАТЕЛЕН.**
**Задачи:**
1. `netdisco/oids.py`: стандартные OID (sysObjectID/sysServices/ipForwarding/dot1dBaseBridgeAddress/ifTable/entPhysicalSerial). Numeric only.
2. `netdisco/snmp_probe.py`: `probe_device(ip, session) -> DeviceProfile` (sys*, ifTable walk → interfaces, bridge/route признаки). Reuse `printers.snmp.SnmpSession`. Read-only.
3. `netdisco/classify.py`: `classify(profile, agent_macs) -> dev_type` (§4.2, UNKNOWN-first).
4. `netdisco/drivers/{__init__,standard}.py`: `standard`-драйвер (OID-карты пустые до hardware-verify, как принтеры) + `select_driver(sys_object_id)`.
5. `scheduler.py` `run_classify_cycle` + петля `_netdisco_classify_loop`.
**Изменяемые модули:** `main.py`, `scheduler.py`, `db.py` (store interfaces). **Новые:** `netdisco/{oids,snmp_probe,classify}.py`, `netdisco/drivers/*`.
**Алгоритмы:** §4.2 classify, sysServices битовая маска (numeric).
**Риски:** вендор-квирки → `standard`-драйвер + UNKNOWN; v3-only устройства (community fail) → `unmanaged`. MED.
**Критерии завершения:** probe+classify+drivers+cycle+тесты (фейковая SnmpSession), gate, security APPROVE.
**Критерии приёмки:** router по ipForwarding; switch по bridge-MIB+FDB; printer reuse; SNMP-немой→endpoint/unmanaged; vendor-OID сам по себе НЕ тип (UNKNOWN).
**План тестирования:** `tests/netdisco/test_snmp_probe.py` (фейк-session, ifTable parse), `test_classify.py` (все типы + UNKNOWN), `test_drivers.py`.
**Файлы:** 4+ новых + 3 edit + 3 теста.
**Порядок коммитов:** (1) oids+probe+test → (2) classify+test → (3) drivers+test → (4) cycle+loop+store interfaces.

## Phase 7 — Passive SNMP harvest (ARP/route) ⬜
**Цель:** из инфра-устройств walk ARP (`ipNetToMedia`) + routes (`ipCidrRoute`) → новые кандидаты + L3-улики, без пинга.
**Зависит от:** P6 (known infra). **Security-review: ОБЯЗАТЕЛЕН.**
**Задачи:**
1. `netdisco/harvest.py`: `harvest_arp(session) -> list[(ip,mac)]`, `harvest_routes(session) -> list[(cidr,next_hop,ifindex)]` (RFC1918-фильтр результатов).
2. `discovery.py`: влить harvest-кандидатов в `gather_candidates`.
3. `scheduler.py`: harvest в `run_discovery_cycle` для устройств type∈{router,switch}.
**Изменяемые модули:** `discovery.py`, `scheduler.py`. **Новые:** `netdisco/harvest.py`.
**Алгоритмы:** §3.1 passive harvest; RFC1918-only результаты.
**Риски:** большие ARP-таблицы → cap `max_rows` (как `snmp_walk`). LOW-MED.
**Критерии приёмки:** harvest даёт RFC1918-кандидаты; публичные next-hop отброшены; пустой/битый ответ→[].
**План тестирования:** `tests/netdisco/test_harvest.py` (фейк-walk, RFC1918-фильтр, cap).
**Файлы:** 1 новый + 2 edit + 1 тест.
**Порядок коммитов:** (1) harvest+test → (2) влить в discovery/cycle.

## Phase 8 — Evidence collection + L2 inference (LLDP/CDP/FDB) ⬜
**Цель:** собрать улики связей и вывести L2-линки (включая FDB-вывод к немым хостам).
**Зависит от:** P6. **Security-review: ОБЯЗАТЕЛЕН.**
**Задачи:**
1. `netdisco/oids.py` += LLDP/CDP/FDB/STP OID.
2. `netdisco/evidence.py`: `collect_evidence(device, session) -> list[LinkEvidence]` (LLDP `lldpRemTable`, CDP `cdpCacheTable`, FDB `dot1dTpFdbPort`+`dot1dBasePortIfIndex`).
3. `netdisco/l2.py`: `infer_edges(fdb, port_ifindex, infra_macs, own_mac) -> list[LinkEvidence]` (§4.3 алгоритм: edge/uplink/ambiguous, UPLINK_MAC_THRESHOLD).
**Изменяемые модули:** `oids.py`. **Новые:** `netdisco/{evidence,l2}.py`.
**Структура данных:** `LinkEvidence`.
**Алгоритмы:** §4.3 FDB-вывод (нестандартный), LLDP/CDP parse.
**Риски:** trunk-шум → threshold + confidence + ambiguous-пометка (не ложные edge). MED.
**Критерии приёмки:** 1 не-инфра MAC на порту→edge HIGH; >threshold/infra-MAC→uplink; LLDP-улика присутствует; мусор→[].
**План тестирования:** `tests/netdisco/test_evidence.py` (фейк-walk LLDP/CDP/FDB), `test_l2.py` (edge/uplink/ambiguous, property: порядок входа не влияет).
**Файлы:** 2 новых + 1 edit + 2 теста.
**Порядок коммитов:** (1) oids+evidence+test → (2) l2 inference+test.

## Phase 9 — Data Fusion + reconcile + persist links ⬜
**Цель:** примирить улики в один граф рёбер, сохранить `net_links` + `net_topology_snapshots`.
**Зависит от:** P8.
**Задачи:**
1. `netdisco/fusion.py`: `fuse(evidence) -> list[ResolvedLink]` (§4.4: группировка по паре, SOURCE_PRIORITY, confidence, node-merge по chassis-id, детерминированный tie-break).
2. `netdisco/reconcile.py`: `run_topology_cycle` — evidence→fuse→`replace_net_links`+`store_topology_snapshot`+upsert devices; петля `_netdisco_topology_loop`.
**Изменяемые модули:** `main.py`, `scheduler.py`. **Новые:** `netdisco/{fusion,reconcile}.py`.
**Структура данных:** `net_links`, `net_topology_snapshots`.
**Алгоритмы:** §4.4 fusion, §4.5 reconcile.
**Риски:** конфликт улик → LOW-confidence/ambiguous, не выдумка (UNKNOWN-first). MED.
**Критерии приёмки:** LLDP>FDB при конфликте; одинаковые улики в любом порядке→идентичный граф (детерминизм); snapshot append-only; rerun не плодит дубль-линки.
**План тестирования:** `tests/netdisco/test_fusion.py` (priority, node-merge, детерминизм), `test_reconcile.py` (persist round-trip, idempotent rerun).
**Файлы:** 2 новых + 2 edit + 2 теста.
**Порядок коммитов:** (1) fusion+test → (2) reconcile+loop+test.

## Phase 10 — Graph engine + reachability correlation + change detection ⬜
**Цель:** операции над графом, root-cause (unreachable vs down), журнал изменений, lifecycle исчезнувших.
**Зависит от:** P9.
**Задачи:**
1. `netdisco/graph.py`: `build_graph(devices, links)`, `neighbors`, `reachable_from(roots)` (BFS), `path_to_root`, `status_rollup`, `find_root_cause(down_set)`.
2. `netdisco/correlation.py`: `correlate(graph, down_set, roots) -> {device: DOWN|UNREACHABLE|suppressed, root_cause}` (§3.7, §1.5).
3. `netdisco/changes.py`: `diff(prev, curr) -> list[TopologyDelta]` (appeared/disappeared/link±/iface-down/reclassified) + lifecycle `missing`(N циклов)→`eligible_purge`; пишет `net_changes`.
4. `reconcile.py`/`scheduler.py`: вызвать changes.diff после snapshot; reachability-петля `_netdisco_reachability_loop` (status + correlate).
**Изменяемые модули:** `main.py`, `scheduler.py`, `reconcile.py`. **Новые:** `netdisco/{graph,correlation,changes}.py`.
**Структура данных:** `net_changes`.
**Алгоритмы:** §3.5 graph, §1.5 reachability, §3.13 change/ghost-lifecycle.
**Риски:** неверный граф→ложное подавление → confidence-cap MEDIUM (D5: blind-spot, не тревога); ложное «исчез» → N-cycle устойчивость (ghost-lesson). MED.
**Критерии приёмки:** шлюз down→за-ним unreachable+suppressed+1 root-cause; одиночный промах≠disappeared; DHCP-смена IP≠appeared/disappeared (identity); delta детерминирована.
**План тестирования:** `tests/netdisco/test_graph.py` (BFS/path/rollup), `test_correlation.py` (down vs unreachable, root-cause), `test_changes.py` (все дельты, ghost-lifecycle, identity-стабильность).
**Файлы:** 3 новых + 3 edit + 3 теста.
**Порядок коммитов:** (1) graph+test → (2) correlation+test → (3) changes+lifecycle+test → (4) wire в reconcile/reachability.

## Phase 11 — API surface + caching + telemetry ⬜
**Цель:** полный read-only API топологии, TTL-кэш графа, внутренняя телеметрия сканера.
**Зависит от:** P9, P10.
**Задачи:**
1. `netdisco/cache.py`: `GraphCache` (TTL 30–60с, read-through, `threading.Lock`, инвалидация по новому snapshot).
2. `netdisco/metrics.py`: счётчики цикла (candidates/probes/snmp_ok/timeout/links/deltas/duration), thread-safe.
3. `api.py`: `GET /topology/graph` (из кэша), `/topology/changes?days`, `/netdisco/devices/{id}` (интерфейсы+линки+reading-серия), `/netdisco/stats`.
4. `correlation` read-side аннотация на device-странице (как `subnet_context_for`) + модулятор `network_risk`-confidence (blind-spot, без новых алармов).
**Изменяемые модули:** `api.py`, `network_risk.py` (опц. confidence-cap при unreachable), device-render. **Новые:** `netdisco/{cache,metrics}.py`.
**API:** `/topology/graph`, `/topology/changes`, `/netdisco/devices/{id}`, `/netdisco/stats`.
**Алгоритмы:** §3.15 cache, §3.16 telemetry.
**Риски:** кэш-рассинхрон → TTL + инвалидация по snapshot-id. LOW.
**Критерии приёмки:** graph из кэша (2-й вызов не пересобирает); stats растут по циклам; changes отдаёт журнал; device-страница показывает unreachable-аннотацию; SQL параметризован, clamp days.
**План тестирования:** `tests/netdisco/test_cache.py` (TTL, инвалидация), `test_metrics.py`, `test_netdisco_api.py` += graph/changes/detail/stats.
**Файлы:** 2 новых + 2-3 edit + 3 теста.
**Порядок коммитов:** (1) cache+test → (2) metrics+test → (3) API роуты+test → (4) device-аннотация.

## Phase 12 — Web UI topology page + map renderer ⬜
**Цель:** страница «Топология» — инвентарь (SSR-таблица) + canvas-граф настоящих линков; карточка устройства; журнал изменений. Skill: `frontend-design`.
**Зависит от:** P11.
**Задачи:**
1. `web/templates/topology.html`: SSR-таблица инвентаря (доступная форма) + JSON-остров `{{ graph|tojson }}` + canvas-движок (переиспользовать `netmap.html`: mulberry32, пружины, темы, reduced-motion). Узлы по `dev_type` (router=ромб/switch=квадрат/ap=треугольник/agent=круг/endpoint=точка), рёбра по confidence/kind. XSS: `srpEsc`/`tojson`, autoescape ON.
2. `web/dashboard.py` + route `/topology`; nav-ссылка в `base.html`.
3. Карточка устройства (расширить `device.html` или новый `net_device.html`): интерфейсы/линки/статус/история/изменения.
**Изменяемые модули:** `web/dashboard.py`, `base.html`, `device.html`. **Новые:** `web/templates/topology.html` (+ опц. `net_device.html`).
**API:** использует Phase-11 роуты.
**Алгоритмы:** рендер §3.11.
**Риски:** XSS из agent/SNMP-строк → `srpEsc`/`tojson` (паттерн netmap, memory `dashboard-xss-srpesc`). MED → security/code-review.
**Критерии приёмки:** граф рисует реальные линки; типы-глифы; темы/reduced-motion; agent-строка `</script>` не рвёт остров (XSS-пин); SSR-форма доступна без JS.
**План тестирования:** `tests/netdisco/test_topology_web.py` (SSR-рендер, JSON-остров round-trip, XSS-пин, пустое состояние); webapp-testing (playwright) — визуальная проверка вручную.
**Файлы:** 1-2 новых шаблона + 3 edit + 1 тест.
**Порядок коммитов:** (1) topology.html+route+test → (2) nav → (3) device-card.

## Phase 13 — Credentials hardening (DPAPI store) ⬜ [SAFETY]
**Цель:** непубличная SNMP community — через DPAPI-шифрованное хранилище, не plaintext. **Security-review: ОБЯЗАТЕЛЕН.**
**Зависит от:** P6 (где community используется). До этой фазы — только community=`public` (безопасный дефолт).
**Задачи:**
1. `netdisco/credentials.py`: `CredentialStore` — `set/get` SNMP community/secret через DPAPI (`ctypes` обёртка `CryptProtectData/CryptUnprotectData`, machine-scope, **stdlib only**); файл секретов только зашифрован; не-Windows fallback = только `public`.
2. `config.py`/`scheduler.py`: брать community из store по `CredentialRef`; в логи/API/дашборд community НЕ выводить.
3. `CredentialRef`-структура с заделом под SNMPv3 (user/authKey/privKey) — не реализуем, только модель.
**Изменяемые модули:** `config.py`, `scheduler.py`, `snmp_probe.py`. **Новые:** `netdisco/credentials.py`.
**Алгоритмы:** §3.18.
**Риски:** ctypes-DPAPI кросс-версийность → обернуть в try, fallback на public + явный warning; bandit на ctypes — `# nosec` с причиной. MED → security-review.
**Критерии приёмки:** community на диске только зашифрована; `get` расшифровывает; community не утекает в логи/ответы; не-Windows→public-only; `public` остаётся допустимым без store.
**План тестирования:** `tests/netdisco/test_credentials.py` (round-trip encrypt/decrypt с фейк-DPAPI на не-Windows, no-leak, public-fallback).
**Файлы:** 1 новый + 3 edit + 1 тест.
**Порядок коммитов:** (1) credentials store+test → (2) wire в config/probe (no-leak).

---

## Опционально (за горизонтом 12 — отдельные RFC при необходимости)
- **P14 Agent-side enrich:** `client/collectors/network.py` += локальный LLDP/маршруты в `historical` (additive-optional контракт §5.1, stdlib, WinPS5.1, RFC1918). Усиливает топологию данными с края.
- **P15 SNMPv3 + trap-listener:** аутентифицированный SNMP + bounded UDP-162 listener (anti-spoof как `snmp._transact`).
- **P16 Saved map positions / экспорт топологии.**

## Глобальные критерии готовности подсистемы
- Каждая фаза: gate green (ruff/mypy/bandit/cov≥80%/smoke) + CHANGELOG-строка + Phase Summary в CONTINUITY + review-verdict.
- OFF-by-default на всём протяжении; включение — явный серверный флаг.
- Zero-regression: при выключенном netdisco поведение SRP идентично сегодняшнему (тест-инвариант).
- Ни один инвариант §5 CLAUDE.md не ослаблен.
