---
name: netmap-unified-assembler
description: Ф2 netmap-unification — netdisco/unified.build_network_map: один чистый суперсет-граф (хребет+FK+оверлеи); дедуп по device_nid; nd-unknown не размещается
metadata:
  type: project
---

Ф2 треда `[[netmap-unification]]`: `server/netdisco/unified.py::build_network_map(net_devices, net_links, snapshots, printers)` — единый read-side ассемблер. netdisco = хребет (узлы + реальные L2/L3-связи), netmap = только оверлеи (agent-uplink рёбра, ICMP-качество `quality_overlay`, субсеть-аномалия `subnet_anomaly`). НЕ вторая модель. Чистая функция над уже прочитанными входами; БД/сеть читает слой API/кэша (Ф3).

**Why:** убирает дублирование «две модели топологии»; одно физ.устройство = один узел → карта и каноническая карточка совпадают.

**How to apply:** канонический ключ узла = `device_nid`. Дедуп: FK (`by_device_id`/`by_printer_id`, Ф1) → `by_mac` → `by_ip` → mint `device_nid` (MAC>IP). `nd-unknown` НЕ размещается (нет MAC/IP → skip, как `link_identities`) — иначе коллизия на null-бакете молча теряет устройство (HIGH-баг из code-review). Шлюз/агент/принтер переиспользует существующий nid (не дублирует net_device); висячий конец net_link → стаб-узел, `_ensure_gateway` апгрейдит стаб `dev_type` unknown→router. `medium` = Ф2-заглушка (линк к AP→wireless, uplink по kind адаптера, l3 по link_kind) — реальные client→AP wireless-рёбра в Ф7. Хелперы `subnet_hint`/`quality_overlay`/`subnet_anomaly` — в `analytics/netmap.py` (вынесены из `build_netmap`, вывод byte-identical; `dashboard._printer_subnet` удалён→общий `subnet_hint`). Вывод `{nodes,links,subnets,totals}` — надмножество формы снимка `reconcile._graph` (Ф4 рисует обе старые страницы из него). Зависит от `[[netmap-identity-spine]]`.
