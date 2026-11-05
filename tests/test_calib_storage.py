"""Health calibration T2 (spec 2026-10-09: D3(г), D4, D8, D9, D11, D12; contract C2).

Storage-side inputs of the literature ladder: genuine recent growth (a new
high-water mark within 14 days, the first reading seeds), damage tiers, the
SATA wear coordinate, the early-event floor, the legacy I/O totals rule, the
D+R worst disk shared by the engine and the series pick, the disk identity
lineage health uses, and the rulestats confirmation predicate.

No test here needs a trend: growth reads disk_series directly, so this file
does not depend on the parallel trends change (T1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from server.analytics import rulestats
from server.analytics.storage import compute_storage_risk, worst_disk_key

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
_SERIAL = "calibhash1"
_GROWTH_LABEL = "рост дефектов диска за последние 14 дней"


def _disk(**kw):
    base = {"disk": "PhysicalDisk0", "media_type": "SSD", "serial_hash": _SERIAL}
    base.update(kw)
    return base


def _hist(*disks):
    return {"storage": list(disks)}


def _row(days_ago, serial=_SERIAL, **fields):
    row = {"serial_hash": serial, "received_at": (_NOW - timedelta(days=days_ago)).isoformat()}
    row.update(fields)
    return row


def _db_order(rows):
    """Newest-first, exactly like db.get_disk_series hands the series over."""
    return sorted(rows, key=lambda r: r["received_at"], reverse=True)


def _coords(score):
    return score.source_lineage["coords"]


def _labels(score):
    return [f["label"] for f in score.factors]


def _flags_with_series(disk, rows):
    s = compute_storage_risk(_hist(disk), None, disk_series=_db_order(rows))
    return _coords(s)["flags"]


# --------------------------------------------------------------------------- #
# Recent genuine growth (D4): a new high-water mark within 14 days
# --------------------------------------------------------------------------- #


def test_single_old_burst_is_not_growth():
    rows = [_row(d, nvme_media_errors=0) for d in range(40, 25, -1)]
    rows += [_row(d, nvme_media_errors=3) for d in range(25, -1, -1)]
    assert "recurrence" not in _flags_with_series(_disk(nvme_media_errors=3), rows)


def test_stable_count_from_first_reading_is_not_growth():
    rows = [_row(d, smart_attrs={"197": 12}) for d in range(30, -1, -1)]
    flags = _flags_with_series(_disk(smart_attrs={"197": 12}), rows)
    assert "recurrence" not in flags
    assert "pending_gt10" in flags


@pytest.mark.parametrize(
    "pending",
    [
        lambda d: 0 if d > 5 else d % 2,  # MX500 0<->1 toggle starting inside the window
        lambda d: 0 if d > 2 else 1,  # a single 0 -> 1 two days ago
    ],
    ids=["toggle_in_window", "single_step_to_1"],
)
def test_197_below_2_is_not_growth(pending):
    rows = [_row(d, smart_attrs={"197": pending(d)}) for d in range(30, -1, -1)]
    assert "recurrence" not in _flags_with_series(_disk(smart_attrs={"197": pending(0)}), rows)


def test_return_to_previous_maximum_is_not_growth():
    rows = [_row(d, smart_attrs={"197": v}) for d, v in ((20, 5), (16, 5), (12, 0), (8, 0), (0, 5))]
    assert "recurrence" not in _flags_with_series(_disk(smart_attrs={"197": 5}), rows)


def _counter(name, value):
    """One damage counter as the agent sends it: ATA attribute ids sit in smart_attrs."""
    return {"smart_attrs": {name: value}} if name.isdigit() else {name: value}


@pytest.mark.parametrize(
    "counter", ["5", "187", "198", "nvme_media_errors", "write_errors_uncorrected"]
)
def test_new_maximum_within_14_days_is_growth(counter):
    rows = [_row(d, **_counter(counter, 8)) for d in range(30, 3, -1)]
    rows += [_row(d, **_counter(counter, 9)) for d in range(3, -1, -1)]
    s = compute_storage_risk(
        _hist(_disk(**_counter(counter, 9))), None, disk_series=_db_order(rows)
    )
    coords = _coords(s)
    assert "recurrence" in coords["flags"]
    assert coords["resilience_loss"] == 30.0
    assert _GROWTH_LABEL in _labels(s)


# 12, not 10: the cut-off is inclusive, so a 10-day-old event also passes a 10-day window.
@pytest.mark.parametrize(("days_ago", "growth"), [(12, True), (16, False)])
def test_growth_window_is_14_days(days_ago, growth):
    rows = [_row(d, smart_attrs={"5": 8}) for d in range(30, days_ago, -1)]
    rows += [_row(d, smart_attrs={"5": 9}) for d in range(days_ago, -1, -1)]
    flags = _flags_with_series(_disk(smart_attrs={"5": 9}), rows)
    assert ("recurrence" in flags) is growth


def test_first_pending_pair_is_growth():
    rows = [_row(d, smart_attrs={"197": 0}) for d in range(30, 0, -1)]
    rows.append(_row(0, smart_attrs={"197": 2}))
    assert "recurrence" in _flags_with_series(_disk(smart_attrs={"197": 2}), rows)


def test_uncorrected_errors_growth_counts():
    # write_errors_uncorrected absent: the pair is summed when EITHER is present.
    rows = [_row(d, read_errors_uncorrected=0) for d in range(30, 2, -1)]
    rows += [_row(d, read_errors_uncorrected=3) for d in range(2, -1, -1)]
    disk = _disk(read_errors_uncorrected=3)
    assert "recurrence" in _flags_with_series(disk, rows)


def test_spare_new_low_within_14_days_sets_spare_consumed():
    spare = {"nvme_spare_threshold_pct": 10}
    rows = [_row(d, nvme_spare_pct=100, **spare) for d in range(22, 2, -1)]
    rows += [_row(d, nvme_spare_pct=8, **spare) for d in range(2, -1, -1)]
    s = compute_storage_risk(
        _hist(_disk(nvme_spare_pct=8, **spare)), None, disk_series=_db_order(rows)
    )
    flags = _coords(s)["flags"]
    assert "spare_consumed" in flags
    assert "spare_below_threshold" in flags
    # informational: zero delta, no coordinate -- the axis is the threshold rule alone
    assert {"label": "резерв NVMe расходуется: новый минимум за 14 дней", "delta": 0.0} in s.factors
    assert s.value == 70.0
    assert _coords(s)["resilience_loss"] == 70.0


@pytest.mark.parametrize(
    "before",
    [[], [_row(d, nvme_spare_pct=100) for d in range(40, 20, -1)]],
    ids=["stable", "stale_collapse"],  # stale: the 100 -> 8 minimum is 20 days old
)
def test_low_spare_without_recent_new_minimum_is_not_consumed(before):
    spare = {"nvme_spare_pct": 8, "nvme_spare_threshold_pct": 10}
    rows = before + [_row(d, **spare) for d in range(20, -1, -1)]
    flags = _flags_with_series(_disk(**spare), rows)
    assert "spare_consumed" not in flags
    assert "spare_below_threshold" in flags


# --------------------------------------------------------------------------- #
# Damage tiers (Backblaze buckets, MX500 197 toggles)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "points"), [(3, 30.0), (15, 30.0), (16, 40.0), (100, 40.0), (101, 50.0)]
)
def test_attr5_tiers(raw, points):
    s = compute_storage_risk(_hist(_disk(smart_attrs={"5": raw})), None)
    assert _coords(s)["damage"] == points


def test_attr187_is_d40():
    s = compute_storage_risk(_hist(_disk(smart_attrs={"187": 1})), None)
    assert _coords(s)["damage"] == 40.0
    assert s.value == 40.0


def test_attr196_not_double_counted_with_attr5():
    both = compute_storage_risk(_hist(_disk(smart_attrs={"5": 8, "196": 8})), None)
    alone = compute_storage_risk(_hist(_disk(smart_attrs={"196": 8})), None)
    zero5 = compute_storage_risk(_hist(_disk(smart_attrs={"5": 0, "196": 8})), None)
    assert _coords(both)["damage"] == 30.0
    assert _coords(alone)["damage"] == 25.0
    assert _coords(zero5)["damage"] == 25.0


def test_single_pending_sector_is_d30():
    one = compute_storage_risk(_hist(_disk(smart_attrs={"197": 1})), None)
    assert _coords(one)["damage"] == 30.0
    assert _coords(one)["flags"] == ["damage_present"]
    assert {"label": "1 сектор в ожидании переназначения", "delta": 30.0} in one.factors
    two = compute_storage_risk(_hist(_disk(smart_attrs={"197": 2})), None)
    assert _coords(two)["damage"] == 45.0


def test_sata_wear_feeds_damage_without_changing_value():
    s = compute_storage_risk(_hist(_disk(wear_pct=96.0)), None)
    assert _coords(s)["damage"] == 40.0
    assert s.value == 40.0
    assert [label for label in _labels(s) if "износ" in label] == ["износ SSD 96% (конец ресурса)"]

    aged = compute_storage_risk(_hist(_disk(wear_pct=96.0, power_on_hours=45000)), None)
    assert aged.value == 40.0 + 8.0  # legacy power-on band only: no bathtub +5
    assert _coords(aged)["damage"] == 40.0

    mid = compute_storage_risk(_hist(_disk(wear_pct=88.0)), None)
    assert (_coords(mid)["damage"], mid.value) == (25.0, 25.0)
    low = compute_storage_risk(_hist(_disk(wear_pct=75.0)), None)
    assert (_coords(low)["damage"], low.value) == (0.0, 12.0)  # the 70-tier stays untagged


def test_attr188_sets_cmd_timeouts():
    s = compute_storage_risk(_hist(_disk(smart_attrs={"188": 3})), None)
    coords = _coords(s)
    assert "cmd_timeouts" in coords["flags"]
    assert coords["resilience_loss"] == 20.0


# --------------------------------------------------------------------------- #
# Event floor (D9) and the legacy I/O totals rule (D11)
# --------------------------------------------------------------------------- #


def _chain(early):
    return SimpleNamespace(stage=1, burstiness=None, counts={"early": early, "damage": 0})


def test_single_early_event_sets_no_flag():
    disk = _disk(nvme_media_errors=1)
    one = compute_storage_risk(_hist(disk), None, chain=_chain(1))
    two = compute_storage_risk(_hist(disk), None, chain=_chain(2))
    assert "early_events" not in _coords(one)["flags"]
    assert "early_events" in _coords(two)["flags"]


def test_legacy_totals_ignored_when_uncorrected_split_present():
    totals = {"read_errors_total": 4000, "write_errors_total": 1000}
    split = compute_storage_risk(
        _hist(_disk(read_errors_uncorrected=0, write_errors_uncorrected=0, **totals)), None
    )
    assert not any("накопленных I/O ошибок" in label for label in _labels(split))
    assert split.value == 0.0
    half = compute_storage_risk(_hist(_disk(read_errors_uncorrected=0, **totals)), None)
    assert half.value == 0.0
    legacy = compute_storage_risk(_hist(_disk(**totals)), None)
    assert {"label": "5000 накопленных I/O ошибок", "delta": 60.0} in legacy.factors


# --------------------------------------------------------------------------- #
# Worst disk = max (D+R, value) in the engine, max D+R in worst_disk_key (D8)
# --------------------------------------------------------------------------- #

_NVME = {
    "disk": "Samsung SSD 980",
    "media_type": "SSD",
    "serial_hash": "nv",
    "nvme_spare_pct": 8,
    "nvme_spare_threshold_pct": 10,
    "nvme_critical_warning": 1,
    "nvme_media_errors": 9,
}
_HDD = {"disk": "WDC WD10EZEX", "media_type": "HDD", "serial_hash": "hd", "smart_attrs": {"198": 1}}


def test_damaged_disk_not_masked_by_legacy_sibling():
    hdd = {
        "disk": "WDC WD10EZEX",
        "media_type": "HDD",
        "serial_hash": "hdd1",
        "read_errors_total": 5000,
        "temperature_c": 62,
    }
    ssd = {"disk": "Samsung SSD 870", "serial_hash": "ssd1", "smart_attrs": {"198": 1}}
    s = compute_storage_risk(_hist(hdd, ssd), None)
    assert "uncorrectable_198" in _coords(s)["flags"]
    assert s.source_lineage["worst_disk"] == "Samsung SSD 870"


def test_worst_disk_key_ranks_by_damage_plus_resilience():
    assert worst_disk_key(_hist(_HDD, _NVME)) == "nv"


def test_engine_and_series_pick_the_same_disk():
    hist = _hist(_HDD, _NVME)
    series = {
        "nv": [_row(d, "nv", nvme_media_errors=5) for d in range(30, 3, -1)]
        + [_row(d, "nv", nvme_media_errors=9) for d in range(3, -1, -1)],
        "hd": [_row(d, "hd", smart_attrs={"198": 1}) for d in range(30, -1, -1)],
    }
    key = worst_disk_key(hist)  # the pipeline fetches this disk's series
    s = compute_storage_risk(hist, None, disk_series=_db_order(series[key]))
    assert s.source_lineage.get("worst_disk_id") == key == "nv"
    assert "recurrence" in _coords(s)["flags"]  # growth stays on the reported disk


# --------------------------------------------------------------------------- #
# Disk identity lineage (D3(г)); gated / no-SMART lineage unchanged (C2)
# --------------------------------------------------------------------------- #


def test_lineage_has_disk_identity():
    ssd = {"disk": "Samsung SSD 870", "serial_hash": "ssd1", "smart_attrs": {"198": 1}}
    # attributes gone (SMART script timeout) while the disk is still listed;
    # smart_predict_fail comes from another WMI class and does not count.
    hdd = {
        "disk": "WDC WD10EZEX",
        "serial_hash": "hdd1",
        "smart_attrs": {},
        "smart_predict_fail": False,
        "temperature_c": 40,
    }
    lineage = compute_storage_risk(_hist(hdd, ssd), None).source_lineage
    assert lineage["worst_disk"] == "Samsung SSD 870"
    assert lineage["worst_disk_id"] == "ssd1"
    assert lineage["disk_ids"] == ["hdd1", "WDC WD10EZEX", "ssd1", "Samsung SSD 870"]
    assert lineage["attr_disk_ids"] == ["ssd1"]


def test_lineage_ids_fall_back_to_name_count_nvme_log_and_skip_none():
    legacy = {"disk": "PhysicalDisk0", "wear_pct": 10.0}  # pre-Ф1: no serial_hash
    nvme = {"disk": "Samsung SSD 980", "serial_hash": "nv", "nvme_media_errors": 0}
    anonymous = {"temperature_c": 30}  # neither id: must not put None in any id list
    lineage = compute_storage_risk(_hist(legacy, nvme, anonymous), None).source_lineage
    assert lineage["worst_disk_id"] == "PhysicalDisk0"  # all tie at (0, 0): first disk stays
    assert lineage["disk_ids"] == ["PhysicalDisk0", "nv", "Samsung SSD 980"]
    assert lineage["attr_disk_ids"] == ["nv"]
    assert lineage["disks_with_smart"] == 3


def test_lineage_counts_smart_disks_apart_from_listed_ones():
    # A listed disk with no SMART reading counts in disks_total only.
    hist = _hist({"disk": "USB Flash"}, _disk(smart_attrs={"198": 1}))
    lineage = compute_storage_risk(hist, None).source_lineage
    assert (lineage["disks_with_smart"], lineage["disks_total"]) == (1, 2)


def test_gated_and_no_smart_lineage_unchanged():
    gated = compute_storage_risk(_hist(_disk(smart_attrs={"198": 1})), None, domain_state="unknown")
    assert gated.value is None
    assert gated.source_lineage == {"storage_domain": "unknown"}
    blind = compute_storage_risk(_hist({"disk": "PhysicalDisk0", "smart_attrs": {}}), None)
    assert blind.value is None
    assert blind.source_lineage == {}


# --------------------------------------------------------------------------- #
# rulestats (D12, F11): confirmation must be independent of the flag's counter
# --------------------------------------------------------------------------- #


def _score_row(ts, flags=(), band="good"):
    storage = {"band": band, "coords": {"flags": list(flags)}}
    return {"ts": ts.isoformat(), "risk": {"score100": {"storage_risk": storage}}}


def test_rulestats_band_bad_alone_does_not_confirm():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _score_row(t0, flags=["pending_gt10"], band="bad"),
        _score_row(t0 + timedelta(days=1), band="bad"),  # closes the run; end_ts = t0
        _score_row(t0 + timedelta(days=10), band="bad"),  # the flag's own counter, still bad
    ]
    outcomes = rulestats.scan_device(rows, now=t0 + timedelta(days=61))
    assert outcomes == [("pending_high", "refuted", t0.isoformat())]


@pytest.mark.parametrize("flag", ["predict_fail", "cw_readonly", "cw_reliability", "chain_stage3"])
def test_rulestats_predict_fail_confirms(flag):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _score_row(t0, flags=["pending_gt10"]),
        _score_row(t0 + timedelta(days=1)),  # closes the run; end_ts = t0
        _score_row(t0 + timedelta(days=10), flags=[flag]),
    ]
    outcomes = rulestats.scan_device(rows, now=t0 + timedelta(days=20))
    assert outcomes == [("pending_high", "confirmed", t0.isoformat())]


def test_recurrence_rule_label_names_growth():
    assert rulestats.RULE_LABELS["media_recurrence"] == "рост дефектов диска"
