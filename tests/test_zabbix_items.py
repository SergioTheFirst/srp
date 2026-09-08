"""Вердикт -> значения элементов Zabbix: четыре ключа и «UNKNOWN, а не ноль».

Директива владельца: в Zabbix уходит ВЫВОД, а не данные. Ровно четыре ключа
на машину (`srp.state`, `srp.risk`, `srp.days_left`, `srp.reason`) и один
на сам SRP (`srp.last_run`). Всё, что помогает понять «почему», остаётся в SRP.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.zabbix import items as it
from server.zabbix.config import ZabbixConfig

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
CFG = ZabbixConfig(server="10.0.0.5")


def row(**over) -> dict:
    base = {
        "device_id": "dev-abc123",
        "hostname": "BUH-01",
        "model": "Latitude 5420",
        "chassis": "laptop",
        "org_code": "7",
        "score_ts": (NOW - timedelta(hours=2)).isoformat(),
        "state": "h3",
        "risk": 62.5,
        "days_left": 30,
        "dominant_label": "Накопитель",
        "action": "заменить диск в ближайшие 30 суток",
    }
    base.update(over)
    return base


def keys_of(items) -> set:
    return {i.key for i in items}


def value_of(items, key):
    for i in items:
        if i.key == key:
            return i.value
    return None


# --------------------------------------------------------------------------- #
# Набор ключей — ровно четыре
# --------------------------------------------------------------------------- #
def test_full_verdict_produces_exactly_four_keys():
    items = it.build_items(row(), CFG, "BUH-01", NOW)

    assert keys_of(items) == set(it.CORE_KEYS)
    assert len(it.CORE_KEYS) == 4


def test_the_declared_key_set_is_five_keys_in_total():
    assert it.declared_keys() == {
        "srp.state",
        "srp.risk",
        "srp.days_left",
        "srp.reason",
        "srp.last_run",
    }


def test_no_detail_keys_leak_back_in():
    """Оси, координаты, уверенность, возраст и ссылки наружу не уходят."""
    items = it.build_items(row(), CFG, "BUH-01", NOW)
    forbidden = {
        "srp.health",
        "srp.observability",
        "srp.confidence",
        "srp.dominant",
        "srp.age_hours",
        "srp.url",
        "srp.severity",
        "srp.damage",
        "srp.horizon",
    }

    assert keys_of(items).isdisjoint(forbidden)


# --------------------------------------------------------------------------- #
# Значения
# --------------------------------------------------------------------------- #
def test_state_is_the_verdict_itself():
    for state in ("h0", "h1", "h2", "h3", "h4"):
        items = it.build_items(row(state=state), CFG, "H", NOW)
        assert value_of(items, "srp.state") == state


def test_risk_is_a_plain_number_without_scientific_notation():
    assert value_of(it.build_items(row(risk=62.5), CFG, "H", NOW), "srp.risk") == "62.5"
    assert value_of(it.build_items(row(risk=41.0), CFG, "H", NOW), "srp.risk") == "41"


def test_days_left_is_the_window_until_degradation():
    assert value_of(it.build_items(row(days_left=7), CFG, "H", NOW), "srp.days_left") == "7"


def test_reason_is_a_short_russian_phrase_about_what_to_do():
    items = it.build_items(row(), CFG, "H", NOW)

    assert value_of(items, "srp.reason") == "Накопитель: заменить диск в ближайшие 30 суток"


def test_reason_carries_no_identifier_of_any_kind():
    """Главный фактор — фраза, а не запись данных: ни имени, ни модели, ни id."""
    items = it.build_items(row(), CFG, "BUH-01", NOW)
    reason = value_of(items, "srp.reason")

    for identifier in ("BUH-01", "dev-abc123", "Latitude 5420", "laptop"):
        assert identifier not in reason


def test_reason_length_is_capped():
    long_action = "ц" * 900
    reason = value_of(it.build_items(row(action=long_action), CFG, "H", NOW), "srp.reason")

    assert len(reason) <= 255


# --------------------------------------------------------------------------- #
# UNKNOWN, а не ноль
# --------------------------------------------------------------------------- #
def test_unknown_state_is_still_sent_so_the_machine_does_not_vanish():
    items = it.build_items(row(state="unknown"), CFG, "H", NOW)

    assert value_of(items, "srp.state") == "unknown"


def test_unknown_state_sends_no_numbers_at_all():
    """Ноль в srp.risk объявил бы слепую машину идеально здоровой."""
    items = it.build_items(row(state="unknown"), CFG, "H", NOW)

    assert "srp.risk" not in keys_of(items)
    assert "srp.days_left" not in keys_of(items)


def test_unknown_state_still_explains_itself():
    items = it.build_items(row(state="unknown"), CFG, "H", NOW)

    assert "нет видимости" in value_of(items, "srp.reason")


def test_missing_days_left_means_no_key_not_a_zero():
    items = it.build_items(row(days_left=None), CFG, "H", NOW)

    assert "srp.days_left" not in keys_of(items)


def test_missing_risk_means_no_key_not_a_zero():
    items = it.build_items(row(risk=None), CFG, "H", NOW)

    assert "srp.risk" not in keys_of(items)


def test_verdict_older_than_ten_days_becomes_unknown():
    stale = row(score_ts=(NOW - timedelta(days=12)).isoformat(), state="h0")
    items = it.build_items(stale, CFG, "H", NOW)

    assert value_of(items, "srp.state") == "unknown"
    assert "srp.risk" not in keys_of(items)


def test_unknown_state_value_is_passed_through_not_swallowed():
    """fail-open: незнакомое состояние не должно молча прятать машину."""
    items = it.build_items(row(state="h9"), CFG, "H", NOW)

    assert value_of(items, "srp.state") == "h9"


# --------------------------------------------------------------------------- #
# Единственный служебный ключ
# --------------------------------------------------------------------------- #
def test_heartbeat_carries_only_the_time_of_the_last_run():
    items = it.heartbeat_items(CFG, when=NOW)

    assert [i.key for i in items] == ["srp.last_run"]
    assert {i.host for i in items} == {"SRP"}
    assert items[0].value == str(int(NOW.timestamp()))


def test_no_service_host_means_no_heartbeat():
    assert it.heartbeat_items(ZabbixConfig(server="10.0.0.5", export_host=""), when=NOW) == []
