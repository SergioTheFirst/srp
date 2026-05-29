# Telemetry-Trust (W0.3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure server-side trust core that classifies every telemetry source into
an authoritative `state` (gate) + secondary `weight` (modulation), and resolves per-domain
trust into TRUSTED / UNKNOWN / NOT_APPLICABLE — so scoring can refuse to claim under
uncertainty.

**Architecture:** New pure-functional package `server/trust/` (zero I/O, fully unit-testable).
Two orthogonal inputs per source — `collector_status` (from agent) and `semantic_status`
(from server validators) — derive a `SourceState` via a fixed precedence; `weight` is computed
ONLY for gate-pass states and can never reanimate a gate-failed source. Domain resolution is
Tiered: required-source gate-fail → domain UNKNOWN; optional gate-fail → trusted with the
dead source dropped. Contract: `telemetry-trust-contract.md`.

**Tech Stack:** Python 3.9 (explicit `Optional`, double quotes, line-length 100), frozen
dataclasses, `enum.Enum`, pytest (`pytest.mark.unit`). mypy + coverage already scope `server/`.

---

## Sub-plan decomposition (this file = Plan 1)

- **Plan 1 — Trust Core (THIS PLAN):** `server/trust/` pure logic — states, gate derivation,
  weight, semantic validators (stateless + frozen-on-last-good), Tiered domain resolution.
  No wiring. Output: a fully unit-tested library matching acceptance §14 of the contract.
- **Plan 2 — Contract + agent collector-status:** `source_health` block in `shared/schema.py`;
  `client/collectors/*` + `ps.py` emit `collector_status` (ok/partial/empty/timeout/blocked/
  absent) instead of bare `None`. Golden-fixture parser tests.
- **Plan 3 — Integration:** wire trust into `server/pipeline.py` + `server/scoring/*`
  (confidence-gated scores, UNKNOWN outcome), persist last-good-per-source + lineage in
  `server/db.py`, capability matrix (newly-blocked detection), surface source health on the
  dashboard. Visible behavior → CHANGELOG entries here.

Plan 1 is purely internal (a library): **no CHANGELOG entries**, no visible behavior change.

---

## File Structure (Plan 1)

- Create `server/trust/__init__.py` — package exports.
- Create `server/trust/states.py` — enums (`CollectorStatus`, `SemanticStatus`, `SourceState`) + `SourceTrust` dataclass.
- Create `server/trust/gate.py` — `derive_state()` + `compute_weight()` (the load-bearing gate/modulation rule).
- Create `server/trust/validators.py` — semantic validators + materiality dispatch (`validate_source()`).
- Create `server/trust/domains.py` — domain→source map + `resolve_domain_trust()` (Tiered).
- Create `tests/test_trust_gate.py`, `tests/test_trust_validators.py`, `tests/test_trust_domains.py`, `tests/test_trust_acceptance.py`.

Each file has one responsibility; all pure (no DB, no FastAPI).

---

### Task 1: Trust states + `SourceTrust` value object

**Files:**
- Create: `server/trust/states.py`
- Create: `server/trust/__init__.py`
- Test: `tests/test_trust_gate.py` (shared with Task 2/3)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trust_gate.py
from __future__ import annotations

import pytest
from server.trust.states import (
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
)

pytestmark = pytest.mark.unit


def test_gate_pass_only_for_ok_and_degraded():
    ok = SourceTrust("storage_reliability", SourceState.OK, 1.0,
                     CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    degraded = SourceTrust("free_space", SourceState.DEGRADED, 0.5,
                           CollectorStatus.PARTIAL, SemanticStatus.PLAUSIBLE)
    suspect = SourceTrust("throttle", SourceState.SUSPECT, 0.0,
                          CollectorStatus.OK, SemanticStatus.FROZEN)
    assert ok.passes_gate is True
    assert degraded.passes_gate is True
    assert suspect.passes_gate is False


def test_source_trust_is_immutable():
    t = SourceTrust("battery", SourceState.OK, 1.0,
                    CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    with pytest.raises(Exception):
        t.weight = 0.9  # frozen dataclass -> FrozenInstanceError
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.trust'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/trust/states.py
"""Trust state model: the authoritative gate + modulation weight per source.

A source is classified by two orthogonal inputs -- collector_status (did we get
the data?) and semantic_status (is the data believable?). From them we derive a
SourceState (the gate) and a weight (modulation, meaningful only when the gate
passes). The weight can never reanimate a gate-failed source.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CollectorStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    ABSENT = "absent"


class SemanticStatus(str, Enum):
    PLAUSIBLE = "plausible"
    IMPLAUSIBLE = "implausible"
    INCONSISTENT = "inconsistent"
    FROZEN = "frozen"
    KNOWN_BAD = "known_bad"
    UNCHECKED = "unchecked"


class SourceState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    SUSPECT = "suspect"
    NOT_APPLICABLE = "not_applicable"


GATE_PASS = frozenset({SourceState.OK, SourceState.DEGRADED})


@dataclass(frozen=True)
class SourceTrust:
    source: str
    state: SourceState
    weight: float                       # [0..1]; meaningful only when passes_gate
    collector_status: CollectorStatus
    semantic_status: SemanticStatus
    age_sec: Optional[float] = None
    reason: Optional[str] = None

    @property
    def passes_gate(self) -> bool:
        return self.state in GATE_PASS
```

```python
# server/trust/__init__.py
"""Pure server-side telemetry-trust core (see telemetry-trust-contract.md)."""

from server.trust.states import (
    GATE_PASS,
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
)

__all__ = [
    "GATE_PASS",
    "CollectorStatus",
    "SemanticStatus",
    "SourceState",
    "SourceTrust",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust_gate.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
make check
git add server/trust/__init__.py server/trust/states.py tests/test_trust_gate.py
git commit -m "feat: add telemetry-trust state model (gate + weight value object)"
```

---

### Task 2: Gate derivation (`derive_state`)

**Files:**
- Create: `server/trust/gate.py`
- Test: `tests/test_trust_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_trust_gate.py
from server.trust.gate import derive_state


def _state(collector, semantic, age=0.0, stale_after=300.0, applicable=True):
    return derive_state(collector, semantic, age, stale_after, applicable)


def test_not_applicable_wins_over_everything():
    # Battery on a desktop: not a degradation, the domain simply does not exist.
    s = _state(CollectorStatus.ABSENT, SemanticStatus.UNCHECKED, applicable=False)
    assert s is SourceState.NOT_APPLICABLE


def test_suspect_beats_collector_ok():
    # A fresh, complete, but lying source is more dangerous than an absent one.
    s = _state(CollectorStatus.OK, SemanticStatus.FROZEN)
    assert s is SourceState.SUSPECT


def test_collector_failure_is_unavailable():
    s = _state(CollectorStatus.BLOCKED, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.UNAVAILABLE


def test_old_sample_is_stale():
    s = _state(CollectorStatus.OK, SemanticStatus.PLAUSIBLE, age=9000.0, stale_after=300.0)
    assert s is SourceState.STALE


def test_partial_payload_is_degraded():
    s = _state(CollectorStatus.PARTIAL, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.DEGRADED


def test_clean_source_is_ok():
    s = _state(CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_gate.py -v`
Expected: FAIL with `ImportError: cannot import name 'derive_state'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/trust/gate.py
"""Gate derivation + weight. state = authoritative gate, weight = modulation only.

Precedence (first match wins): NOT_APPLICABLE -> SUSPECT (semantics beats collector)
-> UNAVAILABLE -> STALE -> DEGRADED -> OK. weight is computed ONLY for gate-pass
states; a gate-failed source gets 0.0 and can never be reanimated by weight.
"""

from __future__ import annotations

from typing import Optional

from server.trust.states import CollectorStatus, SemanticStatus, SourceState

_COLLECTOR_FAIL = frozenset(
    {CollectorStatus.EMPTY, CollectorStatus.TIMEOUT, CollectorStatus.BLOCKED, CollectorStatus.ABSENT}
)
_SEMANTIC_SUSPECT = frozenset(
    {SemanticStatus.IMPLAUSIBLE, SemanticStatus.INCONSISTENT, SemanticStatus.FROZEN, SemanticStatus.KNOWN_BAD}
)


def derive_state(
    collector_status: CollectorStatus,
    semantic_status: SemanticStatus,
    age_sec: Optional[float],
    stale_after_sec: Optional[float],
    applicable: bool = True,
) -> SourceState:
    if not applicable:
        return SourceState.NOT_APPLICABLE
    if semantic_status in _SEMANTIC_SUSPECT:
        return SourceState.SUSPECT
    if collector_status in _COLLECTOR_FAIL:
        return SourceState.UNAVAILABLE
    if stale_after_sec is not None and age_sec is not None and age_sec > stale_after_sec:
        return SourceState.STALE
    if collector_status == CollectorStatus.PARTIAL:
        return SourceState.DEGRADED
    return SourceState.OK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust_gate.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
make check
git add server/trust/gate.py tests/test_trust_gate.py
git commit -m "feat: add trust gate derivation with fixed precedence"
```

---

### Task 3: Weight rule (`compute_weight`) — cannot reanimate a failed source

**Files:**
- Modify: `server/trust/gate.py`
- Test: `tests/test_trust_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_trust_gate.py
from server.trust.gate import compute_weight


def test_weight_full_for_ok():
    assert compute_weight(SourceState.OK) == 1.0


def test_weight_attenuated_for_degraded():
    assert compute_weight(SourceState.DEGRADED) == 0.5


@pytest.mark.parametrize(
    "state",
    [SourceState.STALE, SourceState.UNAVAILABLE, SourceState.SUSPECT, SourceState.NOT_APPLICABLE],
)
def test_weight_zero_for_gate_fail(state):
    # Hard rule: weight never reanimates a gate-failed source.
    assert compute_weight(state) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_gate.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_weight'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to server/trust/gate.py
from server.trust.states import GATE_PASS  # add to existing imports at top instead

_DEGRADED_WEIGHT = 0.5  # single attenuation band; no continuous calculus (scope ceiling)


def compute_weight(state: SourceState) -> float:
    if state == SourceState.OK:
        return 1.0
    if state == SourceState.DEGRADED:
        return _DEGRADED_WEIGHT
    return 0.0  # gate-fail: weight is irrelevant and never reanimates
```

> Note: move `GATE_PASS` into the top-of-file import line (`from server.trust.states import
> CollectorStatus, SemanticStatus, SourceState`) only if you reference it here; this task
> does not require it, so leave imports as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust_gate.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
make check
git add server/trust/gate.py tests/test_trust_gate.py
git commit -m "feat: add trust weight rule (gate-fail -> 0.0, no reanimation)"
```

---

### Task 4: Stateless semantic validators (range + cross-field)

**Files:**
- Create: `server/trust/validators.py`
- Test: `tests/test_trust_validators.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trust_validators.py
from __future__ import annotations

import pytest
from server.trust.states import SemanticStatus
from server.trust.validators import validate_battery, validate_scalar_range, validate_storage_item

pytestmark = pytest.mark.unit


def test_storage_wear_above_100_is_implausible():
    status, reason = validate_storage_item({"wear_pct": 140.0}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE
    assert reason is not None


def test_storage_negative_counter_is_implausible():
    status, _ = validate_storage_item({"reallocated_sectors": -5}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_storage_clean_item_is_plausible():
    status, _ = validate_storage_item({"wear_pct": 12.0, "power_on_hours": 5200}, last=None)
    assert status is SemanticStatus.PLAUSIBLE


def test_battery_full_above_design_is_inconsistent():
    status, _ = validate_battery({"present": True, "design_capacity_mwh": 50000,
                                  "full_charge_capacity_mwh": 60000})
    assert status is SemanticStatus.INCONSISTENT


def test_battery_present_without_design_is_inconsistent():
    status, _ = validate_battery({"present": True, "design_capacity_mwh": None})
    assert status is SemanticStatus.INCONSISTENT


def test_scalar_out_of_range_is_implausible():
    status, _ = validate_scalar_range("free_space", 142.0, 0.0, 100.0)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_scalar_in_range_is_plausible():
    status, _ = validate_scalar_range("free_space", 61.0, 0.0, 100.0)
    assert status is SemanticStatus.PLAUSIBLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_validators.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.trust.validators'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/trust/validators.py
"""Semantic plausibility validators (server-side judgment).

Stateless checks (range, cross-field) + a frozen/impossible-delta check that uses
ONLY the last-good sample (one row, no full history -- trend-based validation is
deferred to W0.1). Materiality governor: only decision-material sources are checked;
everything else returns UNCHECKED and can never become SUSPECT.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from server.trust.states import SemanticStatus

Result = Tuple[SemanticStatus, Optional[str]]

_OK: Result = (SemanticStatus.PLAUSIBLE, None)


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def validate_scalar_range(source: str, value: Any, lo: float, hi: float) -> Result:
    v = _num(value)
    if v is None:
        return _OK  # absence is a collector concern, not a semantic one
    if v < lo or v > hi:
        return SemanticStatus.IMPLAUSIBLE, f"{source}={v} outside [{lo},{hi}]"
    return _OK


def validate_storage_item(item: dict, last: Optional[dict]) -> Result:
    wear = _num(item.get("wear_pct"))
    if wear is not None and (wear < 0 or wear > 100):
        return SemanticStatus.IMPLAUSIBLE, f"wear_pct={wear}"
    for key in ("reallocated_sectors", "power_on_hours", "read_errors_total", "write_errors_total"):
        cur = _num(item.get(key))
        if cur is not None and cur < 0:
            return SemanticStatus.IMPLAUSIBLE, f"{key}={cur} (negative)"
        if last is not None:
            prev = _num(last.get(key))
            if cur is not None and prev is not None and cur < prev:
                return SemanticStatus.INCONSISTENT, f"{key} dropped {prev}->{cur} (counter reset)"
    return _OK


def validate_battery(bat: dict) -> Result:
    if not bat.get("present"):
        return _OK
    design = _num(bat.get("design_capacity_mwh"))
    full = _num(bat.get("full_charge_capacity_mwh"))
    wear = _num(bat.get("wear_pct"))
    if design is None:
        return SemanticStatus.INCONSISTENT, "battery present without design capacity"
    if full is not None and design > 0 and full > design * 1.05:  # 5% slack for sensor noise
        return SemanticStatus.INCONSISTENT, f"full({full})>design({design})"
    if wear is not None and (wear < 0 or wear > 100):
        return SemanticStatus.IMPLAUSIBLE, f"battery wear_pct={wear}"
    return _OK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust_validators.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
make check
git add server/trust/validators.py tests/test_trust_validators.py
git commit -m "feat: add stateless semantic validators (range + cross-field)"
```

---

### Task 5: Frozen / impossible-delta check on last-good

**Files:**
- Modify: `server/trust/validators.py`
- Test: `tests/test_trust_validators.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_trust_validators.py
from server.trust.validators import validate_frozen_constant


def test_thermal_constant_across_samples_is_frozen():
    # OEM fake-constant: throttle proxy never moves between samples.
    status, reason = validate_frozen_constant("throttle", value=27.0, last_value=27.0)
    assert status is SemanticStatus.FROZEN
    assert reason is not None


def test_thermal_changing_value_is_plausible():
    status, _ = validate_frozen_constant("throttle", value=83.0, last_value=97.0)
    assert status is SemanticStatus.PLAUSIBLE


def test_frozen_check_no_history_is_plausible():
    # One sample is not enough to call frozen; defer (needs >=1 prior).
    status, _ = validate_frozen_constant("throttle", value=27.0, last_value=None)
    assert status is SemanticStatus.PLAUSIBLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_validators.py -v`
Expected: FAIL with `ImportError: cannot import name 'validate_frozen_constant'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to server/trust/validators.py
def validate_frozen_constant(source: str, value: Any, last_value: Any) -> Result:
    """Flag a should-vary metric that is byte-identical to its previous sample.

    Weak 1-sample signal (one prior only); multi-sample volatility is deferred to
    W0.1 once history exists. Used for the throttle/thermal proxy (OEM fake-constant).
    """
    cur = _num(value)
    prev = _num(last_value)
    if cur is None or prev is None:
        return _OK
    if cur == prev:
        return SemanticStatus.FROZEN, f"{source} constant at {cur} across samples"
    return _OK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust_validators.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
make check
git add server/trust/validators.py tests/test_trust_validators.py
git commit -m "feat: add frozen-constant semantic check on last-good sample"
```

---

### Task 6: Materiality dispatch + known-bad hook (`validate_source`)

**Files:**
- Modify: `server/trust/validators.py`
- Test: `tests/test_trust_validators.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_trust_validators.py
from server.trust.validators import MATERIAL_SOURCES, validate_source


def test_immaterial_source_is_unchecked():
    # CPU% / queue length never get semantic validation (materiality governor).
    status, _ = validate_source("cpu_pct", {"value": 9999.0}, last=None)
    assert status is SemanticStatus.UNCHECKED


def test_material_storage_routes_to_storage_validator():
    status, _ = validate_source("storage_reliability", {"wear_pct": 200.0}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_known_bad_firmware_is_flagged():
    status, reason = validate_source(
        "storage_reliability",
        {"model": "BadSSD X1", "firmware": "EVIL01", "wear_pct": 3.0},
        last=None,
    )
    assert status is SemanticStatus.KNOWN_BAD
    assert "EVIL01" in (reason or "")


def test_throttle_routes_to_frozen_check():
    status, _ = validate_source("throttle", {"value": 27.0}, last={"value": 27.0})
    assert status is SemanticStatus.FROZEN


def test_free_space_material_and_range_checked():
    status, _ = validate_source("free_space", {"value": 150.0}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_material_sources_set_excludes_raw_perf():
    assert "cpu_pct" not in MATERIAL_SOURCES
    assert "disk_queue" not in MATERIAL_SOURCES
    assert "storage_reliability" in MATERIAL_SOURCES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_validators.py -v`
Expected: FAIL with `ImportError: cannot import name 'validate_source'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to server/trust/validators.py

# Decision-material sources only (materiality governor, contract sec.9). Everything
# else -> UNCHECKED, and an UNCHECKED source can never become SUSPECT.
MATERIAL_SOURCES = frozenset(
    {"storage_reliability", "battery", "free_space", "reliability", "boot_time", "throttle", "event_counts"}
)

# Seed known-bad registry (a hook, not a platform): (model_substr, firmware) -> reason.
# Real list curated out-of-band later; this is the wiring point.
_KNOWN_BAD_FIRMWARE = {
    ("BadSSD X1", "EVIL01"): "known-bad firmware (advisory seed)",
}


def _known_bad(item: dict) -> Result:
    model = str(item.get("model") or "")
    fw = str(item.get("firmware") or "")
    for (m, f), reason in _KNOWN_BAD_FIRMWARE.items():
        if m in model and f == fw:
            return SemanticStatus.KNOWN_BAD, f"{reason}: {model}/{fw}"
    return _OK


def validate_source(source: str, reading: dict, last: Optional[dict]) -> Result:
    if source not in MATERIAL_SOURCES:
        return SemanticStatus.UNCHECKED, None
    if source == "storage_reliability":
        kb = _known_bad(reading)
        if kb[0] is not SemanticStatus.PLAUSIBLE:
            return kb
        return validate_storage_item(reading, last)
    if source == "battery":
        return validate_battery(reading)
    if source == "free_space":
        return validate_scalar_range(source, reading.get("value"), 0.0, 100.0)
    if source == "reliability":
        return validate_scalar_range(source, reading.get("value"), 0.0, 10.0)
    if source == "boot_time":
        return validate_scalar_range(source, reading.get("value"), 0.0, 600000.0)
    if source == "throttle":
        last_value = (last or {}).get("value")
        return validate_frozen_constant(source, reading.get("value"), last_value)
    if source == "event_counts":
        for key, val in reading.items():
            status, reason = validate_scalar_range(key, val, 0.0, 1_000_000.0)
            if status is not SemanticStatus.PLAUSIBLE:
                return status, reason
        return _OK
    return _OK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust_validators.py -v`
Expected: PASS (16 passed)

- [ ] **Step 5: Commit**

```bash
make check
git add server/trust/validators.py tests/test_trust_validators.py
git commit -m "feat: add materiality dispatch + known-bad hook for semantic validation"
```

---

### Task 7: Domain map + Tiered resolution (`resolve_domain_trust`)

**Files:**
- Create: `server/trust/domains.py`
- Modify: `server/trust/__init__.py`
- Test: `tests/test_trust_domains.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trust_domains.py
from __future__ import annotations

import pytest
from server.trust.domains import DomainTrust, DomainTrustState, resolve_domain_trust
from server.trust.states import CollectorStatus, SemanticStatus, SourceState, SourceTrust

pytestmark = pytest.mark.unit


def _src(name, state, weight):
    return SourceTrust(name, state, weight, CollectorStatus.OK, SemanticStatus.PLAUSIBLE)


def test_required_source_unavailable_makes_domain_unknown():
    sources = {"storage_reliability": _src("storage_reliability", SourceState.UNAVAILABLE, 0.0)}
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.UNKNOWN
    assert d.weight == 0.0


def test_required_source_missing_makes_domain_unknown():
    d = resolve_domain_trust("storage", {})  # nothing reported at all
    assert d.state is DomainTrustState.UNKNOWN


def test_battery_not_applicable_propagates():
    sources = {"battery": _src("battery", SourceState.NOT_APPLICABLE, 0.0)}
    d = resolve_domain_trust("battery", sources)
    assert d.state is DomainTrustState.NOT_APPLICABLE


def test_optional_source_failure_keeps_domain_trusted_but_drops_it():
    sources = {
        "storage_reliability": _src("storage_reliability", SourceState.OK, 1.0),
        "disk_latency": _src("disk_latency", SourceState.SUSPECT, 0.0),
    }
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.TRUSTED
    assert "disk_latency" in d.dropped
    assert d.contributing == ["storage_reliability"]


def test_degraded_required_lowers_domain_weight():
    sources = {"storage_reliability": _src("storage_reliability", SourceState.DEGRADED, 0.5)}
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.TRUSTED
    assert d.weight == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_domains.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.trust.domains'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/trust/domains.py
"""Domain trust resolution (Tiered).

Each scoring domain declares required + optional sources. A required source that
fails the gate makes the whole domain UNKNOWN (never optimistic). An optional source
that fails is simply dropped; the domain stays TRUSTED on its required sources, at a
weight that reflects any degraded required source. A required NOT_APPLICABLE source
(e.g. battery on a desktop) makes the domain NOT_APPLICABLE -- not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from server.trust.states import SourceState, SourceTrust

DOMAIN_SOURCES: Dict[str, Dict[str, List[str]]] = {
    "storage": {"required": ["storage_reliability"], "optional": ["disk_latency"]},
    "battery": {"required": ["battery"], "optional": []},
    "disk_fill": {"required": ["free_space"], "optional": []},
    "os_stability": {"required": ["reliability"], "optional": []},
    "boot": {"required": ["boot_time"], "optional": []},
    "thermal": {"required": ["throttle"], "optional": []},
}


class DomainTrustState(str, Enum):
    TRUSTED = "trusted"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class DomainTrust:
    domain: str
    state: DomainTrustState
    weight: float
    contributing: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    reason: str = ""


def resolve_domain_trust(domain: str, sources: Dict[str, SourceTrust]) -> DomainTrust:
    spec = DOMAIN_SOURCES.get(domain)
    if spec is None:
        return DomainTrust(domain, DomainTrustState.UNKNOWN, 0.0, reason="unknown domain")

    required_weights: List[float] = []
    contributing: List[str] = []
    dropped: List[str] = []

    for name in spec["required"]:
        st = sources.get(name)
        if st is None:
            return DomainTrust(domain, DomainTrustState.UNKNOWN, 0.0,
                               reason=f"required source {name} not reported")
        if st.state is SourceState.NOT_APPLICABLE:
            return DomainTrust(domain, DomainTrustState.NOT_APPLICABLE, 0.0,
                               reason=f"{name} not applicable")
        if not st.passes_gate:
            return DomainTrust(domain, DomainTrustState.UNKNOWN, 0.0,
                               reason=f"required source {name} is {st.state.value}")
        required_weights.append(st.weight)
        contributing.append(name)

    for name in spec["optional"]:
        st = sources.get(name)
        if st is not None and st.passes_gate:
            contributing.append(name)
        elif st is not None:
            dropped.append(name)

    weight = min(required_weights) if required_weights else 0.0
    return DomainTrust(domain, DomainTrustState.TRUSTED, weight, contributing, dropped)
```

```python
# server/trust/__init__.py  -- extend __all__ and imports
from server.trust.domains import DOMAIN_SOURCES, DomainTrust, DomainTrustState, resolve_domain_trust
from server.trust.gate import compute_weight, derive_state
from server.trust.states import (
    GATE_PASS,
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
)
from server.trust.validators import MATERIAL_SOURCES, validate_source

__all__ = [
    "GATE_PASS", "CollectorStatus", "SemanticStatus", "SourceState", "SourceTrust",
    "derive_state", "compute_weight", "validate_source", "MATERIAL_SOURCES",
    "DOMAIN_SOURCES", "DomainTrust", "DomainTrustState", "resolve_domain_trust",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trust_domains.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
make check
git add server/trust/domains.py server/trust/__init__.py tests/test_trust_domains.py
git commit -m "feat: add Tiered domain trust resolution (required-fail -> UNKNOWN)"
```

---

### Task 8: Acceptance scenarios (contract §14) end-to-end through the trust core

**Files:**
- Test: `tests/test_trust_acceptance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trust_acceptance.py
"""Acceptance scenarios from telemetry-trust-contract.md sec.14, exercised through
the pure trust core (derive_state -> compute_weight -> resolve_domain_trust)."""

from __future__ import annotations

import pytest
from server.trust import (
    CollectorStatus,
    DomainTrustState,
    SemanticStatus,
    SourceState,
    SourceTrust,
    compute_weight,
    derive_state,
    resolve_domain_trust,
    validate_source,
)

pytestmark = pytest.mark.unit


def _trust(source, collector, semantic, age=0.0, stale_after=300.0, applicable=True):
    state = derive_state(collector, semantic, age, stale_after, applicable)
    return SourceTrust(source, state, compute_weight(state), collector, semantic)


def test_disk_unavailable_yields_unknown_not_healthy():
    # Required SMART source blocked -> storage domain MUST be UNKNOWN, never healthy.
    sources = {"storage_reliability": _trust("storage_reliability", CollectorStatus.BLOCKED,
                                             SemanticStatus.UNCHECKED)}
    d = resolve_domain_trust("storage", sources)
    assert d.state is DomainTrustState.UNKNOWN


def test_thermal_fake_constant_is_suspect_and_zero_weight():
    semantic, _ = validate_source("throttle", {"value": 27.0}, last={"value": 27.0})
    t = _trust("throttle", CollectorStatus.OK, semantic)
    assert t.state is SourceState.SUSPECT
    assert t.weight == 0.0
    assert resolve_domain_trust("thermal", {"throttle": t}).state is DomainTrustState.UNKNOWN


def test_desktop_battery_is_not_applicable_no_noise():
    t = _trust("battery", CollectorStatus.ABSENT, SemanticStatus.UNCHECKED, applicable=False)
    d = resolve_domain_trust("battery", {"battery": t})
    assert d.state is DomainTrustState.NOT_APPLICABLE


def test_weight_cannot_reanimate_a_suspect_source():
    t = _trust("storage_reliability", CollectorStatus.OK, SemanticStatus.IMPLAUSIBLE)
    assert t.state is SourceState.SUSPECT
    assert t.weight == 0.0


def test_immaterial_signal_never_drives_suspect():
    semantic, _ = validate_source("cpu_pct", {"value": 9999.0}, last=None)
    assert semantic is SemanticStatus.UNCHECKED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trust_acceptance.py -v`
Expected: FAIL only if an export is missing; otherwise these pin behavior already built. If any fail, fix the relevant module from Tasks 1-7 (do not weaken the test).

- [ ] **Step 3: Write minimal implementation**

No new implementation expected — Tasks 1-7 cover this. If a test fails, the gap is a real bug in the trust core; fix it in the owning module.

- [ ] **Step 4: Run full suite + gate**

Run: `python -m pytest tests/test_trust_acceptance.py -v`
Expected: PASS (5 passed)
Run: `make check`
Expected: lint + mypy + bandit + coverage(>=80%) all green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_trust_acceptance.py
git commit -m "test: pin telemetry-trust acceptance scenarios (contract sec.14)"
```

---

## Self-Review (Plan 1 vs contract)

**Spec coverage:**
- §3 state=gate/weight=modulation → Tasks 2,3 (+ acceptance test "weight cannot reanimate"). ✅
- §4 collector ⊥ semantic → two distinct enums (Task 1), separate validator path (Tasks 4-6). ✅
- §5 state enum + precedence (NOT_APPLICABLE → SUSPECT → UNAVAILABLE → STALE → DEGRADED → OK) → Task 2. ✅
- §6 UNKNOWN first-class → `DomainTrustState.UNKNOWN` (Task 7) + acceptance (Task 8). Scoring HEALTHY/DEGRADED/AT_RISK mapping → **Plan 3** (trusted domains feed scoring). ✅ (boundary noted)
- §7 mandatory/optional + domain map → `DOMAIN_SOURCES` (Task 7). Global-mandatory identity gate (device `untrusted`) → **Plan 3** (needs device-level wiring). ✅ (boundary noted)
- §8 Tiered reaction → `resolve_domain_trust` (Task 7). ✅
- §9 materiality → `MATERIAL_SOURCES` + dispatch (Task 6); thermal = frozen-only (Tasks 5,6). ✅
- §10 validators v1 (range/cross-field/known-bad/frozen-on-last-good) → Tasks 4,5,6; trend-based deferred to W0.1 (not in scope). ✅
- §11 lineage → `SourceTrust` carries collector/semantic/state/weight/reason (Task 1); persistence of lineage → **Plan 3**. ✅ (boundary noted)
- §12 agent↔server contract (`source_health`) → **Plan 2**. §12 capability matrix → **Plan 3**. (Correctly out of Plan 1.)
- §13 scope ceiling → single weight band, no nested confidence; enforced by design. ✅

**Placeholder scan:** none — every step has runnable code/commands. Known-bad registry is a seed + hook (intentional, per §10), not a placeholder.

**Type consistency:** `SourceTrust`, `SourceState`, `CollectorStatus`, `SemanticStatus`,
`validate_source(source, reading, last)`, `resolve_domain_trust(domain, sources)`,
`DomainTrust`/`DomainTrustState` — names identical across Tasks 1-8. `validate_source` reading
contract: scalars passed as `{"value": x}`, storage/battery as field dicts — consistent in
Tasks 6 and 8.

**Open items for Plans 2-3 (carried, not gaps):** `source_health` schema + agent emission
(Plan 2); device-level `untrusted` gate, last-good persistence, lineage storage, capability
matrix, scoring UNKNOWN mapping, dashboard surfacing (Plan 3).
