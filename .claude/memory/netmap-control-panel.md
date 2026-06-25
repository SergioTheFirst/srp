---
name: netmap-control-panel
description: Тред NETMAP-UNIFICATION Ф5 — операторская панель управления «гениальной карты»: фильтры/слои/раскладки/боковая-панель/сохранить-вид/экспорт + ADVANCED (изоляция/путь/первопричина/машина-времени); что где живёт и как не сломать
metadata:
  type: project
---

Ф5 (R3+review+XSS, смёржена origin/main): единый canvas-движок `_netgraph.html` (Ф4) получил операторскую панель. ВСЁ клиентское поверх единого графа Ф2 (`{nodes,links,subnets,totals}`), ноль новых серверных моделей — кроме read-only «машины времени».

**Сервер (только машина времени, read-only, security-reviewed Opus=APPROVE-WITH-NITS):**
- `db.list_topology_snapshots(limit)` (newest first, `limit` clamp 1..500, параметризован) + `db.get_topology_snapshot(snapshot_id)` (bound int, fail-closed None на non-positive/garbage/missing; рядом с `get_latest_topology_snapshot`).
- `GET /api/v1/network-map/snapshots` → список для слайдера (только id/received_at/counts, без тяжёлого blob).
- `GET /api/v1/network-map/graph?at=<id>` → исторический кадр, нормализованный в unified-форму **через единый helper `netdisco/unified.py::historical_graph_from_snapshot(snap)`** (ОДИН источник формы — API и SSR `/netmap?at=` зовут его оба, поэтому плашка `history_at` не расходится между роутами). **КРИТИЧНО:** `?at` МИНАЕТ live `GraphCache` (исторический кадр НИКОГДА не отравляет кэш; `test_api_graph_at_bypasses_live_cache` пинит). Плохой/отсутствующий id → 404 (не 500). Live-оверлеи (качество/аномалия/identity-FK) для исторического кадра НЕ считаются (D5: no false confidence в stale frame) — `subnets:[]`, totals agents/printers/anomalies=0. Контракт агента НЕ тронут.

**Клиент (всё в `_netgraph.html`, состояние = один объект `state`):**
- Фильтры: типы/статус/medium/confidence/provenance + подсеть(select) + текстовый поиск (`netgraph-search`); пресеты (только инфра / скрыть конечные / **скрыть неподтверждённые** = переинтерпретация «скрыть ARP-only» для unified-графа, т.к. ARP-only узлы в Ф2 уже исключены / сброс). Предикаты `nodeOk(n)` (узел) + `linkOk(L)` (ребро: medium+confidence+слой).
- Слои (L2/L3/uplink/wireless/quality/groups/ports) тогглят видимость рёбер/оверлеев.
- Раскладки: free / subnet (вертик. колонки) / type (ряды) — целевые точки как лёгкая сила в `tick()`. Персист позиций: `localStorage["srp-netmap-positions"]` (по nid), восстановление при загрузке, запись при drag-release/«закрепить всё».
- Боковая панель `ng-side` на клик (`showSide(n)`): факты + кнопка «→ карточка устройства» (`card_url` agent>printer>net). Навигация — по кнопке, НЕ по слепому клику (контекст карты держится).
- Tooltip ребра (`showEdgeTip`): via_source/confidence/medium/порты/качество. Подсветка соседей при hover/выборе.
- Сохранить вид: `localStorage["srp-netmap-view"]` = {filters,layers,layout}. restoreView **merge'ит** сохранённые ключи поверх дефолтов (не wholesale replace — будущий новый тоггл не обнулится).
- Экспорт: PNG (`canvas.toBlob`), CSV (nodes+links, **формульная защита** `csvSafe` как printview `_csv_safe`: экранирование `= + - @ \t \r`), JSON (весь граф `G`).

**ADVANCED (клиентская BFS над `adj` = {[nid]: [соседи]}):**
- Изоляция N-hop (`isolateSet()` — корректный `seen[root]=0` computed-key; `state.isolateRoot`/`state.isolateN`).
- Путь (`pathBfs(a,b)` — **ВАЖНО:** computed-key sentinel `prev[a]=null`, НЕ объект-литерал `{a:null}`; regression-пин `test_netmap_path_engine_terminates` ловит баг-форму, которая зацикливала вкладку).
- Первопричина (`causeResult()` — down-узлы, roots=router/agent/gateway, causes соседствуют с up-reachable; подсветка: cause=пульс bad, UNREACHABLE=dim).
- **КРИТИЧНО (perf):** ADV-результаты frame-invariant → кэшируются в `navCache`, пересчёт только в `recomputeNav()` на смене state (`setMode`/`handleClick`/isoN), НЕ каждый кадр (code-review MEDIUM).
- Машина времени: слайдер по `window.__NETMAP_SNAPSHOTS` (JSON-остров `netmap.html`, `|tojson`) → `?at=<id>`; плашка «исторический кадр» + кнопка «вернуться к живой карте» (рендерится по `IS_HISTORY = !!G.history_at`).

**XSS-контур (Opus verified PASS):** 0 `innerHTML`/`insertAdjacentHTML`/`outerHTML`. Все agent/SNMP-строки (hostname/ip/mac/vendor/port/via_source/received_at) → только `textContent` (боковая панель через `kv()`, tooltip через `row()`, плашка через `span.textContent`, canvas через `fillText` — не HTML-sink). Граф + snapshots → `|tojson` (autoescape ON → `</script>` breakout инертен). Click-through = только `card_url` (серверный из ассемблера, всегда `/device/`|`/printers/`|`/netdisco/device/` — нельзя собрать `javascript:`). `window.location.href` — только сервер-относительные пути + `encodeURIComponent`. CSV — формульная защита.

**Тесты:** `test_netmap_web.py` (+6: панель/пресеты/машина-времени-mount/исторический-кадр-SSR/no-innerHTML/XSS-pin×2 + 2 regression-пина pathBfs/plaque-on-SSR), `test_netmap_history.py` (20: db readers + API `?at`/`/snapshots`/cache-bypass/404). Gate green cov 92.82%. Reviews: Sonnet REQUEST-CHANGES→исправлено (CRITICAL pathBfs infinite-loop + HIGH plaque divergence через shared-helper + MEDIUM navCache + 6 LOW); Opus security APPROVE-WITH-NITS (0 crit/high, 2 LOW-нита применены).

Related: [[netmap-unification]] (Ф1–Ф4 статус), [[netmap-unified-assembler]], [[netmap-unified-api]], [[dashboard-xss-srpesc]].
