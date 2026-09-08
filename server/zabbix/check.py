"""Проверка настройки экспорта в Zabbix из командной строки.

    python -m server.zabbix.check                      # чек-лист по конфигурации
    python -m server.zabbix.check --server 10.0.0.5    # ещё до правки конфига
    python -m server.zabbix.check --list-hosts         # CSV имён узлов под импорт
    python -m server.zabbix.check --check-hosts        # какие узлы Zabbix не знает
    python -m server.zabbix.check --device BUH-01      # что уедет по одной машине
    python -m server.zabbix.check --dry-run            # весь пакет, ничего не отправляя

Код возврата 0 — всё в порядке, 1 — есть на что посмотреть. Утилита ничего
не меняет в SRP и ничего не пишет в базу.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from typing import Optional

from server import db
from server.api import _csv_safe
from server.config import load_config
from server.zabbix import items as items_mod
from server.zabbix import sender
from server.zabbix.config import ZabbixConfig, load_zabbix_config
from server.zabbix.protocol import Item

_OK = "ок"
_FAIL = "ПРОБЛЕМА"


def _resolve_config(args) -> ZabbixConfig:
    cfg = load_config().zabbix_config()
    if args.server or args.port:
        raw = {
            "server": args.server or cfg.server,
            "port": args.port or cfg.port,
            "host_field": cfg.host_field,
            "host_suffix": cfg.host_suffix,
            "export_host": cfg.export_host,
            "org_codes": list(cfg.org_codes),
            "host_map": dict(cfg.host_map),
        }
        cfg = load_zabbix_config(raw)
    return cfg


def _line(label: str, value: str) -> None:
    print(f"{label:.<24} {value}")


def _packet(cfg: ZabbixConfig):
    return items_mod.build_packet(db.get_fleet_verdicts(), cfg)


def _visible_name(row) -> str:
    """Человекочитаемое имя для колонки «Видимое имя» при импорте хостов."""
    if not row:
        return ""
    return db.display_name(
        row.get("hostname"),
        model=row.get("model"),
        chassis=row.get("chassis"),
        device_id=row.get("device_id"),
        disambiguate=True,
    )


def cmd_list_hosts(cfg: ZabbixConfig) -> int:
    """CSV с именами узлов ровно в том виде, в каком их пришлёт SRP."""
    packet = _packet(cfg)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    # visible_name — вторая колонка формы импорта хостов в Zabbix («Видимое имя»).
    writer.writerow(["host", "visible_name", "device_id", "skip_reason"])
    by_id = {str(r.get("device_id") or ""): r for r in db.get_fleet_verdicts()}
    # _csv_safe: hostname приходит из телеметрии, а этот файл открывают в таблице
    # для массового импорта хостов — ячейка, начинающаяся с =, там станет формулой.
    for host, device_id in packet.hosts:
        writer.writerow(
            _csv_safe(v) for v in (host, _visible_name(by_id.get(device_id)), device_id, "")
        )
    for skip in packet.skipped:
        writer.writerow(
            _csv_safe(v)
            for v in (
                skip.host,
                _visible_name(by_id.get(skip.device_id)),
                skip.device_id,
                skip.label,
            )
        )
    sys.stdout.write(out.getvalue())
    return 0


def cmd_device(cfg: ZabbixConfig, name: str) -> int:
    """Полный JSON того, что уедет по одной машине — заодно ответ «что значат ключи»."""
    packet = _packet(cfg)
    wanted = [h for h in packet.hosts if name in (h[0], h[1])]
    if not wanted:
        print(f"{_FAIL}: машина «{name}» не найдена среди {len(packet.hosts)} отправляемых")
        return 1
    host = wanted[0][0]
    values = [
        {"host": i.host, "key": i.key, "value": i.value} for i in packet.items if i.host == host
    ]
    print(json.dumps(values, ensure_ascii=False, indent=2))
    return 0


def cmd_dry_run(cfg: ZabbixConfig) -> int:
    """Что уйдёт, если нажать «Отправить сейчас». Ни одного сетевого вызова."""
    packet = _packet(cfg)
    _line("машин к отправке", str(len(packet.hosts)))
    _line("значений", str(len(packet.items)))
    _line("пропущено машин", str(len(packet.skipped)))
    _line("в состоянии unknown", str(packet.unknown))
    for skip in packet.skipped[:20]:
        print(f"  пропуск: {skip.device_id} — {skip.label}")
    if packet.hosts:
        print("\nпример узла:", packet.hosts[0][0])
    return 0


def cmd_check_hosts(cfg: ZabbixConfig) -> int:
    """Какие узлы Zabbix не знает. Шлёт по одному значению srp.state на узел."""
    if not cfg.enabled:
        print(f"{_FAIL}: адрес Zabbix не задан")
        return 1
    packet = _packet(cfg)
    unknown = []
    for host, _device_id in packet.hosts:
        result = sender.send(
            [Item(host, "srp.state", "unknown")],
            host=cfg.server,
            port=cfg.port,
            timeout_sec=cfg.timeout_sec,
        )
        if result.error:
            print(f"{_FAIL}: {result.error}")
            return 1
        if result.failed:
            unknown.append(host)
    for host in unknown:
        print(f"Zabbix не знает узел: {host}")
    print(f"\nизвестно {len(packet.hosts) - len(unknown)} из {len(packet.hosts)} узлов")
    return 1 if unknown else 0


def _probe(cfg: ZabbixConfig, host: str, label: str) -> bool:
    result = sender.send(
        [Item(host, "srp.state", "unknown")],
        host=cfg.server,
        port=cfg.port,
        timeout_sec=cfg.timeout_sec,
    )
    if result.error:
        _line(label, f"{_FAIL}: {result.error}")
        return False
    if result.failed:
        _line(label, f"{_FAIL}: узел «{host}» Zabbix не принял")
        return False
    _line(label, f"{_OK}: узел «{host}» принял значение")
    return True


def cmd_checklist(cfg: ZabbixConfig) -> int:
    """Пункт за пунктом: адрес, связь, служебный узел, реальная машина, шифрование."""
    if not cfg.enabled:
        _line("адрес", f"{_FAIL}: не задан (server/config.json -> zabbix.server)")
        return 1
    _line("адрес", f"{cfg.server}:{cfg.port}")
    _line("время SRP (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print("  (траппер не сообщает своё время — сверьте с часами сервера Zabbix вручную)")

    ok = True
    if cfg.export_host:
        ok &= _probe(cfg, cfg.export_host, "хост службы")
    else:
        _line("хост службы", "не настроен (export_host пуст)")

    packet = _packet(cfg)
    _line("машин к отправке", str(len(packet.hosts)))
    _line("пропущено машин", str(len(packet.skipped)))
    if packet.hosts:
        ok &= _probe(cfg, packet.hosts[0][0], "пробная машина")
    else:
        _line("пробная машина", "нет ни одной машины с вердиктом")

    warning = cfg.encryption_warning()
    _line("шифрование", "НЕТ; " + warning if warning else "НЕТ (адрес локальный — допустимо)")
    print("\nИТОГ:", "экспорт настроен верно" if ok else "есть что исправить, см. выше")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server.zabbix.check",
        description="Проверка настройки экспорта вердикта SRP в Zabbix",
    )
    p.add_argument("--server", help="адрес Zabbix вместо указанного в конфигурации")
    p.add_argument("--port", type=int, help="порт траппера вместо указанного в конфигурации")
    p.add_argument("--list-hosts", action="store_true", help="CSV имён узлов под импорт в Zabbix")
    p.add_argument("--check-hosts", action="store_true", help="какие узлы Zabbix не знает")
    p.add_argument("--dry-run", action="store_true", help="что уйдёт, ничего не отправляя")
    p.add_argument("--device", help="полный JSON по одной машине (имя узла или device_id)")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_db(load_config().resolved_db_path())
    cfg = _resolve_config(args)
    if args.list_hosts:
        return cmd_list_hosts(cfg)
    if args.device:
        return cmd_device(cfg, args.device)
    if args.dry_run:
        return cmd_dry_run(cfg)
    if args.check_hosts:
        return cmd_check_hosts(cfg)
    return cmd_checklist(cfg)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
