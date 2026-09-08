"""Вердикт SRP -> значения элементов данных Zabbix. Позитивный список полей.

**Принцип (директива владельца 2026-09-08): в Zabbix уходит ВЫВОД, а не данные.**
Всё, что помогает понять «почему» — оси, координаты, доверие, тренды, SMART,
журналы, история — остаётся в SRP, там и смотрят. Наружу уезжают ровно четыре
ключа на машину и один на сам SRP.

Что не уходит НИКОГДА и почему:

* серийники дисков, материнских плат и принтеров — идентифицируют физическую
  единицу, Zabbix они не нужны, а риск утечки реален;
* MAC-адреса, IP, DNS-имена соседей — свою сеть Zabbix знает и сам, чужая
  топология из SRP ему не нужна;
* имена учётных записей и залогиненных пользователей, ФИО владельцев ПК,
  инвентарные и кабинетные привязки;
* сырые SMART-атрибуты, журналы событий, списки процессов и ПО — это объём,
  а не сигнал;
* история — только текущий вердикт, тренд Zabbix построит сам;
* внутренние поля SRP: версия агента, идентификатор установки, хеши, версии схемы.

Инвариант «UNKNOWN over false confidence» на проводе: в траппер нельзя послать
«неизвестно», а послать 0 — значит объявить слепую машину идеально здоровой.
Поэтому неизвестное числовое значение НЕ ОТПРАВЛЯЕТСЯ вовсе, а текстовый
``srp.state`` отправляется всегда, в том числе со значением ``unknown``, —
иначе машина исчезла бы из мониторинга молча.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from server.analytics.health import apply_health_staleness
from server.db import display_name
from server.zabbix.config import ZabbixConfig
from server.zabbix.protocol import MAX_HOST_CHARS, Item, fmt_value

#: Ровно четыре ключа на хост парка. Больше в Zabbix не уходит ничего.
CORE_KEYS = ("srp.state", "srp.risk", "srp.days_left", "srp.reason")

#: Единственный ключ на хост самого SRP: время последнего расчёта, чтобы Zabbix
#: видел, что SRP жив. Формат — unix-время (штатный тип элемента в Zabbix).
EXPORT_KEYS = ("srp.last_run",)

#: Причины, по которым машина не попала в пакет (машинные значения — английские).
SKIP_NO_NAME = "no_name"
SKIP_DUPLICATE_NAME = "duplicate_name"
SKIP_BACKOFF = "unknown_to_zabbix"

_SKIP_LABELS = {
    SKIP_NO_NAME: "не удалось определить имя узла",
    SKIP_DUPLICATE_NAME: "имя узла совпадает с другой машиной",
    SKIP_BACKOFF: "узел неизвестен Zabbix, отправка отложена",
}

_UNKNOWN_REASON = "нет видимости: SRP не получает достаточно данных об этой машине"
_NO_DATA_REASON = "данных недостаточно для вывода"


@dataclass(frozen=True)
class Skipped:
    device_id: str
    host: str
    reason: str

    @property
    def label(self) -> str:
        return _SKIP_LABELS.get(self.reason, self.reason)


@dataclass(frozen=True)
class Packet:
    items: list = field(default_factory=list)
    hosts: list = field(default_factory=list)  # (host, device_id) в порядке пакета
    skipped: list = field(default_factory=list)  # list[Skipped]
    unknown: int = 0  # машин в состоянии unknown (для карточки SRP, не для Zabbix)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_host(name: str) -> Optional[str]:
    """Привести имя узла к безопасному виду или отвергнуть его.

    Имя приходит из ``hostname`` агента, а ``/ingest`` в поставке не
    аутентифицирован — то есть это недоверенная строка. Без чистки она уезжает
    и в чужую систему, и в журнал сервера (``\\n`` подделал бы строки журнала),
    и в HTML карточки, а длина не ограничена ничем.
    """
    cleaned = "".join(ch for ch in (name or "") if ch.isprintable()).strip()
    if not cleaned:
        return None
    return cleaned[:MAX_HOST_CHARS]


def resolve_host(row: dict, cfg: ZabbixConfig) -> Optional[str]:
    """Имя узла Zabbix для машины, либо ``None``, если имя не определяется.

    Приоритет: явное соответствие ``host_map`` (исключения для парков, где хосты
    названы инвентарными кодами) -> выбранное ``host_field`` -> суффикс.
    """
    device_id = str(row.get("device_id") or "")
    mapped = cfg.host_map.get(device_id)
    if mapped:
        return sanitize_host(mapped)
    if cfg.host_field == "device_id":
        base = device_id
    elif cfg.host_field == "display_name":
        base = display_name(
            row.get("hostname"),
            model=row.get("model"),
            chassis=row.get("chassis"),
            device_id=device_id,
            disambiguate=True,
        )
    else:
        base = str(row.get("hostname") or "").strip()
    clean = sanitize_host(base)
    if clean is None:
        return None
    if not cfg.host_suffix:
        return clean
    # Суффикс не должен молча теряться на длинном имени: место под него
    # освобождается за счёт базовой части, а не наоборот.
    suffix = sanitize_host(cfg.host_suffix) or ""
    room = max(0, MAX_HOST_CHARS - len(suffix))
    return sanitize_host(f"{clean[:room]}{suffix}")


def _fresh_state(row: dict, now: datetime) -> str:
    """Состояние с наложенным оверлеем устаревания.

    ``get_fleet_verdicts`` отдаёт сохранённое состояние СЫРЫМ: без этого шага
    вердикт двухнедельной давности уехал бы в Zabbix как свежий «h0 — здоров».
    """
    overlaid = apply_health_staleness(
        {"state": row.get("state") or "unknown"}, row.get("score_ts"), now
    )
    return str(overlaid.get("state") or "unknown")


def _reason(row: dict, state: str) -> str:
    """Главный фактор одной короткой фразой — БЕЗ единого идентификатора.

    Собирается только из словарных подписей вердикта (``dominant_label``
    и ``action`` — обе из фиксированных таблиц ``server/analytics/health.py``),
    поэтому ни имени машины, ни модели диска, ни имени пользователя сюда
    попасть не может по построению.
    """
    if state == "unknown":
        return _UNKNOWN_REASON
    parts = [str(row.get("dominant_label") or "").strip(), str(row.get("action") or "").strip()]
    text = ": ".join(p for p in parts if p)
    return text or _NO_DATA_REASON


def build_items(row: dict, cfg: ZabbixConfig, host: str, now: datetime) -> list:
    """Четыре значения для одной машины. Числовое «неизвестно» не отправляется."""
    state = _fresh_state(row, now)
    out = [Item(host, "srp.state", fmt_value(state))]
    if state != "unknown":
        risk = row.get("risk")
        if risk is not None:
            out.append(Item(host, "srp.risk", fmt_value(float(risk))))
        days_left = row.get("days_left")
        if days_left is not None:
            out.append(Item(host, "srp.days_left", fmt_value(int(days_left))))
    out.append(Item(host, "srp.reason", fmt_value(_reason(row, state))))
    return out


def build_packet(
    rows: Iterable[dict],
    cfg: ZabbixConfig,
    *,
    now: Optional[datetime] = None,
    backoff_hosts: Optional[set] = None,
) -> Packet:
    """Собрать значения по всему парку с отбором, дедупликацией имён и пропусками."""
    moment = now or _now()
    skipped_backoff = backoff_hosts or set()
    selected = [
        r for r in rows if not cfg.org_codes or str(r.get("org_code") or "") in cfg.org_codes
    ]

    skipped: list = []
    seen: dict = {}
    for row in selected:
        host = resolve_host(row, cfg)
        device_id = str(row.get("device_id") or "")
        if not host:
            skipped.append(Skipped(device_id, "", SKIP_NO_NAME))
            continue
        seen.setdefault(host, []).append((host, row, device_id))

    named: list = []
    for host, group in seen.items():
        if len(group) > 1:
            # Две машины на одном узле Zabbix склеились бы молча, и график прыгал
            # бы между разными компьютерами. Молчаливое искажение хуже пропуска.
            skipped.extend(Skipped(d, host, SKIP_DUPLICATE_NAME) for _h, _r, d in group)
            continue
        _h, row, device_id = group[0]
        if host in skipped_backoff:
            skipped.append(Skipped(device_id, host, SKIP_BACKOFF))
            continue
        named.append((host, row, device_id))

    items: list = []
    hosts: list = []
    unknown = 0
    for host, row, device_id in named:
        built = build_items(row, cfg, host, moment)
        items.extend(built)
        hosts.append((host, device_id))
        if any(i.key == "srp.state" and i.value == "unknown" for i in built):
            unknown += 1
    return Packet(items=items, hosts=hosts, skipped=skipped, unknown=unknown)


def heartbeat_items(cfg: ZabbixConfig, *, when: Optional[datetime] = None) -> list:
    """Единственный служебный ключ: время последнего расчёта SRP (unix-время).

    Нужен ровно для одного: чтобы Zabbix штатным ``nodata`` увидел, что SRP
    перестал считать или перестал слать. Счётчиков экспорта наружу не уходит —
    они видны в самом SRP, на странице /pipeline.
    """
    if not cfg.export_host:
        return []
    moment = when or _now()
    return [Item(cfg.export_host, "srp.last_run", fmt_value(int(moment.timestamp())))]


def declared_keys() -> set:
    """Полный набор ключей, которые SRP вообще способен отправить."""
    return set(CORE_KEYS) | set(EXPORT_KEYS)
