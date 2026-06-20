"""Phase 4: NetdiscoConfig — OFF by default, intervals clamped (mirror PrinterConfig).

Discovery must never run unless explicitly enabled, and no interval may drop
below the floor however the config is set (never hammer the network/server).
"""

from __future__ import annotations

import dataclasses

import pytest
from server.netdisco.config import load_netdisco_config


def test_defaults_are_off_and_safe() -> None:
    cfg = load_netdisco_config(None)
    assert cfg.enabled is False
    assert cfg.inventory_interval_sec == 900
    assert cfg.jitter_sec == 30


def test_enabled_requires_explicit_true() -> None:
    assert load_netdisco_config({"enabled": "yes"}).enabled is False  # only True enables
    assert load_netdisco_config({"enabled": 1}).enabled is False
    assert load_netdisco_config({"enabled": True}).enabled is True


def test_interval_is_clamped_to_the_floor() -> None:
    assert load_netdisco_config({"inventory_interval_sec": 5}).inventory_interval_sec == 60
    assert load_netdisco_config({"inventory_interval_sec": 1200}).inventory_interval_sec == 1200


def test_jitter_is_non_negative_and_unknown_keys_ignored() -> None:
    cfg = load_netdisco_config({"jitter_sec": -5, "totally_unknown_key": 99})
    assert cfg.jitter_sec == 0


def test_bad_types_fall_back_to_defaults() -> None:
    assert load_netdisco_config({"inventory_interval_sec": "abc"}).inventory_interval_sec == 900


def test_config_is_frozen() -> None:
    cfg = load_netdisco_config(None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.enabled = True


def test_server_config_exposes_netdisco_config() -> None:
    from server.config import ServerConfig

    cfg = ServerConfig(netdisco={"enabled": True, "inventory_interval_sec": 5})
    nd = cfg.netdisco_config()
    assert nd.enabled is True
    assert nd.inventory_interval_sec == 60  # clamped through load_netdisco_config
    assert ServerConfig().netdisco_enabled is False  # OFF by default
