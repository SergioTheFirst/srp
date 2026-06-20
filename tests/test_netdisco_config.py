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


# --- Phase 5: active-scan config fields (mirror PrinterConfig + scan ports/workers) ---


def test_scan_defaults_are_safe_and_off() -> None:
    cfg = load_netdisco_config(None)
    assert cfg.active_scan is False  # the stop-gate default
    assert cfg.scan_cidrs == ()
    assert cfg.static_ips == ()
    assert cfg.scan_max_hosts == 4096
    assert cfg.scan_workers == 64
    assert cfg.snmp_community == "public"
    assert cfg.snmp_version == 1
    assert cfg.discovery_interval_sec == 900
    assert cfg.scan_ports  # a non-empty default liveness-port set


def test_active_scan_requires_explicit_true() -> None:
    assert load_netdisco_config({"active_scan": "yes"}).active_scan is False
    assert load_netdisco_config({"active_scan": 1}).active_scan is False
    assert load_netdisco_config({"active_scan": True}).active_scan is True


def test_scan_cidrs_keep_only_rfc1918() -> None:
    cfg = load_netdisco_config(
        {"scan_cidrs": ["10.0.0.0/24", "8.8.8.0/24", "192.168.1.0/24", "not-a-cidr"]}
    )
    assert cfg.scan_cidrs == ("10.0.0.0/24", "192.168.1.0/24")  # public/garbage dropped


def test_static_ips_keep_only_rfc1918() -> None:
    cfg = load_netdisco_config({"static_ips": ["10.0.0.5", "1.1.1.1", "192.168.0.9"]})
    assert cfg.static_ips == ("10.0.0.5", "192.168.0.9")


def test_scan_max_hosts_is_non_negative() -> None:
    assert load_netdisco_config({"scan_max_hosts": -1}).scan_max_hosts == 0  # 0 = kill-switch
    assert load_netdisco_config({"scan_max_hosts": 10}).scan_max_hosts == 10
    assert load_netdisco_config({"scan_max_hosts": "abc"}).scan_max_hosts == 4096


def test_scan_workers_clamped_to_bounds() -> None:
    assert load_netdisco_config({"scan_workers": 0}).scan_workers == 1  # at least one worker
    assert load_netdisco_config({"scan_workers": 9999}).scan_workers == 256  # hard ceiling
    assert load_netdisco_config({"scan_workers": 32}).scan_workers == 32


def test_scan_ports_validated_deduped_order_preserved() -> None:
    cfg = load_netdisco_config({"scan_ports": [80, 80, 70000, 0, -5, 443, "x"]})
    assert cfg.scan_ports == (80, 443)  # only in-range ints, deduped, order kept


def test_scan_ports_empty_or_all_invalid_falls_back_to_default() -> None:
    default_ports = load_netdisco_config(None).scan_ports
    assert load_netdisco_config({"scan_ports": []}).scan_ports == default_ports
    assert load_netdisco_config({"scan_ports": [0, 99999]}).scan_ports == default_ports


def test_snmp_version_only_0_or_1() -> None:
    assert load_netdisco_config({"snmp_version": 3}).snmp_version == 1
    assert load_netdisco_config({"snmp_version": 0}).snmp_version == 0


def test_discovery_interval_clamped_to_floor() -> None:
    assert load_netdisco_config({"discovery_interval_sec": 5}).discovery_interval_sec == 60
    assert load_netdisco_config({"discovery_interval_sec": 1800}).discovery_interval_sec == 1800
