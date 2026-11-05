"""Spec 2026-10-09 §8.2 G4 / plan T8: the calibrated verdict through the production path.

pipeline.ingest_envelope -> pipeline.recompute_scores (worst_disk_key ->
db.get_disk_series -> compute_trends -> analyze_events -> compute_storage_risk ->
compute_health, prev_health round-tripped through the scores table) over a simulated
4 h agent cycle. The clock is patched where pipeline reads it -- ``_now_iso`` and its
module-level ``datetime`` -- the way research/sim_health.py drives it; one recompute
per cycle, as the rescore queue coalesces a cycle's envelopes.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

import pytest
from server import db, pipeline
from server.analytics.health_hold import PHANTOM_MISSING
from server.trust import staleness
from shared.schema import Envelope, SourceHealth

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
DEVICE = "calib-int-01"
HDD_MODEL = "ST1000DM010-2EP102"
NVME_MODEL = "NVMe SSD 512GB"
_HIST_SOURCES = ("storage_reliability", "smart", "reliability", "boot_time", "network")
_HB_SOURCES = ("throttle", "free_space")
_BAD_BLOCK = {"log": "System", "source": "disk", "event_id": 7, "level": "Error"}


class _Clock(datetime):
    t = START

    @classmethod
    def now(cls, tz=None):  # pipeline only ever asks for now(timezone.utc)
        return cls.t if tz is None else cls.t.astimezone(tz)


class _NoQueue:  # the explicit recompute below is the cycle's only one
    def submit(self, device_id: str) -> None:
        pass


@pytest.fixture
def sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Speed only: synchronous=OFF and one idle keeper connection (no fsync per commit,
    # no WAL checkpoint per close) change no query semantics.
    connect = sqlite3.connect

    def fast_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    monkeypatch.setattr(sqlite3, "connect", fast_connect)
    db.init_db(tmp_path / "calib.db")
    keeper = connect(str(tmp_path / "calib.db"))
    _Clock.t = START
    monkeypatch.setattr(pipeline, "datetime", _Clock)
    # db ages disk keys (90-day eviction) and cohort windows on its own clock: off the
    # simulated one, the X4 predecessor key is evicted once real time passes START + 90 d
    monkeypatch.setattr(db, "datetime", _Clock)
    monkeypatch.setattr(pipeline, "_now_iso", lambda: _Clock.t.isoformat())
    pipeline.set_rescore_queue(_NoQueue())  # conftest resets the queue after every test
    yield
    keeper.close()


# --------------------------------------------------------------------------- #
# Payloads (shapes = shared/schema.py, values = what the agent emits)
# --------------------------------------------------------------------------- #
def _hdd(serial: Optional[str], attrs: Optional[dict] = None) -> dict:
    smart = {"5": 0, "9": 26000, "187": 0, "188": 0, "194": 36, "197": 0, "198": 0}
    return {
        "disk": HDD_MODEL,
        "media_type": "HDD",
        "bus_type": "SATA",
        "serial_hash": serial,
        "power_on_hours": 26000,
        "read_errors_total": 0,
        "write_errors_total": 0,
        "temperature_c": 36,
        "read_errors_uncorrected": 0,
        "write_errors_uncorrected": 0,
        "smart_predict_fail": False,
        "smart_attrs": {**smart, **(attrs or {})},
    }


def _nvme(serial: str, spare: int = 100, media: int = 0) -> dict:
    return {
        "disk": NVME_MODEL,
        "media_type": "SSD",
        "bus_type": "NVMe",
        "serial_hash": serial,
        "wear_pct": 20,
        "power_on_hours": 9000,
        "read_errors_total": 0,
        "write_errors_total": 0,
        "temperature_c": 40,
        "nvme_critical_warning": 1 if spare < 10 else 0,  # bit 0: spare below threshold
        "nvme_spare_pct": spare,
        "nvme_spare_threshold_pct": 10,
        "nvme_percentage_used": 20,
        "nvme_media_errors": media,
        "nvme_unsafe_shutdowns": 4,
        "nvme_error_log_entries": 3 * media,
        "nvme_data_units_written": 9_000_000,
        "nvme_power_cycles": 900,
    }


def _inventory(disks: list) -> dict:
    """Get-PhysicalDisk view: the disks installed, whether or not SMART answers."""
    return {
        "hostname": "CALIB-INT-01",
        "manufacturer": "Dell Inc.",
        "model": "OptiPlex CALIB-INT",
        "chassis": "desktop",
        "os_caption": "Microsoft Windows 10 Pro",
        "os_build": "19045",
        "cpu_name": "Intel Core i5",
        "cpu_cores": 6,
        "cpu_logical": 12,
        "total_ram_gb": 16.0,
        "disks": [
            {"model": d["disk"], "media_type": d["media_type"], "serial_hash": d["serial_hash"]}
            for d in disks
        ],
        "driver_problem_count": 0,
        "pending_reboot": False,
    }


def _historical(storage: list) -> dict:
    return {
        "reliability_stability_index": 9.5,
        "kernel_power_41_30d": 0,
        "dirty_shutdowns_30d": 0,
        "bugchecks_30d": 0,
        "app_crashes_30d": 1,
        "whea_errors_30d": 0,
        "avg_boot_ms": 30000,
        "observation_days": 30,
        "storage": storage,
    }


def _heartbeat(cycle: int) -> dict:
    return {
        "cpu_pct": 8.0,
        "cpu_perf_pct": 99.0,
        "mem_avail_mb": 8500.0,
        "committed_pct": 40.0,
        "pagefile_pct": 7.0,
        "disk_read_sec": 0.004,
        "disk_write_sec": 0.003,
        "disk_queue": 0.1,
        "free_space_pct": 55.0,
        "handle_count_total": 42000,
        "nic_errors": 0,
        "user_present": True,
        "uptime_hours": 4.0 * cycle + 0.3,
    }


def _send(
    msg: str,
    payload: dict,
    at: datetime,
    sources: tuple = (),
    status: str = "ok",
    reason: Optional[str] = None,
) -> None:
    _Clock.t = at
    ts = at.isoformat()
    health = {src: SourceHealth(status=status, collected_at=ts, reason=reason) for src in sources}
    env = Envelope(
        device_id=DEVICE,
        msg_type=msg,
        ts=ts,
        payload=payload,
        agent_version="0.1.0",
        hostname="CALIB-INT-01",
        source_health=health,
    )
    pipeline.ingest_envelope(env)


def _run(
    cycles: int,
    storage: Callable[[int], list],
    installed: Callable[[int], list],
    events: Callable[[int], list] = lambda cycle: [],
) -> list:
    """One 4 h agent cycle each: inventory (when the installed disks change), heartbeat,
    historical, events, then ONE recompute. Returns every cycle's stored risk blob."""
    risks, listed = [], None
    for k in range(cycles):
        t = START + timedelta(hours=4 * k)
        if installed(k) != listed:
            listed = installed(k)
            _send("inventory", _inventory(listed), t - timedelta(seconds=10))
        _send("heartbeat", _heartbeat(k), t, _HB_SOURCES)
        _send("historical", _historical(storage(k)), t + timedelta(seconds=20), _HIST_SOURCES)
        if events(k):
            _send("events", {"events": events(k), "window_hours": 4.0}, t + timedelta(seconds=40))
        _Clock.t = t + timedelta(seconds=60)
        risks.append(pipeline.recompute_scores(DEVICE)["risk"])
    return risks


def _flags(health: dict) -> set:
    return set(health["damage"]["flags"]) | set(health["resilience"]["flags"])


def _labels(items: list) -> list:
    return [i.get("label") for i in items]


# --------------------------------------------------------------------------- #
# G4 (spec §8.2)
# --------------------------------------------------------------------------- #
def test_g4_nvme_below_spare_threshold_beats_hdd_with_one_198(sim) -> None:
    """An NVMe whose spare fell below the vendor threshold and whose media errors grow,
    next to an HDD with one offline-uncorrectable sector (listed first): h4 by the NVMe.
    The growth flags exist only on the NVMe's own series, so worst_disk_key has to rank
    by D+R like the engine -- by D alone it fetched the HDD's series and capped at h3."""
    hdd = _hdd("g4-hdd", {"198": 1})

    def storage(k: int) -> list:
        return [hdd, _nvme("g4-nvme", spare=100 if k < 6 else 9, media=max(0, k - 7))]

    risks = _run(18, storage, lambda k: storage(0))
    health = [r["health"] for r in risks]
    assert [h["state"] for h in health] == ["h3"] * 6 + ["h4"] * 12
    assert {h["worst_disk_id"] for h in health[:6]} == {"g4-hdd"}
    assert {h["worst_disk_id"] for h in health[6:]} == {"g4-nvme"}
    last = health[-1]
    assert {"cw_spare", "spare_consumed", "recurrence"} <= _flags(last)
    assert (last["dominant"], last["band"], last["horizon_days"]) == ("storage", "bad", 7)


@pytest.mark.parametrize(
    ("still_installed", "serial"), [(True, "b-hdd"), (False, "b-hdd"), (True, None)]
)
def test_g4_dying_data_disk_dropped_from_storage(
    sim, still_installed: bool, serial: Optional[str]
) -> None:
    """G4, second half: the agent drops a disk from its storage rows when it stops
    answering the reliability/SMART query. Still in the inventory (Get-PhysicalDisk) it
    is blind: the h4 hold is frozen, never reset or released, and its identity carried --
    a disk without a SerialNumber too, listed by its FriendlyName like its disk id.
    Gone from the inventory too, it is removed: the 48 h time release, still no reset."""
    nvme, hdd = _nvme("b-nvme"), _hdd(serial, {"197": 12, "198": 2})

    def installed(k: int) -> list:
        return [nvme, hdd] if still_installed or k < 4 else [nvme]

    risks = _run(22, lambda k: [nvme] + ([hdd] if k < 4 else []), installed)
    health = [r["health"] for r in risks]
    assert [h["state"] for h in health[:4]] == ["h4"] * 4  # pending > 10 and 198 > 0
    after = health[4:]  # 18 cycles = 72 h without the dying disk's row
    if still_installed:
        assert [h["state"] for h in after] == ["h4"] * 18
        assert {h["worst_disk_id"] for h in after} == {serial or HDD_MODEL}
        assert {h["below_h"] for h in after} == {None}  # frozen: the clock never runs
        assert all("SMART недоступен" in h["blind_spots"] for h in after)
        assert {h["action"] for h in after} == {"снять образ данных, планировать замену накопителя"}
    else:
        assert [h["state"] for h in after] == ["h4"] * 12 + ["h3"] * 6  # one level per 48 h
        note = "диск пропал из перечня SMART — снижение подтверждается по времени"
        assert note in _labels(after[0]["state_evidence"])


def test_same_model_replacement_resets_on_the_swap_cycle(sim) -> None:
    """A dying HDD with bad blocks in the event log is replaced by the same model (new
    serial_hash, clean counters; the inventory follows): the hold resets on the swap
    cycle itself and the identity follows the new disk. On a PC with ONE fixed disk the
    removed disk's own 153/129/7/51 leave the chain from the swap cycle on (X4, spec
    2026-11-02), so the new disk starts clean; a two-disk PC keeps the device-wide chain
    (the FN guard below)."""

    def storage(k: int) -> list:
        return [_hdd("int-old", {"197": 12, "198": 2}) if k < 8 else _hdd("int-new")]

    def events(k: int) -> list:
        ts = (START + timedelta(hours=4 * k - 1)).isoformat()
        bad_block = {**_BAD_BLOCK, "ts": ts, "message": "The device has a bad block."}
        return [bad_block] if 2 <= k < 8 else []

    risks = _run(10, storage, storage, events)
    assert [r["errchain"]["stage"] for r in risks[2:8]] == [2] * 6
    old, new = [r["health"] for r in risks[:8]], [r["health"] for r in risks[8:]]
    assert {h["state"] for h in old} == {"h4"}
    assert "chain_stage2" in _flags(old[-1])
    assert {h["worst_disk_id"] for h in new} == {"int-new"}
    # a reset, not the 48 h hold of the removed disk's h4
    assert new[0]["state"] != "h4" and new[0]["below_h"] is None
    # the swap cycle alone is a replacement, and the journal gets one auto label (plan 3a)
    swap = [False] * len(old) + [True] + [False] * (len(new) - 1)
    assert [h["disk_replaced"] for h in old + new] == swap
    labels = [o["outcome"] for o in db.get_health_outcomes(DEVICE)]
    assert labels.count("disk_replaced") == 1
    # X4: the swap cycle and the next one see the new disk alone
    assert [r["errchain"]["stage"] for r in risks[8:]] == [0, 0]
    assert [h["state"] for h in new] == ["h0", "h0"]


def test_replacing_the_other_disk_keeps_the_chain_h4(sim) -> None:
    """Spec §9.1 FN guard: errchain is device-wide and cannot tell which disk logged an
    event. A system NVMe without the IOCTL log fails on chain evidence alone (a paging
    error, then a BugCheck) next to a healthy old data HDD, which the engine names worst
    on its legacy points. Replacing the HDD resets the hold, but the failing NVMe is still
    installed: its damage -> BSOD chain keeps h4 on the swap cycle and after it."""
    nvme = {k: v for k, v in _nvme("fn-nvme").items() if not k.startswith("nvme_")}
    chain = {
        1: {"source": "disk", "event_id": 51, "level": "Warning"},  # paging error
        2: {"source": "Microsoft-Windows-WER-SystemErrorReporting", "event_id": 1001},
    }

    def storage(k: int) -> list:
        return [_hdd("fn-hdd" if k < 4 else "fn-hdd-new"), nvme]

    def events(k: int) -> list:
        ts = (START + timedelta(hours=4 * k - 1)).isoformat()
        return [{**chain[k], "log": "System", "ts": ts}] if k in chain else []

    risks = _run(8, storage, storage, events)
    for k, r in enumerate(risks[2:], start=2):
        health = r["health"]
        worst = "fn-hdd" if k < 4 else "fn-hdd-new"
        assert (health["state"], health["worst_disk_id"]) == ("h4", worst), k
        assert r["errchain"]["stage"] == 3 and "chain_stage3" in _flags(health), k


def test_offline_morning_pauses_the_release_clock(sim) -> None:
    """Spec H1 / F-01 (R3: D01 held h3 for weeks): a heartbeat scored before the
    morning's first historical sees storage_reliability STALE (no historical for > 6 h),
    the gate hides the axis -> 'offline'. The held level stays and the observed hours
    wait for the next seen cycle instead of restarting from zero every morning."""
    disk = _hdd("a-hdd")
    seed = _run(1, lambda k: [disk], lambda k: [disk])[0]
    seen = START + timedelta(seconds=60)
    # an earlier h3 left a hold on this very disk; 0 h observed below it so far
    seed["health"] = {
        **seed["health"],
        "state": "h3",
        "below_h": 0.0,
        "below_seen_at": seen.isoformat(),
    }
    db.store_scores(DEVICE, seen.isoformat(), {"risk": seed})

    def cycle(t: datetime) -> dict:
        _send("heartbeat", _heartbeat(1), t, _HB_SOURCES)
        _send("historical", _historical([disk]), t + timedelta(seconds=20), _HIST_SOURCES)
        _Clock.t = t + timedelta(seconds=60)
        return pipeline.recompute_scores(DEVICE)["risk"]["health"]

    evening = [cycle(START + timedelta(hours=4 * k)) for k in (1, 2, 3)]
    assert [(h["state"], h["below_h"]) for h in evening] == [("h3", 4.0), ("h3", 8.0), ("h3", 12.0)]

    morning = START + timedelta(hours=24)  # the agent was off for the night
    staleness.run_staleness_cycle(21600, now=morning - timedelta(hours=1))
    _send("heartbeat", _heartbeat(1), morning, _HB_SOURCES)
    _Clock.t = morning + timedelta(seconds=5)
    blind = pipeline.recompute_scores(DEVICE)["risk"]["health"]
    assert (blind["state"], blind["blind_cause"]) == ("h3", "offline")
    assert (blind["below_h"], blind["below_seen_at"]) == (12.0, None)  # paused, not reset

    _send("historical", _historical([disk]), morning + timedelta(seconds=20), _HIST_SOURCES)
    _Clock.t = morning + timedelta(seconds=60)
    back = pipeline.recompute_scores(DEVICE)["risk"]["health"]
    assert (back["state"], back["blind_cause"], back["below_h"]) == ("h3", None, 12.0)
    assert cycle(morning + timedelta(hours=4))["below_h"] == 16.0  # the clock runs again


@pytest.mark.parametrize(
    ("status", "cause", "below"), [("ok", "loss", None), ("empty", "unknown", 8.0)]
)
def test_disks_answering_nothing_is_a_loss_but_a_silent_script_is_not(
    sim, status: str, cause: str, below: Optional[float]
) -> None:
    """Spec H1, third loss rule through the real ingest: the historical script ran and the
    disks answered nothing (storage [], reliability ok) -> loss, the clock resets;
    PowerShell printed nothing (every historical source 'empty') -> unknown, the first
    episode pauses."""
    disk = _hdd("a-hdd")
    seed = _run(1, lambda k: [disk], lambda k: [disk])[0]
    seen = START + timedelta(seconds=60)
    seed["health"] = {
        **seed["health"],
        "state": "h3",
        "below_h": 0.0,
        "below_seen_at": seen.isoformat(),
    }
    db.store_scores(DEVICE, seen.isoformat(), {"risk": seed})
    for k in (1, 2):
        t = START + timedelta(hours=4 * k)
        _send("heartbeat", _heartbeat(k), t, _HB_SOURCES)
        _send("historical", _historical([disk]), t + timedelta(seconds=20), _HIST_SOURCES)
        _Clock.t = t + timedelta(seconds=60)
        held = pipeline.recompute_scores(DEVICE)["risk"]["health"]
    assert (held["state"], held["below_h"]) == ("h3", 8.0)

    t = START + timedelta(hours=12)
    _send("historical", _historical([]), t, _HIST_SOURCES, status=status)
    _Clock.t = t + timedelta(seconds=40)
    health = pipeline.recompute_scores(DEVICE)["risk"]["health"]
    assert (health["state"], health["blind_cause"], health["below_h"]) == ("h3", cause, below)


_CLEAN = {"events": [], "window_hours": 24.0}


@pytest.mark.parametrize(
    ("status", "reason", "payload", "live"),
    [
        ("empty", None, _CLEAN, True),
        ("empty", "rc_1", {}, False),  # PowerShell printed nothing: a failure (review 3d)
        ("timeout", None, {}, False),
    ],
)
def test_a_clean_journal_keeps_the_events_channel_live(
    sim, status: str, reason: Optional[str], payload: dict, live: bool
) -> None:
    """Spec H4 through the real ingest: the agent sends a clean event log as EMPTY --
    the channel is delivered; a failed run (no payload) with nothing counted is not."""
    disk = _hdd("a-hdd")
    _run(1, lambda k: [disk], lambda k: [disk])
    t = START + timedelta(hours=4)
    _send("events", payload, t, ("events",), status=status, reason=reason)
    _send("heartbeat", _heartbeat(1), t + timedelta(seconds=10), _HB_SOURCES)
    _Clock.t = t + timedelta(seconds=60)
    risk = pipeline.recompute_scores(DEVICE)["risk"]
    assert risk["errchain"]["delivered"] is live
    assert ("нет анализа событий" not in risk["health"]["blind_spots"]) is live


def test_a_phantom_disk_stores_a_low_confidence_storage_axis(sim) -> None:
    """H5 on the stored axis (review 3e): the fleet's phantom (R5) reports only wear /
    temperature / flush -- the device card must not read «высокая» while the verdict
    says low; a disk with its uncorrected pair is no phantom."""
    phantom = {
        "disk": "KCG NVME SSD 512GB",
        "media_type": "SSD",
        "bus_type": "NVMe",
        "serial_hash": "p1",
        "wear_pct": 1,
        "temperature_c": 44,
        "flush_latency_max_ms": 3,
    }
    axis = _run(1, lambda k: [phantom], lambda k: [phantom])[0]["score100"]["storage_risk"]
    assert axis["source_lineage"]["health_disk_ids"] == []
    assert axis["confidence"] == "low" and PHANTOM_MISSING in axis["missing_evidence"]


def test_a_disk_with_health_data_keeps_its_storage_confidence(sim) -> None:
    disk = _hdd("a-hdd")
    axis = _run(1, lambda k: [disk], lambda k: [disk])[0]["score100"]["storage_risk"]
    assert axis["source_lineage"]["health_disk_ids"] == ["a-hdd"]
    assert PHANTOM_MISSING not in (axis.get("missing_evidence") or [])


def test_an_isolated_ntfs55_is_a_file_system_h2_through_the_pipeline(sim) -> None:
    """Spec H6 end to end: clean SMART, one Ntfs 55 with nothing confirming it, a dirty
    shutdown in the historical -> h2 «fs_corruption», dominant os, cause power."""
    disk = _hdd("a-hdd")
    _run(1, lambda k: [disk], lambda k: [disk])
    t = START + timedelta(hours=4)
    ntfs = {
        "log": "System",
        "source": "Ntfs",
        "event_id": 55,
        "level": "Error",
        "ts": (t - timedelta(hours=2)).isoformat(),
    }
    _send("events", {"events": [ntfs], "window_hours": 24.0}, t, ("events",))
    _send("heartbeat", _heartbeat(1), t + timedelta(seconds=10), _HB_SOURCES)
    hist = {**_historical([disk]), "dirty_shutdowns_30d": 1}
    _send("historical", hist, t + timedelta(seconds=20), _HIST_SOURCES)
    _Clock.t = t + timedelta(seconds=60)
    risk = pipeline.recompute_scores(DEVICE)["risk"]
    assert risk["errchain"]["fs_isolated"] == 1 and risk["errchain"]["stage"] == 2
    health = risk["health"]
    assert (health["state"], health["dominant"], health["cause_class"]) == (
        "h2",
        "os",
        "power_likely",
    )


# --------------------------------------------------------------------------- #
# X4 (2026-11-02): after the only fixed disk is replaced, its predecessor's bad
# blocks no longer hold the new disk at h3/h4 for the rest of the 30-day window
# --------------------------------------------------------------------------- #
def _swap_storage(k: int) -> list:
    return [_hdd("int-old", {"197": 12, "198": 2}) if k < 8 else _hdd("int-new")]


def _bad_block_at(at: datetime) -> dict:
    return {**_BAD_BLOCK, "ts": at.isoformat(), "message": "The device has a bad block."}


def test_a_bad_block_logged_before_the_swap_and_sent_after_it_stays_cut(sim) -> None:
    """The old disk logged a bad block before shutdown, its events envelope lands after the
    swap recompute: the event's own (plausible) ts, not the receipt, puts it before the cut."""

    def events(k: int) -> list:
        if 2 <= k < 8:
            return [_bad_block_at(START + timedelta(hours=4 * k - 1))]
        if k == 9:  # 200 min before the swap cycle, received a cycle later
            return [_bad_block_at(START + timedelta(hours=4 * 8, minutes=-200))]
        return []

    risks = _run(12, _swap_storage, _swap_storage, events)
    assert [r["errchain"]["stage"] for r in risks[8:]] == [0] * 4
    assert {r["health"]["state"] for r in risks[8:]} == {"h0"}


def test_the_new_disks_own_bad_block_still_escalates(sim) -> None:
    def events(k: int) -> list:
        if 2 <= k < 8 or k == 12:
            return [_bad_block_at(START + timedelta(hours=4 * k - 1))]
        return []

    risks = _run(13, _swap_storage, _swap_storage, events)
    assert {r["health"]["state"] for r in risks[8:12]} == {"h0"}
    assert risks[12]["errchain"]["stage"] == 2
    assert risks[12]["health"]["state"] == "h3"


def test_a_former_two_disk_pc_keeps_the_survivors_chain(sim) -> None:
    """Review 2026-11-02 (X4 major): the F7g/F7h PC replaces its HDD, then the replacement
    leaves -- one fixed disk now. The NVMe that logged the paging error and the BugCheck
    reported from the start, so its own chain stays (no cut, stage 3)."""
    nvme = {k: v for k, v in _nvme("fn-nvme").items() if not k.startswith("nvme_")}
    chain = {
        1: {"source": "disk", "event_id": 51, "level": "Warning"},  # paging error
        2: {"source": "Microsoft-Windows-WER-SystemErrorReporting", "event_id": 1001},
    }

    def storage(k: int) -> list:
        if k < 6:
            return [_hdd("fn-hdd" if k < 4 else "fn-hdd-new"), nvme]
        return [nvme]

    def events(k: int) -> list:
        ts = (START + timedelta(hours=4 * k - 1)).isoformat()
        return [{**chain[k], "log": "System", "ts": ts}] if k in chain else []

    risks = _run(10, storage, storage, events)
    assert [r["errchain"]["stage"] for r in risks[6:]] == [3] * 4
    assert {r["health"]["raw_state"] for r in risks[6:]} == {"h4"}


def test_an_old_disk_put_back_keeps_its_own_history(sim) -> None:
    """Review 2026-11-02 (X4 minor): a reverted swap -- the returning disk reported from the
    start, so its own bad blocks are no predecessor's and stay in the chain."""

    def storage(k: int) -> list:
        return [_hdd("int-new")] if 8 <= k < 10 else [_hdd("int-old")]

    def events(k: int) -> list:
        return [_bad_block_at(START + timedelta(hours=4 * k - 1))] if 2 <= k < 8 else []

    risks = _run(14, storage, storage, events)
    assert [r["errchain"]["stage"] for r in risks[8:10]] == [0, 0]  # the new disk: cut
    assert [r["errchain"]["stage"] for r in risks[10:]] == [2] * 4  # the old one is back


def test_disk_since_needs_a_fixed_predecessor_and_a_whole_series(monkeypatch) -> None:
    """X4 guards: no predecessor (a freshly enrolled PC's first 24-h window), a USB stick as
    the only earlier key, a series cut by the retention cap, two disks, no serial: no cut."""
    sata, usb = {"bus_type": "SATA", "serial_hash": "x"}, {"bus_type": "USB", "serial_hash": "u"}
    new, old = "2026-10-02T00:00:00+00:00", "2026-09-01T00:00:00+00:00"

    def since(rows: list, disks: Optional[list] = None) -> Optional[datetime]:
        spans = [{"disk_key": k, "first": f, "truncated": t, "last": b} for k, f, t, b in rows]
        monkeypatch.setattr(pipeline.db, "get_disk_key_spans", lambda _device_id: spans)
        return pipeline._disk_since("d", {"disks": disks or [{"serial_hash": "new"}]})

    assert since([("new", new, False, sata), ("old", old, False, sata)]) == datetime(
        2026, 10, 2, tzinfo=timezone.utc
    )
    assert since([("new", new, False, sata)]) is None
    assert since([("new", new, False, sata), ("stick", old, False, usb)]) is None
    assert since([("new", new, True, sata), ("old", old, False, sata)]) is None
    assert since([("new", new, False, sata), ("twin", new, False, sata)]) is None  # same receipt
    later = "2026-10-20T00:00:00+00:00"
    assert since([("new", new, False, sata), ("added", later, False, sata)]) is None
    with_stick = [{"serial_hash": "new"}, {"serial_hash": "u", "bus_type": "USB"}]
    cut = since([("new", new, False, sata), ("old", old, False, sata)], with_stick)
    assert cut == datetime(2026, 10, 2, tzinfo=timezone.utc)  # a plugged stick is no 2nd disk
    assert (
        since([("new", new, False, sata), ("fallback", old, False, {"bus_type": "SATA"})]) is None
    )
    two = [{"serial_hash": "new"}, {"serial_hash": "old"}]
    assert since([("new", new, False, sata), ("old", old, False, sata)], two) is None
    assert since([("new", new, False, sata), ("old", old, False, sata)], [{"model": "X"}]) is None


def test_a_counter_zero_raid_phantom_stays_a_capped_phantom(sim) -> None:
    """S2 (test review): presence is untouched end to end -- a RAID row with only the
    counter's zeros stays a scored phantom that H5 caps, never UNKNOWN."""
    phantom = {
        "disk": "RAID Vol",
        "media_type": "Unspecified",
        "bus_type": "RAID",
        "serial_hash": "r1",
        "wear_pct": 0,
        "temperature_c": 0,
    }
    axis = _run(1, lambda k: [phantom], lambda k: [phantom])[0]["score100"]["storage_risk"]
    assert axis["source_lineage"]["health_disk_ids"] == []
    assert axis["confidence"] == "low"
    assert PHANTOM_MISSING in axis["missing_evidence"]
