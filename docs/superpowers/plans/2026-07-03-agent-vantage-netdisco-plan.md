# План: усиление сетевых/исследовательских возможностей SRP (agent-vantage netdisco)

_Составлен 2026-07-03 после живого анализа реальной площадки (LAN `192.168.9.0/24`)._

## 1. Что выяснено про сеть — чего программа сейчас не видит

Сеть: `192.168.9.0/24`, шлюз `.1` (Keenetic, OUI `50:FF:20`), DNS `10.8.8.8/.9` (**вне подсети** → инфра на другом сегменте). Хост туннелирует весь egress через VPN-стек (OpenVPN + Outline `10.0.85.2/32` + tun2socks; в таблице маршрутов — anti-leak-роуты bogon-диапазонов в туннель).

Живые замеры с этого хоста:

| Сигнал | Программа сейчас | Добыто с хоста | Ценность |
|---|---|---|---|
| Имена хостов | сервер шлёт reverse-DNS → **0/14 PTR** тут | NetBIOS: `.6`=MEDPOST, `.25`=SKPD3, `.100`=I3 | ops+research |
| Тип устройства | SNMP/banner **с сервера** (off-LAN → таймаут) | port-touch 400мс: `.1`=router(80/443/53/22/23), `.189`=Kyocera-printer(9100/515/631), `.6`=windows(135/139/445) | ops |
| Таблица маршрутов | **не собирается вовсе** | VPN-туннели, split-tunnel, достижимые подсети | research |
| VPN/туннель-адаптеры | `kind="other"`, без семантики | OpenVPN/Outline/tun2socks | research+sec |
| mDNS/SSDP/WSD | collectors **на сервере** (multicast не маршрутизируется) | хост в группах `224.0.0.251`/`239.255.255.250` — live на сегменте | ops |
| Полный список хостов | ARP-**кэш** агента (только с кем говорили) | ARP-свип /24 даст всех живых | ops |

## 2. Корневой разрыв

**Агент — единственный хост с L2-смежностью к целевой LAN, но самый слабый discoverer.** Весь enrichment (SNMP, mDNS/SSDP/WSD, NetBIOS, banner, active-scan) живёт в `server/netdisco/`, запускается **с сервера**. Для площадки, где центрального сервера физически нет:
- multicast (mDNS/SSDP/WSD) не пересекает роутер → `run_passive_cycle` пуст;
- unicast RFC1918 не маршрутизируется к серверу → SNMP/banner/NetBIOS таймаутят;
- работает только ARP-инвентарь, релеенный агентом.

**Фикс-тезис:** перенести link-local/passive-discovery НА агента, релеить структурный результат в тот же серверный pipeline идентичности/fusion (`_apply_passive_hints`/`fill_net_device_identity`). Переиспользование, не новая логика.

## 3. План изменений — по убыванию (signal ÷ diff)

### P0 — расширить сетевой коллектор агента (наибольший сигнал, наименьший diff)
Файлы: `client/collectors/network.py` (`_NET_SCRIPT`+парсеры) · `shared/schema.py` (аддитивно) · приём в `server/netdisco/inventory.py::build_inventory`.

1. **NetBIOS-имя соседа** — `nbtstat -A <ip>` по RFC1918-соседям; парсить по **числовому суффиксу** `<20>`/`<00>`, не по локализуемым словам (`[[language-independence]]`). Поля `neighbor.name`+`name_source="netbios"`.
2. **Таблица маршрутов** — `Get-NetRoute` → `network_routes[]` (dest_prefix,next_hop,if_index,metric); приватность-фильтр как у neighbors.
3. **Роль адаптера** — по `desc`/`name`: `role` (`lan|wifi|tunnel|virtual`) + `tunnel` bool (OpenVPN/Outline/tun2socks/TAP/WireGuard).
4. *(опц.)* **port-touch-типизация** соседей: 3–5 портов → `dev_type` там, где SNMP недостижим.

Сервер: `build_inventory` сеет `hostname` из `neighbor.name`; источник `agent_netbios` в `_HOSTNAME_PRIO` выше `reverse_dns`. **Без bump `CONTRACT_VERSION`** (аддитивно, прецеденты `liveness`/`update_status`).

### P1 — релей multicast-дискавери с агента (mDNS/SSDP/WSD)
Новый `client/collectors/lan_discovery.py` (**stdlib socket, не PS**). Отвечает на мёртвый off-LAN `collect_mdns/ssdp/wsd`.
- **Критично** (доказано промахом SSDP при анализе): биндить сокет явно на каждый RFC1918-адаптер, иначе multicast уходит в VPN-туннель.
- Cap/бюджет как в серверном `passive`. Релей → `passive.PassiveHint` → существующий fill.

### P2 — ограниченный ARP+port-свип локального /24 (за существующим разрешением)
Stop-gate снят владельцем письменно 2026-06-19 (RFC1918-only, hard-cap, `[[printer-active-scan-authorized]]`). Зеркалит серверный `scan.py`. Гейт: явный флаг как `active_scan`.

**Механизм (first-principles, 2026-07-05):** буквальные ARP-фреймы не нужны — TCP-connect к живому хосту ЗАСТАВЛЯЕТ Windows разрешить его MAC (ARP) независимо от того, открыт ли порт; результат уже читает `Get-NetNeighbor` в существующем `_NET_SCRIPT`. Значит P2 = триггер, не новый транспорт данных: подёргать порты по своей /24 → существующий `network_neighbors`-конвейер (парсинг/privacy-фильтр/NetBIOS-имя/relay/fusion, весь уже готов с P0) сам подхватит новые записи. **Ноль новых полей схемы.**

Порядок внутри `collect_network()` (chicken-and-egg с ролью адаптера): `_NET_SCRIPT` уже даёт adapters(+role из T3)+neighbors(до свипа)+routes+quality за один проход → если `active_scan`: `lan_scan.sweep(_lan_adapter_ips(adapters))` (переиспользует T3/P1-гейт «role∈{lan,wifi}», НЕ RFC1918-принадлежность IP — та же VPN-tunnel-ловушка: свип через туннель означало бы сканирование чужой, неавторизованной сети) → затем **малый** второй PS-вызов `_NEIGHBOR_RESCAN_SCRIPT` (только `Get-NetNeighbor`, не весь `_NET_SCRIPT` — полный скрипт содержит Test-Connection quality-блок с бюджетом до ~48с, дважды прогонять дорого) → merge по ip (rescan-pass поверх). Полностью best-effort/fail-open на каждом шаге (сеть жив без свипа как и раньше).

Цели/порты НЕ конфигурируются (G2 исходной спеки `2026-06-11-network-phase3-active-scan.md`): фиксированный список портов в коде (80, 443, 445, 3389, 9100), CIDR всегда только auto-derived `.0/24` вокруг собственных lan/wifi-адресов (никакого `scan_cidrs`-эквивалента на клиенте — иначе это универсальный сканер, не самопроверка своего сегмента).

Новый `client/collectors/lan_scan.py` (pure stdlib socket/ipaddress/concurrent.futures, зеркалит `server/netdisco/scan.py`/`server/printers/scan.py` структурно, но БЕЗ SNMP — агент лёгкий, SNMP — не-цель). `ClientConfig.active_scan: bool` (новое поле). Функция-параметр `collect_network(active_scan: bool = False)`/`collect_historical(active_scan: bool = False)` остаётся False по умолчанию (тесты никогда не сканируют реальную сеть — тот же паттерн, что `PrinterConfig`/`NetdiscoConfig`); `client/agent.py` привязывает через `partial(collect_historical, active_scan=cfg.active_scan)` (тот же паттерн, что уже `print_jobs`/`autoenable`). `ClientConfig.active_scan` дефолтит `True` в коде (dataclass) — по правилу «no dormant features»: сервер-сторонние `printers.active_scan`/`netdisco.active_scan` уже `true` в shipped-конфиге, разрешение владельца 2026-06-19 прямо включает «по умолчанию»; так же как и в server/config.json, никакого отдельного «шаблон-файла» для клиента нет — dataclass-дефолт И ЕСТЬ эффективный дефолт свежего инсталла.

### Не-цели (YAGNI)
- SNMP с агента — нет (агент лёгкий; SNMP на сервере для достижимой инфры).
- Инвестиции в reverse-DNS — нет (тут 0 PTR).
- Новые зависимости / bump контракта / новый msg_type — нет (всё аддитивно; `[[agent-stdlib-only]]` держится — только stdlib socket/subprocess).

## 4. Guardrails (R4)
- **Приватность:** только RFC1918 покидает агента. NetBIOS-имена = самозаявленные hostname LAN-пиров (класс уже релеемых MAC/ARP); таргетить только RFC1918. VPN-туннель классифицируем, но внешние endpoint-IP туннеля НЕ отдаём (только `tunnel=true`+локальный конец).
- **PS 5.1 floor:** `nbtstat`/`Get-NetRoute` есть в 5.1; парсить числовые суффиксы/enum (`[[agent-powershell-51-floor]]`).
- **Схема:** additive-optional в `shared/schema.py`, pydantic на границе, cap ≤ `NET_*_MAX`.
- **Ревью:** `security-reviewer` (Opus) обязателен перед мержем. Гейты §6 зелёные + smoke.

## 5. Порядок исполнения
P0 → аудит → мерж → P1 → аудит → мерж → P2 (за флагом) → аудит → мерж. Каждая фаза: TDD (RED→GREEN) → subagent security-review → фикс → gate green → merge --no-ff → push.
