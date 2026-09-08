"""Конфигурация экспорта в Zabbix (блок ``zabbix`` в ``server/config.json``).

Форма повторяет ``server/printers/config.py`` и ``server/netdisco/config.py``:
frozen dataclass + загрузчик, который КЛАМПИТ и ЧИНИТ мусор, а не падает —
опечатка в конфиге не должна ронять сервер, к которому стучится весь парк.

Обязательная настройка ровно одна — ``server``. Пустой адрес означает «экспорт
выключен»: отдельного ``enabled`` нет намеренно, два переключателя (один из
которых говорит «включено», когда слать некуда) вводят в заблуждение при разборе
аварии.

Адрес назначения НЕ фильтруется через ``is_rfc1918``: этот предикат применяется
к адресам В ДАННЫХ, а не к адресату. Zabbix законно стоит на публичном IP или
за DNS-именем; фильтр сломал бы саму фичу. Вместо фильтра — предупреждение
об отсутствии шифрования (``encryption_warning``).
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

log = logging.getLogger("srp.zabbix")

_MIN_INTERVAL_SEC = 60
_DEFAULT_INTERVAL_SEC = 300
_DEFAULT_PORT = 10051
_DEFAULT_TIMEOUT_SEC = 5.0
_MIN_TIMEOUT_SEC = 1.0
_MAX_TIMEOUT_SEC = 60.0
_DEFAULT_EXPORT_HOST = "SRP"

HOST_FIELDS = ("hostname", "device_id", "display_name")


@dataclass(frozen=True)
class ZabbixConfig:
    #: Адрес сервера Zabbix (IP или имя). Пусто = экспорт выключен.
    server: str = ""
    port: int = _DEFAULT_PORT
    interval_sec: int = _DEFAULT_INTERVAL_SEC
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC
    #: Чем называть узел Zabbix: hostname | device_id | display_name.
    host_field: str = "hostname"
    #: Суффикс имени узла — для парков, где хосты в Zabbix заведены как FQDN.
    host_suffix: str = ""
    #: Имя узла для служебных счётчиков самого экспорта. Пусто = не слать их.
    export_host: str = _DEFAULT_EXPORT_HOST
    #: Отбор для пилота: пусто = весь парк, иначе только эти организации.
    org_codes: tuple = ()
    #: Исключения соответствия имён: device_id -> имя узла в Zabbix.
    host_map: Mapping[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.server)

    def encryption_warning(self) -> Optional[str]:
        """Причина предупредить об отсутствии шифрования, либо ``None``.

        Траппер не шифруется (PSK/TLS не реализованы), поэтому вердикты по всему
        парку уходят открытым текстом. Для литерального адреса из RFC1918 или
        loopback это нормально и молчим; для публичного IP — предупреждаем;
        для DNS-имени сказать наверняка нельзя, поэтому просим проверить.
        """
        if not self.server:
            return None
        try:
            addr = ipaddress.ip_address(self.server)
        except ValueError:
            return (
                f"адрес {self.server} задан именем — убедитесь, что Zabbix "
                "в локальной сети: траппер не шифруется (PSK/TLS не реализованы)"
            )
        # is_global, а не is_private: последнее считает «частными» и документационные
        # диапазоны, а нас интересует ровно одно — уйдёт ли трафик в интернет.
        if not addr.is_global:
            return None
        return (
            f"адрес {self.server} не в локальной сети, а траппер не шифруется "
            "(PSK/TLS не реализованы) — вердикты по всему парку уйдут открытым текстом"
        )


def _as_int(value: Any, default: int, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        if value is not None:
            log.warning("zabbix.%s: непонятное значение %r — беру %r", name, value, default)
        return default


def _as_float(value: Any, default: float, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        if value is not None:
            log.warning("zabbix.%s: непонятное значение %r — беру %r", name, value, default)
        return default


def _as_str(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _choice(value: Any, allowed: tuple, default: str, name: str) -> str:
    text = _as_str(value)
    if text in allowed:
        return text
    if text:
        log.warning("zabbix.%s: %r не из %s — беру %r", name, text, "/".join(allowed), default)
    return default


def _host_map(value: Any) -> dict:
    if not isinstance(value, Mapping):
        if value:
            log.warning("zabbix.host_map: ожидался объект device_id -> имя узла — игнорирую")
        return {}
    out = {}
    for device_id, name in value.items():
        clean = _as_str(name)
        if isinstance(device_id, str) and device_id and clean:
            out[device_id] = clean
    return out


def load_zabbix_config(data: Optional[Mapping[str, Any]]) -> ZabbixConfig:
    """Собрать ZabbixConfig из сырого блока конфигурации, починив небезопасное."""
    d = data or {}
    port = _as_int(d.get("port"), _DEFAULT_PORT, "port")
    if not 1 <= port <= 65535:
        log.warning("zabbix.port: %d вне 1..65535 — беру %d", port, _DEFAULT_PORT)
        port = _DEFAULT_PORT
    interval = max(
        _MIN_INTERVAL_SEC, _as_int(d.get("interval_sec"), _DEFAULT_INTERVAL_SEC, "interval_sec")
    )
    timeout = _as_float(d.get("timeout_sec"), _DEFAULT_TIMEOUT_SEC, "timeout_sec")
    timeout = min(_MAX_TIMEOUT_SEC, max(_MIN_TIMEOUT_SEC, timeout))
    org_codes = tuple(
        str(c).strip() for c in d.get("org_codes") or () if isinstance(c, (str, int)) and str(c)
    )
    return ZabbixConfig(
        server=_as_str(d.get("server")),
        port=port,
        interval_sec=interval,
        timeout_sec=timeout,
        host_field=_choice(d.get("host_field"), HOST_FIELDS, "hostname", "host_field"),
        host_suffix=_as_str(d.get("host_suffix")),
        export_host=_as_str(d.get("export_host"), _DEFAULT_EXPORT_HOST),
        org_codes=org_codes,
        host_map=_host_map(d.get("host_map")),
    )
