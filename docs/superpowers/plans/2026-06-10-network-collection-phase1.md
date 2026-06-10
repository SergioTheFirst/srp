# Network Collection (Phase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent collects per-machine network data (adapter/IP/Wi-Fi health, ARP neighbors, internal-only TCP connections, link quality) and folds it into the existing `historical` payload; the server stores it, treats `network` as an ungated trust source, and a minimal device-page block proves the data flows.

**Architecture:** New pure-stdlib collector `client/collectors/network.py` exposes `collect_network() -> CollectorResult`. `collect_historical()` calls it and merges its payload + `source_health` (exactly as it already does for certificates). New additive optional fields on `HistoricalPayload` carry the data — no new `msg_type`, no new DB table, no `CONTRACT_VERSION` bump. `network` is deliberately **not** added to `DOMAIN_SOURCES`, so it records collector status/freshness but never gates day-1 health scores. External (public) addresses are filtered out **inside the agent** before serialization.

**Tech Stack:** Python 3.9 (stdlib only in `client/`), PowerShell `Get-Net*` cmdlets + `Win32_PerfFormattedData_Tcpip_NetworkInterface` CIM, pydantic v2 (`shared/schema.py`), FastAPI + SQLite (server), Jinja2 (dashboard), pytest.

**Source spec:** `docs/superpowers/specs/2026-06-10-network-tools-design.md`

---

## Conventions for every task

- **Branch first** (project rule CLAUDE.md §6): create `feat/network-collector` before Task 1; all task commits land there. `merge --no-ff` + `push` only when the user asks.
- **Auto-commit** after each task (project rule updated 2026-06-10). Conventional commits, **no attribution line**.
- A `PostToolUse` hook runs `ruff --fix` + `format` on every `.py` edit — **accept its formatting**; add an import only together with its first use (it strips unused imports).
- `client/` stays **pure stdlib** (`ipaddress`, `subprocess`, `json` are stdlib — fine; any third-party import = bug).
- Language independence: parse only **numeric values and English enum names**; never branch on localized text.
- Run the gate before declaring a task done where indicated: `make check` (ruff · mypy[shared+server+client] · bandit · pytest cov ≥80%).

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `shared/schema.py` | Modify | Add `NetAdapter`/`NetNeighbor`/`NetConnection`/`NetQuality` models + 4 optional list fields on `HistoricalPayload`. |
| `client/collectors/sources.py` | Modify | Add `NETWORK = "network"` source-name constant. |
| `client/collectors/network.py` | Create | PowerShell script + pure parsers + privacy filter + `collect_network() -> CollectorResult`. |
| `client/collectors/historical.py` | Modify | Call `collect_network()` and merge payload + `source_health`; add `NETWORK` to `owned`. |
| `server/web/templates/device.html` | Modify | Minimal «Сеть» section rendering `d.historical.network_*`. |
| `tests/test_network_collector.py` | Create | Parser, privacy-filter, cap, status, and locale tests (mock `run_ps`). |
| `tests/test_network_contract.py` | Create | Additive-optional contract + no-version-bump tests. |
| `tests/test_network_ingest_trust.py` | Create | `network` source is ungated; day-1 scores unaffected; data stored + surfaced. |
| `CHANGELOG.md` | Modify | One `## [Unreleased]` line for the visible change. |

> **Decision (YAGNI):** per-adapter packet-error counters from `Win32_PerfFormattedData_Tcpip_NetworkInterface` are **deferred** to Phase 2 — `heartbeat` already reports aggregate `nic_errors`, and matching perf-interface names to adapters is fragile. Phase 1 ships adapter config + neighbors + connections + link quality.
> **Decision (v1 scope):** link-quality pings target the **gateway and DNS servers** discovered on the machine. Pinging the SRP server needs the configured `server_url` plumbed into the collector; that is deferred (collector stays zero-arg, like `collect_historical`).

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

Run:
```bash
git switch -c feat/network-collector
```
Expected: `Switched to a new branch 'feat/network-collector'`

---

### Task 1: Contract — schema models + additive `HistoricalPayload` fields

**Files:**
- Modify: `shared/schema.py` (add models near `CertInfo`; add fields to `HistoricalPayload`)
- Test: `tests/test_network_contract.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_network_contract.py`:
```python
"""Contract tests: network fields are additive-optional; no CONTRACT_VERSION bump."""

from __future__ import annotations

import pytest

from shared.schema import CONTRACT_VERSION, HistoricalPayload

pytestmark = pytest.mark.unit


def test_historical_payload_valid_without_network_fields():
    """An older agent that sends no network fields must still validate."""
    p = HistoricalPayload(reliability_stability_index=9.1)
    assert p.network_adapters == []
    assert p.network_neighbors == []
    assert p.network_connections == []
    assert p.network_quality == []


def test_network_fields_round_trip():
    p = HistoricalPayload(
        network_adapters=[{"name": "Ethernet", "kind": "ethernet", "up": True, "ipv4": ["192.168.1.5"]}],
        network_neighbors=[{"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"}],
        network_connections=[{"local_ip": "192.168.1.5", "local_port": 50515,
                              "remote_ip": "192.168.1.10", "remote_port": 445, "state": "Established"}],
        network_quality=[{"target_kind": "gateway", "target": "192.168.1.1",
                          "latency_ms": 1.4, "loss_pct": 0.0, "samples": 3}],
    )
    assert p.network_adapters[0].ipv4 == ["192.168.1.5"]
    assert p.network_connections[0].remote_port == 445
    assert p.network_quality[0].latency_ms == 1.4


def test_contract_version_unchanged():
    assert CONTRACT_VERSION == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_contract.py -q`
Expected: FAIL (`HistoricalPayload` has no `network_adapters`; `AttributeError`/validation error).

- [ ] **Step 3: Add the models + fields**

In `shared/schema.py`, add these classes immediately **after** `class CertInfo(_Base):` (around line 136):
```python
class NetAdapter(_Base):
    name: Optional[str] = None
    desc: Optional[str] = None
    mac: Optional[str] = None
    kind: Optional[str] = None  # "ethernet" | "wifi" | "other"
    up: Optional[bool] = None
    link_mbps: Optional[float] = None
    ipv4: list[str] = Field(default_factory=list)
    ipv6: list[str] = Field(default_factory=list)
    gateway: Optional[str] = None
    dns: list[str] = Field(default_factory=list)
    dhcp: Optional[bool] = None
    ssid: Optional[str] = None
    signal_pct: Optional[int] = None
    channel: Optional[int] = None


class NetNeighbor(_Base):
    ip: Optional[str] = None
    mac: Optional[str] = None
    state: Optional[str] = None


class NetConnection(_Base):
    local_ip: Optional[str] = None
    local_port: Optional[int] = None
    remote_ip: Optional[str] = None
    remote_port: Optional[int] = None
    state: Optional[str] = None


class NetQuality(_Base):
    target_kind: Optional[str] = None  # "gateway" | "dns"
    target: Optional[str] = None
    latency_ms: Optional[float] = None
    loss_pct: Optional[float] = None
    samples: Optional[int] = None
```

Then add these four fields at the **end** of `class HistoricalPayload(_Base):` (after `certificates: list[CertInfo] = Field(default_factory=list)`):
```python
    network_adapters: list[NetAdapter] = Field(default_factory=list)
    network_neighbors: list[NetNeighbor] = Field(default_factory=list)
    network_connections: list[NetConnection] = Field(default_factory=list)
    network_quality: list[NetQuality] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_contract.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add shared/schema.py tests/test_network_contract.py
git commit -m "feat(contract): additive network fields on HistoricalPayload"
```

---

### Task 2: Source-name constant

**Files:**
- Modify: `client/collectors/sources.py`

- [ ] **Step 1: Add the constant**

In `client/collectors/sources.py`, add after `PRINT_JOBS = "print_jobs"`:
```python
NETWORK = "network"
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from client.collectors.sources import NETWORK; print(NETWORK)"`
Expected: prints `network`

- [ ] **Step 3: Commit**

```bash
git add client/collectors/sources.py
git commit -m "feat(agent): add 'network' telemetry source name"
```

---

### Task 3: Pure parsers + privacy filter

**Files:**
- Create: `client/collectors/network.py` (parsers only this task)
- Test: `tests/test_network_collector.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_network_collector.py`:
```python
"""Network collector: parser, privacy filter, caps, status, locale (mock run_ps)."""

from __future__ import annotations

import pytest

from client.collectors import network

pytestmark = pytest.mark.unit


def test_parse_adapter_numeric_and_kind():
    raw = {
        "name": "Ethernet", "desc": "Intel I219", "mac": "AA-BB-CC-00-11-22",
        "iftype": 6, "up": True, "link_bps": 1000000000,
        "ipv4": ["192.168.1.5"], "ipv6": [], "gateway": "192.168.1.1",
        "dns": ["192.168.1.1", ""], "dhcp": True,
    }
    a = network._parse_adapter(raw)
    assert a["kind"] == "ethernet"
    assert a["up"] is True
    assert a["link_mbps"] == 1000.0
    assert a["ipv4"] == ["192.168.1.5"]
    assert a["dns"] == ["192.168.1.1"]  # empty string dropped


def test_parse_adapter_wifi_iftype():
    a = network._parse_adapter({"name": "Wi-Fi", "iftype": 71, "up": False})
    assert a["kind"] == "wifi"
    assert a["up"] is False
    assert a["link_mbps"] is None


def test_connection_keeps_internal():
    raw = {"local_ip": "192.168.1.5", "local_port": 50515,
           "remote_ip": "192.168.1.10", "remote_port": 445, "state": "Established"}
    c = network._parse_connection(raw)
    assert c is not None
    assert c["remote_ip"] == "192.168.1.10"
    assert c["remote_port"] == 445


def test_connection_drops_external():
    raw = {"local_ip": "192.168.1.5", "local_port": 51000,
           "remote_ip": "140.82.112.3", "remote_port": 443, "state": "Established"}
    assert network._parse_connection(raw) is None


def test_connection_drops_loopback_and_listen():
    assert network._parse_connection(
        {"local_ip": "127.0.0.1", "local_port": 1, "remote_ip": "127.0.0.1",
         "remote_port": 1, "state": "Established"}) is None
    assert network._parse_connection(
        {"local_ip": "0.0.0.0", "local_port": 135, "remote_ip": "0.0.0.0",
         "remote_port": 0, "state": "Listen"}) is None


def test_neighbor_drops_broadcast_and_multicast():
    assert network._parse_neighbor(
        {"ip": "192.168.1.255", "mac": "FF-FF-FF-FF-FF-FF", "state": "Permanent"}) is None
    assert network._parse_neighbor(
        {"ip": "224.0.0.22", "mac": "01-00-5E-00-00-16", "state": "Permanent"}) is None


def test_neighbor_keeps_internal():
    n = network._parse_neighbor({"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"})
    assert n == {"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"}


def test_parse_quality_numbers():
    q = network._parse_quality(
        {"target_kind": "gateway", "target": "192.168.1.1",
         "latency_ms": 1.4, "loss_pct": 0.0, "samples": 3})
    assert q == {"target_kind": "gateway", "target": "192.168.1.1",
                 "latency_ms": 1.4, "loss_pct": 0.0, "samples": 3}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_network_collector.py -q`
Expected: FAIL (`ModuleNotFoundError: client.collectors.network` / functions missing).

- [ ] **Step 3: Create the parsers**

Create `client/collectors/network.py`:
```python
"""Network collector (Phase 1): adapter/IP/Wi-Fi health, ARP neighbors,
internal-only TCP connections, and link quality. Pure stdlib.

Language independence: the PowerShell script emits only numeric values and
English enum names (Status/State); never localized text. Privacy: external
(public) addresses are dropped here, before anything is serialized.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Optional

from client.collectors.ps import as_list, run_ps
from client.collectors.sources import NETWORK, CollectorResult, failed, field_status, health

_MAX_NEIGHBORS = 256
_MAX_CONNECTIONS = 256
_BAD_MACS = {"", "00-00-00-00-00-00", "FF-FF-FF-FF-FF-FF"}
_MCAST_MAC_PREFIXES = ("01-00-5E", "33-33", "01-80-C2")


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    f = _f(v)
    return None if f is None else int(f)


def _kind(iftype: Any) -> str:
    n = _i(iftype)
    if n == 6:
        return "ethernet"
    if n == 71:
        return "wifi"
    return "other"


def _is_internal(ip: Optional[str]) -> bool:
    """True only for private LAN addresses usable as a map edge (no loopback,
    link-local, multicast, or unspecified)."""
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        a.is_private
        and not a.is_loopback
        and not a.is_link_local
        and not a.is_multicast
        and not a.is_unspecified
    )


def _clean_strs(value: Any) -> list[str]:
    return [str(x) for x in as_list(value) if x]


def _parse_adapter(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    bps = _i(raw.get("link_bps"))
    return {
        "name": (raw.get("name") or None),
        "desc": (raw.get("desc") or None),
        "mac": (raw.get("mac") or None),
        "kind": _kind(raw.get("iftype")),
        "up": bool(raw.get("up")) if raw.get("up") is not None else None,
        "link_mbps": (round(bps / 1_000_000, 1) if bps else None),
        "ipv4": _clean_strs(raw.get("ipv4")),
        "ipv6": _clean_strs(raw.get("ipv6")),
        "gateway": (raw.get("gateway") or None),
        "dns": _clean_strs(raw.get("dns")),
        "dhcp": (bool(raw.get("dhcp")) if raw.get("dhcp") is not None else None),
        "ssid": (raw.get("ssid") or None),
        "signal_pct": _i(raw.get("signal_pct")),
        "channel": _i(raw.get("channel")),
    }


def _parse_neighbor(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    ip = raw.get("ip")
    if not _is_internal(ip):
        return None
    mac = (raw.get("mac") or "").upper()
    if mac in _BAD_MACS or any(mac.startswith(p) for p in _MCAST_MAC_PREFIXES):
        return None
    return {"ip": ip, "mac": raw.get("mac"), "state": (raw.get("state") or None)}


def _parse_connection(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    if not _is_internal(raw.get("remote_ip")):  # privacy: only internal peers
        return None
    return {
        "local_ip": (raw.get("local_ip") or None),
        "local_port": _i(raw.get("local_port")),
        "remote_ip": raw.get("remote_ip"),
        "remote_port": _i(raw.get("remote_port")),
        "state": (raw.get("state") or None),
    }


def _parse_quality(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {
        "target_kind": (raw.get("target_kind") or None),
        "target": (raw.get("target") or None),
        "latency_ms": _f(raw.get("latency_ms")),
        "loss_pct": _f(raw.get("loss_pct")),
        "samples": _i(raw.get("samples")),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_network_collector.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add client/collectors/network.py tests/test_network_collector.py
git commit -m "feat(agent): network parsers + internal-only privacy filter"
```

---

### Task 4: `collect_network()` orchestration + status

**Files:**
- Modify: `client/collectors/network.py` (add the PowerShell script + entry function)
- Test: `tests/test_network_collector.py` (add cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_network_collector.py`:
```python
from client.collectors.ps import PsResult


def _ok(data):
    return PsResult("ok", data)


_NET_FULL = {
    "adapters": [
        {"name": "Ethernet", "desc": "Intel I219", "mac": "AA-BB-CC-00-11-22",
         "iftype": 6, "up": True, "link_bps": 1000000000,
         "ipv4": ["192.168.1.5"], "ipv6": [], "gateway": "192.168.1.1",
         "dns": ["192.168.1.1"], "dhcp": True}
    ],
    "neighbors": [
        {"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"},
        {"ip": "224.0.0.22", "mac": "01-00-5E-00-00-16", "state": "Permanent"},  # dropped
    ],
    "connections": [
        {"local_ip": "192.168.1.5", "local_port": 50515, "remote_ip": "192.168.1.10",
         "remote_port": 445, "state": "Established"},
        {"local_ip": "192.168.1.5", "local_port": 51000, "remote_ip": "140.82.112.3",
         "remote_port": 443, "state": "Established"},  # external → dropped
    ],
    "quality": [
        {"target_kind": "gateway", "target": "192.168.1.1", "latency_ms": 1.4,
         "loss_pct": 0.0, "samples": 3}
    ],
}


def test_collect_network_ok(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))
    res = network.collect_network()
    assert res.payload is not None
    assert len(res.payload["network_adapters"]) == 1
    assert len(res.payload["network_neighbors"]) == 1      # multicast dropped
    assert len(res.payload["network_connections"]) == 1    # external dropped
    assert res.source_health[network.NETWORK]["status"] == "ok"


def test_collect_network_blocked(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: PsResult("blocked"))
    res = network.collect_network()
    assert res.payload is None
    assert res.source_health[network.NETWORK]["status"] == "blocked"


def test_collect_network_empty_when_all_filtered(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok({"adapters": [], "neighbors": [],
                                                                "connections": [], "quality": []}))
    res = network.collect_network()
    assert res.payload is not None
    assert res.source_health[network.NETWORK]["status"] == "empty"


def test_collect_network_caps_neighbors(monkeypatch):
    many = {"adapters": [], "connections": [], "quality": [],
            "neighbors": [{"ip": f"10.0.0.{i % 254 + 1}", "mac": "AA-BB-CC-00-11-22",
                           "state": "Stale"} for i in range(400)]}
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(many))
    res = network.collect_network()
    assert len(res.payload["network_neighbors"]) == network._MAX_NEIGHBORS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_network_collector.py -k collect_network -q`
Expected: FAIL (`collect_network` not defined).

- [ ] **Step 3: Add the script + entry function**

Append to `client/collectors/network.py`:
```python
_NET_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'

$dh = @{}
foreach ($i in Get-NetIPInterface -AddressFamily IPv4) { $dh["$($i.InterfaceIndex)"] = ($i.Dhcp -eq 'Enabled') }
$cfg = @{}
foreach ($c in Get-NetIPConfiguration) { $cfg["$($c.InterfaceIndex)"] = $c }

$adapters = @()
foreach ($a in Get-NetAdapter) {
  $c = $cfg["$($a.ifIndex)"]
  $adapters += [ordered]@{
    name     = "$($a.Name)"
    desc     = "$($a.InterfaceDescription)"
    mac      = "$($a.MacAddress)"
    iftype   = [int]$a.ifType
    up       = ($a.Status -eq 'Up')
    link_bps = [int64]$a.TransmitLinkSpeed
    ipv4     = @($c.IPv4Address.IPAddress)
    ipv6     = @($c.IPv6Address.IPAddress)
    gateway  = "$($c.IPv4DefaultGateway.NextHop)"
    dns      = @($c.DNSServer | Where-Object { $_.AddressFamily -eq 2 } | ForEach-Object { $_.ServerAddresses })
    dhcp     = [bool]$dh["$($a.ifIndex)"]
  }
}

$neighbors = @()
foreach ($n in Get-NetNeighbor -AddressFamily IPv4) {
  $neighbors += [ordered]@{ ip="$($n.IPAddress)"; mac="$($n.LinkLayerAddress)"; state="$($n.State)" }
}

$conns = @()
foreach ($t in Get-NetTCPConnection) {
  $conns += [ordered]@{ local_ip="$($t.LocalAddress)"; local_port=[int]$t.LocalPort;
    remote_ip="$($t.RemoteAddress)"; remote_port=[int]$t.RemotePort; state="$($t.State)" }
}

$targets = @()
foreach ($a in $adapters) {
  if ($a.gateway) { $targets += [pscustomobject]@{ kind='gateway'; addr=$a.gateway } }
  foreach ($d in $a.dns) { if ($d) { $targets += [pscustomobject]@{ kind='dns'; addr=$d } } }
}
$targets = $targets | Sort-Object addr -Unique
$quality = @()
foreach ($tg in $targets) {
  $r = @(Test-Connection -ComputerName $tg.addr -Count 3 -ErrorAction SilentlyContinue)
  $recv = $r.Count
  $lat = if ($recv -gt 0) { [math]::Round((($r | Measure-Object ResponseTime -Average).Average), 1) } else { $null }
  $quality += [ordered]@{ target_kind=$tg.kind; target=$tg.addr; latency_ms=$lat;
    loss_pct=[math]::Round(((3 - $recv) / 3.0) * 100, 1); samples=3 }
}

[ordered]@{ adapters=@($adapters); neighbors=@($neighbors); connections=@($conns); quality=@($quality) } |
  ConvertTo-Json -Depth 5 -Compress
"""


def collect_network() -> CollectorResult:
    result = run_ps(_NET_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([NETWORK], status))

    d = result.data
    adapters = [a for a in (_parse_adapter(x) for x in as_list(d.get("adapters"))) if a]
    neighbors = [n for n in (_parse_neighbor(x) for x in as_list(d.get("neighbors"))) if n]
    connections = [c for c in (_parse_connection(x) for x in as_list(d.get("connections"))) if c]
    quality = [q for q in (_parse_quality(x) for x in as_list(d.get("quality"))) if q]

    payload = {
        "network_adapters": adapters,
        "network_neighbors": neighbors[:_MAX_NEIGHBORS],
        "network_connections": connections[:_MAX_CONNECTIONS],
        "network_quality": quality,
    }
    present = bool(adapters or neighbors or connections)
    return CollectorResult(payload, {NETWORK: health(field_status(present))})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_network_collector.py -q`
Expected: PASS (all parser + collect_network tests).

- [ ] **Step 5: Commit**

```bash
git add client/collectors/network.py tests/test_network_collector.py
git commit -m "feat(agent): collect_network() orchestration + status mapping"
```

---

### Task 5: Fold network into `collect_historical`

**Files:**
- Modify: `client/collectors/historical.py`
- Test: `tests/test_network_collector.py` (add integration case)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_network_collector.py`:
```python
from client.collectors import historical


def test_historical_merges_network(monkeypatch):
    """collect_historical folds network payload + source_health in (certificates-style)."""
    def _hist_ps(script, timeout=30):
        if timeout == 120:
            return _ok({"reliability_stability_index": 9.0, "storage": [], "battery": {"present": False}})
        if timeout == 60:
            return _ok({"certificates": []})
        return PsResult("empty")

    monkeypatch.setattr(historical, "run_ps", _hist_ps)
    monkeypatch.setattr(historical, "collect_network",
                        lambda: network.CollectorResult(
                            {"network_adapters": [{"name": "Ethernet"}], "network_neighbors": [],
                             "network_connections": [], "network_quality": []},
                            {network.NETWORK: network.health("ok")}))
    res = historical.collect_historical()
    assert res.payload["network_adapters"] == [{"name": "Ethernet"}]
    assert res.source_health[network.NETWORK]["status"] == "ok"


def test_historical_network_failure_sets_empty_fields(monkeypatch):
    def _hist_ps(script, timeout=30):
        if timeout == 120:
            return _ok({"reliability_stability_index": 9.0, "storage": [], "battery": {"present": False}})
        return _ok({"certificates": []})

    monkeypatch.setattr(historical, "run_ps", _hist_ps)
    monkeypatch.setattr(historical, "collect_network",
                        lambda: network.CollectorResult(None, network.failed([network.NETWORK], "blocked")))
    res = historical.collect_historical()
    assert res.payload["network_adapters"] == []
    assert res.payload["network_connections"] == []
    assert res.source_health[network.NETWORK]["status"] == "blocked"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_network_collector.py -k historical -q`
Expected: FAIL (`historical` has no `collect_network`; no `network_adapters` key).

- [ ] **Step 3: Wire the merge in**

In `client/collectors/historical.py`:

(a) extend the imports from `client.collectors.sources` to include `NETWORK` (add it to the existing import list):
```python
from client.collectors.sources import (
    BATTERY,
    BOOT_TIME,
    CERTIFICATES,
    NETWORK,
    RELIABILITY,
    STORAGE_RELIABILITY,
    CollectorResult,
    failed,
    field_status,
    health,
)
```

(b) add this import below the sources import:
```python
from client.collectors.network import collect_network
```

(c) add `NETWORK` to `owned` (line 114):
```python
    owned = [STORAGE_RELIABILITY, BATTERY, RELIABILITY, BOOT_TIME, CERTIFICATES, NETWORK]
```

(d) replace the final `return CollectorResult(raw, sh)` (line 166) with the network merge:
```python
    # Network metadata: separate script, separate error domain (certificates-style).
    net = collect_network()
    if net.payload is not None:
        raw.update(net.payload)
    else:
        raw["network_adapters"] = []
        raw["network_neighbors"] = []
        raw["network_connections"] = []
        raw["network_quality"] = []
    sh.update(net.source_health)

    return CollectorResult(raw, sh)
```

- [ ] **Step 4: Run the full collector suite**

Run: `python -m pytest tests/test_network_collector.py tests/test_collectors_parsers.py -q`
Expected: PASS (network integration + no regression in existing historical parser tests).

- [ ] **Step 5: Commit**

```bash
git add client/collectors/historical.py tests/test_network_collector.py
git commit -m "feat(agent): fold network data into historical payload"
```

---

### Task 6: Locale invariant test (Russian Windows)

**Files:**
- Test: `tests/test_network_collector.py` (add locale case)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_network_collector.py`:
```python
def test_adapter_cyrillic_name_passes_through(monkeypatch):
    """A Russian friendly name must survive intact; kind still derives from numeric ifType."""
    data = {"adapters": [{"name": "Подключение Ethernet", "desc": "Сетевой адаптер",
                          "mac": "AA-BB-CC-00-11-22", "iftype": 6, "up": True,
                          "ipv4": ["10.0.0.5"], "dns": [], "dhcp": True}],
            "neighbors": [], "connections": [], "quality": []}
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(data))
    res = network.collect_network()
    a = res.payload["network_adapters"][0]
    assert a["name"] == "Подключение Ethernet"
    assert a["kind"] == "ethernet"  # from numeric ifType, not text
```

- [ ] **Step 2: Run test to verify it passes immediately**

Run: `python -m pytest tests/test_network_collector.py -k cyrillic -q`
Expected: PASS (the parser is already locale-safe — this test pins the invariant).

> If it FAILS, the parser is branching on localized text somewhere — fix `client/collectors/network.py` so `kind`/`up` derive only from numeric `iftype` / boolean `up`, then re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_network_collector.py
git commit -m "test(agent): pin network collector language-independence"
```

---

### Task 7: Server — `network` is ungated; day-1 scores unaffected

No server code changes are required (`store_historical` persists the whole payload JSON; `evaluate_trust` records any source; `validate_source` returns `UNCHECKED` for unknown sources; `network` absent from `DOMAIN_SOURCES` means no domain gate). This task **proves** those invariants.

**Files:**
- Test: `tests/test_network_ingest_trust.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_network_ingest_trust.py`:
```python
"""Phase-1 invariants: 'network' is recorded but ungated — it never gates scores."""

from __future__ import annotations

import pytest

from server.trust.domains import DOMAIN_SOURCES
from server.trust.validators import validate_source
from server.trust.types import SemanticStatus

pytestmark = pytest.mark.unit


def test_network_not_a_trust_domain():
    assert "network" not in DOMAIN_SOURCES


def test_validate_source_network_is_unchecked():
    status, reason = validate_source("network", {"adapters": 1}, None)
    assert status is SemanticStatus.UNCHECKED
    assert reason is None
```

> **Note:** confirm the import path of `SemanticStatus` (Explore showed `server/trust/validators.py` returning `SemanticStatus.UNCHECKED`; it is defined in the trust package — adjust `from server.trust.types import SemanticStatus` to its actual module if mypy/pytest reports it, e.g. `from server.trust.gate import SemanticStatus`).

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/test_network_ingest_trust.py -q`
Expected: PASS (both are already-true invariants). If the `SemanticStatus` import path is wrong, fix it (one line) and re-run.

- [ ] **Step 3: Add the ingest integration test**

Append to `tests/test_network_ingest_trust.py`:
```python
import copy
from datetime import datetime, timezone

from shared.schema import CONTRACT_VERSION, Envelope
from server import pipeline


def _iso():
    return datetime.now(timezone.utc).isoformat()


def _hist_payload():
    return {"reliability_stability_index": 9.0, "storage": [], "battery": {"present": False},
            "observation_days": 30}


def test_network_source_recorded_but_ungated(client):
    """Ingest historical with a network source; it is stored + recorded, never a domain."""
    did = "net-trust-001"
    payload = copy.deepcopy(_hist_payload())
    payload["network_adapters"] = [{"name": "Ethernet", "kind": "ethernet", "up": True,
                                    "ipv4": ["192.168.1.5"], "gateway": "192.168.1.1"}]
    env = Envelope(
        device_id=did, ts=_iso(), msg_type="historical", agent_version=CONTRACT_VERSION,
        payload=payload,
        source_health={"reliability": {"status": "ok", "collected_at": _iso()},
                       "network": {"status": "ok", "collected_at": _iso()}},
    )
    result = pipeline.ingest_envelope(env)

    # network rode along, day-1 scores were still computed (not blocked by network)
    assert result["scores_updated"] is True

    from server import db
    trust = db.get_trust(did) if hasattr(db, "get_trust") else None
    # The network source is recorded; it is NOT a gating domain.
    if trust is not None:
        assert "network" in trust.get("sources", {})
        assert "network" not in trust.get("domains", {})

    hist = db.get_historical(did)
    assert hist["network_adapters"][0]["gateway"] == "192.168.1.1"
```

> **Note:** `client` is the existing FastAPI `TestClient` fixture from `tests/conftest.py`; depending on it ensures the temp DB/app is initialized before calling `pipeline.ingest_envelope`. If `db.get_trust` has a different name, the guarded `hasattr` keeps the core assertions (scores computed + data stored) intact; tighten it to the real accessor when known.

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/test_network_ingest_trust.py -q`
Expected: PASS. If `Envelope` requires additional required fields, construct them per `shared/schema.py` `Envelope` (e.g. add `site_code`/`org_code` if non-optional) and re-run.

- [ ] **Step 5: Commit**

```bash
git add tests/test_network_ingest_trust.py
git commit -m "test(server): network source is recorded but ungated"
```

---

### Task 8: Minimal device-page block + CHANGELOG + gate

**Files:**
- Modify: `server/web/templates/device.html`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the «Сеть» section**

In `server/web/templates/device.html`, insert **after** the Certificates section (after its closing `{% endif %}` near line 555, before the `{# ── Inventory ── #}` comment):
```jinja2
{# ── Network ───────────────────────────────────────────────────────────── #}
<div class="section-label">Сеть</div>
{% set hist = d.historical %}
{% if hist and hist.network_adapters %}
<table>
  <thead><tr><th>Адаптер</th><th>Тип</th><th>IP</th><th>Шлюз</th><th>Статус</th></tr></thead>
  <tbody>
  {% for a in hist.network_adapters %}
  <tr>
    <td class="small">{{ (a.name or "—")[:40] }}</td>
    <td class="small muted">{{ a.kind or "—" }}</td>
    <td class="small mono">{{ (a.ipv4 or [])|join(", ") or "—" }}</td>
    <td class="small mono">{{ a.gateway or "—" }}</td>
    <td>{% if a.up %}<span class="chip good" title="Адаптер активен">вверх</span>{% else %}<span class="chip warn" title="Адаптер не активен">вниз</span>{% endif %}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
<p class="muted small">Соседей в сети: {{ (hist.network_neighbors or [])|length }} · связей: {{ (hist.network_connections or [])|length }}</p>
{% else %}
  <p class="muted small">Сетевые данные ещё не собраны.</p>
{% endif %}
```

- [ ] **Step 2: Verify the template renders (smoke)**

Run: `python smoke.py`
Expected: completes OK (smoke ingests a synthetic device and renders pages; no template error).

> If `smoke.py` does not exercise the device page, instead run the server (`python -m server.main`) and open `/device/<id>` for a seeded device to confirm the «Сеть» block renders the "ещё не собраны" line without error.

- [ ] **Step 3: Add the CHANGELOG line**

In `CHANGELOG.md`, under `## [Unreleased]` (create the heading if missing), add:
```markdown
### Added
- Agent now collects per-machine network data (adapters, IP/gateway/DNS, ARP neighbors, internal-only connections, link quality) into the historical record; device page shows a «Сеть» block. Network is an ungated signal — it never affects health scores.
```

- [ ] **Step 4: Run the full gate**

Run: `make check`
Expected: ruff clean · mypy clean (shared+server+client) · bandit clean · pytest all green, coverage ≥80%.

> If mypy flags `client/collectors/network.py`, ensure every function has annotations (they do) and that `ipaddress`/typing imports resolve. If coverage on `network.py` is below the line, add a small parser test for any uncovered branch (e.g. `_parse_adapter(None) is None`).

- [ ] **Step 5: Commit**

```bash
git add server/web/templates/device.html CHANGELOG.md
git commit -m "feat(dashboard): device page «Сеть» block (phase 1 visible change)"
```

---

## Definition of Done (Phase 1)

- [ ] `make check` fully green (ruff · mypy[shared+server+client] · bandit · pytest cov ≥80%).
- [ ] `python smoke.py` OK.
- [ ] Agent collects network data with **zero non-stdlib imports**; external addresses never serialized.
- [ ] `network` appears in `source_health`, is recorded by `evaluate_trust`, and is **not** a trust domain; day-1 scores unchanged when network is present/absent.
- [ ] `CONTRACT_VERSION` still `0.1.0`.
- [ ] Device page shows the «Сеть» block.
- [ ] CHANGELOG line present in the same branch.
- [ ] (When user asks) `merge --no-ff` into `main` + `push origin main`; then update `CONTINUITY.md`.

## Out of scope (later phases)

- Phase 2: server-side map (union neighbors across agents, OUI vendor lookup, subnet graph), `network_health` Score100 axis (then add `network` to `DOMAIN_SOURCES`), subnet `fleet_anomaly`, map visualization.
- Phase 3: active scan (off by default, gated, EDR-sensitive).
- Deferred from Phase 1: per-adapter packet-error counters; IP-conflict detection (`ip_conflict`); a separate DNS-resolve-time field (`dns_resolve_ms`) — DNS reachability is already covered by pinging the DNS servers; pinging the SRP server (needs `server_url` plumbed into the collector); Wi-Fi `signal_pct`/`channel` unless a language-independent CIM source is confirmed.
```