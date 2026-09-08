"""Поставляемый шаблон Zabbix должен описывать ровно те ключи, которые SRP шлёт.

Расхождение здесь — это не косметика: лишний ключ в шаблоне создаёт элемент,
в который никогда ничего не придёт (и триггер nodata по нему будет врать),
а недостающий ключ означает, что часть значений Zabbix будет вечно отвергать.

PyYAML в зависимостях проекта нет (сервер и агент обходятся без него), поэтому
ключи выбираются регулярным выражением, а не разбором YAML.
"""

from __future__ import annotations

import re
from pathlib import Path

from server.zabbix.items import declared_keys

TEMPLATE = Path(__file__).resolve().parents[1] / "docs" / "zabbix" / "srp_template.yaml"


def _template_keys() -> set:
    text = TEMPLATE.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*-?\s*key:\s*(srp\.[\w.]+)\s*$", text, flags=re.MULTILINE))


def test_template_file_ships_with_the_repository():
    assert TEMPLATE.exists(), "шаблон для импорта в Zabbix должен лежать в docs/zabbix/"


def test_template_describes_exactly_the_keys_srp_sends():
    assert _template_keys() == declared_keys()


def test_every_item_in_the_template_is_a_trapper():
    text = TEMPLATE.read_text(encoding="utf-8")
    types = set(re.findall(r"^\s*-?\s*type:\s*([A-Z_]+)\s*$", text, flags=re.MULTILINE))

    assert types <= {"TRAPPER"}, f"элементы должны быть типа TRAPPER, найдено: {types}"


def test_state_value_map_covers_every_state_srp_can_send():
    text = TEMPLATE.read_text(encoding="utf-8")
    mapped = set(re.findall(r"^\s*-?\s*value:\s*(h[0-4]|unknown)\s*$", text, flags=re.MULTILINE))

    assert {"h0", "h1", "h2", "h3", "h4", "unknown"} <= mapped


def test_nodata_is_only_used_on_text_and_service_keys():
    """На числовых элементах разрывы штатны — nodata на них поднимал бы ложную тревогу."""
    text = TEMPLATE.read_text(encoding="utf-8")
    numeric = ("srp.risk", "srp.days_left")

    for expression in re.findall(r"nodata\(([^)]*)\)", text):
        assert not any(key in expression for key in numeric), expression


# --------------------------------------------------------------------------- #
# Импортируемость в Zabbix (находка второго security-ревью)
# --------------------------------------------------------------------------- #
#: Zabbix 6.0 валидирует ТЕХНИЧЕСКОЕ имя шаблона как ZBX_PREG_INTERNAL_NAMES
#: (ui/include/defines.inc.php): только [0-9a-zA-Z_. -]. Двоеточие и кириллица
#: там незаконны, и импорт шаблона просто отваливается с «invalid host name».
_ZBX_TECHNICAL_NAME = re.compile(r"^[0-9a-zA-Z_. \-]+$")


def _technical_names() -> list:
    text = TEMPLATE.read_text(encoding="utf-8")
    return re.findall(r"^\s*-?\s*template:\s*(.+?)\s*$", text, flags=re.MULTILINE)


def test_technical_template_names_are_importable_into_zabbix():
    names = _technical_names()

    assert names, "в шаблоне должно быть хотя бы одно техническое имя"
    for name in names:
        assert _ZBX_TECHNICAL_NAME.match(name), f"Zabbix не примет имя шаблона: {name!r}"


def test_trigger_expressions_reference_existing_templates_and_keys():
    text = TEMPLATE.read_text(encoding="utf-8")
    # выражения бывают многострочные (YAML сворачивает их с переносом)
    flat = re.sub(r"\s*\n\s*", " ", text)
    refs = set(re.findall(r"/([^/]+)/(srp\.[\w.]+)", flat))

    assert refs, "в шаблоне должны быть триггеры"
    known_templates = set(_technical_names())
    for template_name, key in refs:
        assert template_name in known_templates, f"ссылка на неизвестный шаблон: {template_name}"
        assert key in declared_keys(), f"ссылка на неизвестный ключ: {key}"


def test_a_machine_that_stops_being_exported_raises_a_trigger():
    """Пропущенная машина не должна быть в Zabbix неотличима от тишины."""
    flat = re.sub(r"\s*\n\s*", " ", TEMPLATE.read_text(encoding="utf-8"))

    assert re.search(r"nodata\(/[^/]+/srp\.state,", flat), "нужен nodata на srp.state"
