---
name: netmap-unified-api
description: Ф3 netmap-unification — единый API /api/v1/network-map/graph + GraphCache loader→ассемблер; /netmap·/topology/graph = deprecated-алиасы; invalidate после poll; cache в create_app
metadata:
  type: project
---

Ф3 треда `[[netmap-unification]]`: один канонический граф-контракт. Зависит от `[[netmap-unified-assembler]]` (Ф2). Контракт агента НЕ тронут.

**Why:** раньше карта сети (`/api/v1/netmap`, кластерная модель) и карта топологии (`/api/v1/topology/graph`, граф снимка) жили по разным формам → две модели, расходящиеся данные. Теперь один источник истины.

**How to apply:**
- Канонический эндпоинт `GET /api/v1/network-map/graph` → единый граф `{nodes,links,subnets,totals}` через `_network_map_graph(request)`.
- `server/netdisco/cache.py::load_network_map()` = ДЕФОЛТНЫЙ loader `GraphCache`: DB fan-out (`db.get_net_devices`/`get_net_links`/`get_network_snapshots`/`get_printers`) → `build_network_map`. Loader НИКОГДА не возвращает None (пустой флот → пустой well-formed граф) — `get()` возвращает сам граф (не обёрнутый в `{graph, received_at}`).
- `GraphCache(ttl_sec=45)` read-through single-slot: `get()` держит `self._lock` на весь rebuild → холодный/expired rebuild блокирует concurrent readers (один build на TTL-окно by design; serve-stale = future opt-in). `invalidate()` сбрасывает.
- Deprecated-АЛИАСЫ: `GET /api/v1/netmap` и `GET /api/v1/topology/graph` → тот же `_network_map_graph` (СТРУКТУРНО один кэш-объект → идентичный граф, не совпадение). ФОРМА ОТЛИЧАЕТСЯ от старой: теперь `nodes/links/subnets/totals`, НЕТ `graph`-обёртки, НЕТ `received_at` (отметка осталась на странице `/topology` через SSR snapshot).
- Invalidate после force-poll: `/discovery/poll` и `/topology/poll` зовут `_invalidate_network_map_cache(request)` ПОСЛЕ работы (свежий inventory/links видны сразу, не ждать TTL). Фоновые циклы НЕ инвалидируют (TTL 45с поглощает steady-state).
- `create_app` (main.py) создаёт `app.state.network_map_cache = GraphCache()` up-front → P11-LOW ленивый init в хэндлере снят (хэндлер оставил `getattr(...) or GraphCache()` fallback только для app вне `create_app`). Граф грузится на первом `get()` (cold start не блокирует на инициализацию).
- Веб-страницы `/netmap` (`build_netmap`) и `/topology` (`get_latest_topology_snapshot`) НЕ тронуты — они переходят на единый граф в Ф4 (canvas `_netgraph.html`).

**Безопасность:** API = `application/json` (браузер не парсит как HTML → `</script>` в hostname инертен). Кэш не отравляется request-вводом (граф целиком из БД, нет per-request ключа). В графе только inventory-поля (nid/dev_type/ip/hostname/mac/vendor/model/status/subnet/card_url/provenance) + оверлеи; нет секретов/community/публичных-IP. RFC1918-only на сборе.

**Карта кода:** loader/кэш `server/netdisco/cache.py` (`load_network_map`/`GraphCache`); роуты `server/api.py` (`_network_map_graph`/`network_map_graph`/`netmap` alias/`topology_graph` alias/`poll_discovery`+`poll_topology`→`_invalidate_network_map_cache`); старт `server/main.py::create_app` (up-front cache). Тесты `tests/test_netmap_unified_api.py` (8: форма/empty/кэш-no-rebuild-в-TTL/reload-после-TTL/алиасы=тот-граф/invalidate-after-оба-poll/XSS-inert). Merge `9c6ae9f`, gate cov 92.56%, code-review APPROVE-WITH-FIXES (0 crit/high).
