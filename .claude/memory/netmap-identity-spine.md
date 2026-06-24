---
name: netmap-identity-spine
description: Ф1 netmap-unification — net_devices +device_id/printer_id soft-FK (MAC-хребет); link_identities; cleanup обнуляет FK (узел не удаляется)
metadata:
  type: project
---

Ф1 треда `[[netmap-unification]]`: одно физ.устройство ↔ один узел через FK-связку по нормализованному MAC.

**Что есть:** `db.net_devices +device_id +printer_id` (аддитивно nullable, идемпотентная legacy-миграция через `PRAGMA table_info`, индексы `idx_netdev_mac`/`idx_netdev_device_id` создаются ПОСЛЕ миграции колонок). `netmap.agent_mac_index(snapshots)` — единый публичный индекс MAC→device_id (дубль `inventory._agent_macs` удалён; scheduler/inventory берут отсюда). `identity.link_identities(net_devices, agent_macs, printers)` — join по `oui.normalize_mac` с обеих сторон; IP-резерв ТОЛЬКО для MAC-less строк и ТОЛЬКО приватный (`_canon_ip`+is_private), no-false-positive на mismatch, `nd-unknown` пропускается. `set_net_device_links` (COALESCE-preserve — транзиентный промах не стирает FK) + `get_net_device_links`. Связка в конце `run_inventory_cycle` под `_poll_lock`, best-effort (try/except+log).

**Why:** общий ключ (нормализованный MAC) раньше нигде не персистился → одно устройство = до 3 несвязанных записей (devices/net_devices/printers).

**How to apply:** `net_devices` НЕ в `_DEVICE_TABLES` (узел keyed-by-MAC, не agent-owned) → на delete агента/принтера soft-FK ОБНУЛЯЕТСЯ (`device_id`/`printer_id`=NULL), узел ОСТАЁТСЯ, нет висячих указателей (`[[device-identity-cleanup-not-continuity]]`). Контракт агента не тронут (нет bump). Потребитель FK — ассемблер Ф2 `[[netmap-unified-assembler]]` (читает device_id/printer_id прямо со строк net_devices).
