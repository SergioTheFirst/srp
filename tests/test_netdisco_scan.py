"""Phase 5: generalized active LAN scan (behind the active_scan stop-gate).

Owner-authorized RFC1918 asset discovery (memory printer-active-scan-authorized,
generalized to netdisco). Every test injects host checks or monkeypatches the
socket/SNMP layer -- the real network is NEVER touched. The safety rails proven
here: OFF unless active_scan, RFC1918-only, host/kill-switch caps, and one bad
host never aborting the whole scan.
"""

from __future__ import annotations

import pytest
from server.netdisco import scan as scan_mod
from server.netdisco.config import NetdiscoConfig


def _cfg(**over: object) -> NetdiscoConfig:
    base: dict[str, object] = {
        "active_scan": True,
        "scan_cidrs": ("10.0.0.0/30",),  # hosts 10.0.0.1, 10.0.0.2
        "scan_max_hosts": 4096,
        "scan_workers": 8,
        "scan_ports": (80,),
    }
    base.update(over)
    return NetdiscoConfig(**base)  # type: ignore[arg-type]


def test_scan_is_empty_when_active_scan_off() -> None:
    cfg = _cfg(active_scan=False)

    def boom(ip: str) -> bool:  # must never run when the gate is shut
        raise AssertionError("host_check called while active_scan is off")

    assert scan_mod.scan(cfg, host_check=boom) == []


def test_scan_is_empty_when_max_hosts_kill_switch() -> None:
    cfg = _cfg(scan_max_hosts=0)

    def boom(ip: str) -> bool:
        raise AssertionError("host_check called with the kill-switch engaged")

    assert scan_mod.scan(cfg, host_check=boom) == []


def test_scan_returns_alive_hosts_via_injected_check() -> None:
    cfg = _cfg()
    found = scan_mod.scan(cfg, host_check=lambda ip: ip.endswith(".1"))
    assert found == ["10.0.0.1"]


def test_scan_drops_public_cidr_defense_in_depth() -> None:
    # A public CIDR can reach scan() only if config validation is bypassed;
    # expand_cidrs re-checks RFC1918 so no public host is ever enumerated.
    cfg = _cfg(scan_cidrs=("8.8.8.0/30",))
    assert scan_mod.scan(cfg, host_check=lambda ip: True) == []


def test_scan_one_bad_host_does_not_abort_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_alive(ip: str, **_: object) -> bool:
        if ip.endswith(".2"):
            raise RuntimeError("simulated probe failure")
        return True

    monkeypatch.setattr(scan_mod, "host_is_alive", fake_alive)
    # default_check (not an injected host_check) must swallow the bad host.
    assert scan_mod.scan(_cfg()) == ["10.0.0.1"]


def test_host_is_alive_rejects_public_ip_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scan_mod, "_tcp_open", lambda *a, **k: pytest.fail("public IP TCP-probed"))
    monkeypatch.setattr(
        scan_mod.snmp, "snmp_get", lambda *a, **k: pytest.fail("public IP SNMP-probed")
    )
    assert scan_mod.host_is_alive("8.8.8.8", ports=(80,)) is False


def test_host_is_alive_true_on_open_tcp_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan_mod, "_tcp_open", lambda ip, port, timeout: port == 80)
    monkeypatch.setattr(
        scan_mod.snmp, "snmp_get", lambda *a, **k: pytest.fail("SNMP tried though TCP open")
    )
    assert scan_mod.host_is_alive("10.0.0.5", ports=(80, 443)) is True


def test_host_is_alive_falls_back_to_snmp_when_ports_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scan_mod, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(scan_mod.snmp, "snmp_get", lambda *a, **k: {"1.3.6.1": "x"})
    assert scan_mod.host_is_alive("10.0.0.6", ports=(80,)) is True
    monkeypatch.setattr(scan_mod.snmp, "snmp_get", lambda *a, **k: {})
    assert scan_mod.host_is_alive("10.0.0.6", ports=(80,)) is False
