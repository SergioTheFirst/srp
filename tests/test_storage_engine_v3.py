"""ssd3 Ф2: storage engine v3 -- coordinate-tagged (D/R) rules layered onto the
existing storage_risk axis. Every new rule keys off a field ssd3 Ф1 introduced,
so a payload with none of them (test_analytics_storage.py's fixtures) must
still produce byte-identical legacy values -- that module's whole suite is the
real backward-compatibility regression pin; this file adds the coordinate
pins and the new rules' own coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.analytics.storage import compute_storage_risk, worst_disk_key
from server.analytics.trends import compute_trends


def _disk(**kw):
    base = {"disk": "PhysicalDisk0", "media_type": "SSD", "serial_hash": "diskhash1"}
    base.update(kw)
    return base


def _hist(disks):
    return {"storage": disks}


def _coords(score):
    return score.source_lineage["coords"]


# --------------------------------------------------------------------------- #
# Backward compatibility (T2.4 regression pin): old-shaped payloads/calls
# --------------------------------------------------------------------------- #


def test_backward_compat_reallocated_sectors_value_unchanged():
    s = compute_storage_risk(_hist([_disk(media_type="HDD", reallocated_sectors=150)]), None)
    assert s.value == 60.0  # legacy-only hit, no ssd3 rule can fire (no new fields)


def test_backward_compat_no_new_params_matches_no_new_fields():
    old_disk = {"disk": "PhysicalDisk0", "media_type": "SSD", "wear_pct": 88.0, "temperature_c": 65}
    a = compute_storage_risk(_hist([old_disk]), None)
    b = compute_storage_risk(_hist([old_disk]), None, disk_series=None, chain=None, trends=None)
    assert a.value == b.value == 25.0 + 8.0  # wear (85-95 band) + warm-temp band, legacy-tagged


def test_old_payload_gets_zero_coordinates():
    # No ssd3 Ф1 fields at all -> legacy value present, but nothing to tag D/R with.
    s = compute_storage_risk(_hist([_disk(reallocated_sectors=300, read_errors_total=4)]), None)
    coords = _coords(s)
    assert coords["damage"] == 0.0
    assert coords["resilience_loss"] == 0.0
    assert coords["flags"] == []


# --------------------------------------------------------------------------- #
# _has_smart extension: an NVMe-only disk (new fields only) is not UNKNOWN
# --------------------------------------------------------------------------- #


def test_nvme_only_disk_is_not_unknown():
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=0, nvme_spare_pct=100)]), None)
    assert s.value is not None
    assert s.confidence != "unknown"


def test_smart_attrs_only_disk_is_not_unknown():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"5": 0})]), None)
    assert s.value is not None


# --------------------------------------------------------------------------- #
# Static D/R rules, one fixture each
# --------------------------------------------------------------------------- #


def test_smart_predict_fail_hard_flag():
    s = compute_storage_risk(_hist([_disk(smart_predict_fail=True)]), None)
    assert s.value == 70.0
    coords = _coords(s)
    assert coords["damage"] == 70.0
    assert "predict_fail" in coords["flags"]


def test_smart_predict_fail_false_does_not_fire():
    s = compute_storage_risk(_hist([_disk(smart_predict_fail=False, wear_pct=1.0)]), None)
    assert "predict_fail" not in _coords(s)["flags"]


def test_critical_warning_bit0_spare():
    s = compute_storage_risk(_hist([_disk(nvme_critical_warning=0b00001)]), None)
    coords = _coords(s)
    assert coords["resilience_loss"] == 70.0
    assert "cw_spare" in coords["flags"]


def test_critical_warning_bit1_temperature():
    s = compute_storage_risk(_hist([_disk(nvme_critical_warning=0b00010)]), None)
    assert _coords(s)["resilience_loss"] == 30.0


def test_critical_warning_bit2_reliability():
    s = compute_storage_risk(_hist([_disk(nvme_critical_warning=0b00100)]), None)
    coords = _coords(s)
    assert coords["damage"] == 70.0
    assert "cw_reliability" in coords["flags"]


def test_critical_warning_bit3_readonly():
    s = compute_storage_risk(_hist([_disk(nvme_critical_warning=0b01000)]), None)
    coords = _coords(s)
    assert coords["damage"] == 80.0
    assert "cw_readonly" in coords["flags"]


def test_critical_warning_bit4_backup_failed():
    s = compute_storage_risk(_hist([_disk(nvme_critical_warning=0b10000)]), None)
    assert _coords(s)["damage"] == 40.0


def test_critical_warning_multiple_bits_stack():
    s = compute_storage_risk(_hist([_disk(nvme_critical_warning=0b00101)]), None)  # bit0 + bit2
    coords = _coords(s)
    assert coords["resilience_loss"] == 70.0
    assert coords["damage"] == 70.0


def test_attr197_pending_low_tier():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"197": 3})]), None)
    coords = _coords(s)
    assert coords["damage"] == 45.0
    assert "damage_present" in coords["flags"]
    assert "pending_gt10" not in coords["flags"]


def test_attr197_pending_hard_tier():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"197": 11})]), None)
    coords = _coords(s)
    assert coords["damage"] == 60.0
    assert "pending_gt10" in coords["flags"]


def test_attr198_uncorrectable():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"198": 1})]), None)
    coords = _coords(s)
    assert coords["damage"] == 60.0
    assert "uncorrectable_198" in coords["flags"]


def test_nvme_media_errors():
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=2)]), None)
    coords = _coords(s)
    assert coords["damage"] == 45.0
    assert "damage_present" in coords["flags"]


def test_spare_below_threshold():
    s = compute_storage_risk(_hist([_disk(nvme_spare_pct=5, nvme_spare_threshold_pct=10)]), None)
    coords = _coords(s)
    assert coords["resilience_loss"] == 70.0
    assert "spare_below_threshold" in coords["flags"]


def test_spare_above_threshold_does_not_fire():
    s = compute_storage_risk(_hist([_disk(nvme_spare_pct=50, nvme_spare_threshold_pct=10)]), None)
    assert "spare_below_threshold" not in _coords(s)["flags"]


def test_attr5_low_and_high_tier():
    low = compute_storage_risk(_hist([_disk(smart_attrs={"5": 3})]), None)
    high = compute_storage_risk(_hist([_disk(smart_attrs={"5": 101})]), None)
    assert _coords(low)["damage"] == 30.0
    assert _coords(high)["damage"] == 50.0


def test_attr187_reported_uncorrectable():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"187": 1})]), None)
    assert _coords(s)["damage"] == 35.0


def test_attr188_command_timeouts():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"188": 1})]), None)
    assert _coords(s)["resilience_loss"] == 20.0


def test_uncorrected_read_write_errors():
    s = compute_storage_risk(
        _hist([_disk(read_errors_uncorrected=1, write_errors_uncorrected=0)]), None
    )
    assert _coords(s)["damage"] == 40.0


def test_attr196_reallocation_events():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"196": 2})]), None)
    assert _coords(s)["damage"] == 25.0


def test_nvme_percentage_used_high_tier():
    s = compute_storage_risk(_hist([_disk(nvme_percentage_used=96)]), None)
    assert _coords(s)["damage"] == 40.0


def test_nvme_percentage_used_mid_tier():
    s = compute_storage_risk(_hist([_disk(nvme_percentage_used=90)]), None)
    assert _coords(s)["damage"] == 25.0


def test_wear_pct_alone_does_not_trigger_new_wear_rule():
    """Backward-compat gate: the ssd3 wear rule requires nvme_percentage_used
    (a Ф1-only field) so a pre-Ф1 payload's wear_pct never double-fires
    against the legacy wear rule (T2.4 pin)."""
    s = compute_storage_risk(_hist([_disk(wear_pct=96.0)]), None)
    assert _coords(s)["damage"] == 0.0
    assert s.value == 40.0  # legacy wear rule only


def test_wear_rule_takes_max_of_wear_pct_and_percentage_used():
    s = compute_storage_risk(_hist([_disk(wear_pct=97.0, nvme_percentage_used=86)]), None)
    assert _coords(s)["damage"] == 40.0  # max(97, 86) = 97 -> >95 tier


def test_unsafe_shutdowns_high_with_live_defects():
    s = compute_storage_risk(_hist([_disk(nvme_unsafe_shutdowns=20, nvme_media_errors=1)]), None)
    coords = _coords(s)
    assert coords["damage"] == 45.0  # media_errors
    assert coords["resilience_loss"] == 10.0  # unsafe_shutdowns, conditional on media_errors>0


def test_unsafe_shutdowns_high_without_defects_does_not_fire():
    # nvme_spare_pct present so the disk still counts as "has SMART data"
    # (a real Tier-B read populates the whole log page, incl. spare_pct).
    s = compute_storage_risk(_hist([_disk(nvme_unsafe_shutdowns=20, nvme_spare_pct=100)]), None)
    assert _coords(s)["resilience_loss"] == 0.0


def test_bathtub_needs_another_ssd3_factor_first():
    # power_on_hours alone (legacy-only) must NOT trigger the ssd3 bathtub add-on.
    s = compute_storage_risk(_hist([_disk(power_on_hours=45000)]), None)
    assert _coords(s)["damage"] == 0.0


def test_bathtub_fires_once_another_factor_present():
    s = compute_storage_risk(_hist([_disk(power_on_hours=45000, nvme_media_errors=1)]), None)
    coords = _coords(s)
    assert coords["damage"] == 45.0 + 5.0  # media_errors + bathtub context


# --------------------------------------------------------------------------- #
# Coordinate pins (K2/K4): pure-D and pure-R fixtures
# --------------------------------------------------------------------------- #


def test_pure_d_fixture_has_zero_resilience_loss():
    """Static damage levels with no series/trends/chain -> R stays untouched (K4)."""
    s = compute_storage_risk(_hist([_disk(smart_predict_fail=True, smart_attrs={"198": 2})]), None)
    coords = _coords(s)
    assert coords["damage"] > 0
    assert coords["resilience_loss"] == 0.0


def test_pure_r_fixture_has_zero_damage():
    s = compute_storage_risk(
        _hist([_disk(nvme_critical_warning=0b00001, smart_attrs={"188": 5})]), None
    )
    coords = _coords(s)
    assert coords["resilience_loss"] > 0
    assert coords["damage"] == 0.0


def test_coordinate_sums_match_the_hits_that_fired():
    # Kept under the 100-clamp so the raw sum is directly checkable.
    s = compute_storage_risk(_hist([_disk(smart_attrs={"5": 5, "187": 1})]), None)
    coords = _coords(s)
    assert coords["damage"] == 30.0 + 35.0
    assert set(coords["flags"]) == {"damage_present"}  # 5 and 187 share this flag


def test_coordinate_damage_clamps_at_100_like_the_legacy_axis():
    s = compute_storage_risk(
        _hist([_disk(smart_attrs={"5": 5, "187": 1}, nvme_media_errors=1)]), None
    )
    coords = _coords(s)
    assert coords["damage"] == 100.0  # 30+35+45=110, clamped same as the legacy value
    assert s.value == 100.0


# --------------------------------------------------------------------------- #
# UNKNOWN / untrusted gates still hold under v3
# --------------------------------------------------------------------------- #


def test_no_storage_data_is_unknown_v3():
    s = compute_storage_risk({"storage": []}, None)
    assert s.value is None


def test_untrusted_withholds_even_with_new_fields():
    s = compute_storage_risk(
        _hist([_disk(smart_predict_fail=True)]), None, device_trust="untrusted"
    )
    assert s.value is None
    assert s.confidence == "unknown"


# --------------------------------------------------------------------------- #
# worst_disk_key: pure, D-points of the latest reading only
# --------------------------------------------------------------------------- #


def test_worst_disk_key_picks_highest_damage():
    disks = [
        _disk(serial_hash="ok", wear_pct=1.0),
        _disk(serial_hash="bad", smart_predict_fail=True),
    ]
    assert worst_disk_key(_hist(disks)) == "bad"


def test_worst_disk_key_ignores_disks_without_serial_hash():
    disks = [{"disk": "no-key", "smart_predict_fail": True}, _disk(serial_hash="only-key")]
    assert worst_disk_key(_hist(disks)) == "only-key"


def test_worst_disk_key_none_when_no_disks():
    assert worst_disk_key({"storage": []}) is None
    assert worst_disk_key(None) is None


# --------------------------------------------------------------------------- #
# Dynamics: recurrence (disk_series), gated to the matching serial_hash
# --------------------------------------------------------------------------- #


def _series_row(days_ago: int, **fields):
    when = (datetime(2026, 7, 8, tzinfo=timezone.utc) - timedelta(days=days_ago)).isoformat()
    row = {"serial_hash": "diskhash1", "received_at": when}
    row.update(fields)
    return row


def test_recurrence_fires_when_gap_is_at_least_seven_days():
    series = [
        _series_row(10, nvme_media_errors=1),
        _series_row(2, nvme_media_errors=3),  # grew, >=7d after the older reading
    ]
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=3)]), None, disk_series=series)
    coords = _coords(s)
    assert "recurrence" in coords["flags"]
    assert coords["resilience_loss"] >= 30.0


def test_recurrence_does_not_fire_under_seven_days():
    series = [
        _series_row(5, nvme_media_errors=1),
        _series_row(1, nvme_media_errors=3),  # grew, but only 4 days apart
    ]
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=3)]), None, disk_series=series)
    assert "recurrence" not in _coords(s)["flags"]


def test_recurrence_ignored_for_a_different_disk_in_the_same_pass():
    series = [_series_row(10, nvme_media_errors=1), _series_row(2, nvme_media_errors=3)]
    other_disk = _disk(serial_hash="other-disk", nvme_media_errors=3)
    s = compute_storage_risk(_hist([other_disk]), None, disk_series=series)
    assert "recurrence" not in _coords(s)["flags"]


def test_disk_key_change_resets_recurrence():
    """A disk replacement mid-series must not be read as recurring damage."""
    series = [
        _series_row(20, nvme_media_errors=5),  # old disk, high errors
        _series_row(1, nvme_media_errors=0),  # NEW disk (lower reading is a legit reset)
    ]
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=0)]), None, disk_series=series)
    assert "recurrence" not in _coords(s)["flags"]  # errors only ever fell, never grew


# --------------------------------------------------------------------------- #
# Dynamics: acceleration (via compute_trends -> TrendResult.accelerating)
# --------------------------------------------------------------------------- #


def _accel_series(values):
    return [_series_row(len(values) - i, smart_attrs={"197": v}) for i, v in enumerate(values)]


def test_doubling_attr197_is_accelerating():
    series = _accel_series([1, 2, 4, 8, 16, 32])
    trends = compute_trends([], [], disk_series=series)
    assert trends["smart_pending"].accelerating is True

    s = compute_storage_risk(
        _hist([_disk(smart_attrs={"197": 32})]), None, disk_series=series, trends=trends
    )
    coords = _coords(s)
    assert "accel" in coords["flags"]
    assert coords["resilience_loss"] >= 40.0


def test_linear_attr197_growth_is_not_accelerating():
    series = _accel_series([1, 2, 3, 4, 5, 6])
    trends = compute_trends([], [], disk_series=series)
    assert trends["smart_pending"].accelerating is False

    s = compute_storage_risk(
        _hist([_disk(smart_attrs={"197": 6})]), None, disk_series=series, trends=trends
    )
    assert "accel" not in _coords(s)["flags"]


def test_fewer_than_six_points_accelerating_is_none():
    series = _accel_series([1, 2, 4, 8])
    trends = compute_trends([], [], disk_series=series)
    assert trends["smart_pending"].accelerating is None
    assert trends["smart_pending"].slope_recent is None


# --------------------------------------------------------------------------- #
# Dynamics: remap-masking (attr 5 worsening while attr 197 improving)
# --------------------------------------------------------------------------- #


def test_remap_masking_fires_when_realloc_up_and_pending_down():
    up = [1, 2, 3, 4, 5, 6]  # attr5 worsening (rising)
    down = [6, 5, 4, 3, 2, 1]  # attr197 improving (falling)
    series = [_series_row(6 - i, smart_attrs={"5": up[i], "197": down[i]}) for i in range(6)]
    trends = compute_trends([], [], disk_series=series)
    assert trends["smart_realloc"].direction == "worsening"
    assert trends["smart_pending"].direction == "improving"

    s = compute_storage_risk(
        _hist([_disk(smart_attrs={"5": 6, "197": 1})]), None, disk_series=series, trends=trends
    )
    coords = _coords(s)
    assert "remap_masking" in coords["flags"]
    assert coords["resilience_loss"] >= 25.0


def test_remap_masking_absent_when_both_worsening():
    both_up = [1, 2, 3, 4, 5, 6]
    series = [
        _series_row(6 - i, smart_attrs={"5": both_up[i], "197": both_up[i]}) for i in range(6)
    ]
    trends = compute_trends([], [], disk_series=series)
    s = compute_storage_risk(
        _hist([_disk(smart_attrs={"5": 6, "197": 6})]), None, disk_series=series, trends=trends
    )
    assert "remap_masking" not in _coords(s)["flags"]


# --------------------------------------------------------------------------- #
# Synergy pairs (closed list) + multiplier policy (max-wins, not multiplied)
# --------------------------------------------------------------------------- #


class _FakeChain:
    def __init__(self, stage=0, burstiness=None, counts=None):
        self.stage = stage
        self.burstiness = burstiness
        self.counts = counts or {}


def test_synergy_pair_pending_and_chain_stage():
    chain = _FakeChain(stage=1)
    s = compute_storage_risk(_hist([_disk(smart_attrs={"197": 1})]), None, chain=chain)
    coords = _coords(s)
    assert "compensation_breach" in coords["flags"]
    assert coords["resilience_loss"] >= 25.0


def test_synergy_pair_absent_without_chain_stage():
    s = compute_storage_risk(_hist([_disk(smart_attrs={"197": 1})]), None, chain=None)
    assert "compensation_breach" not in _coords(s)["flags"]


def test_synergy_pair_spare_trend_and_percentage_used():
    declining = [90, 80, 70, 60, 50, 40]
    series = [_series_row(6 - i, nvme_spare_pct=declining[i]) for i in range(6)]
    trends = compute_trends([], [], disk_series=series)
    assert trends["nvme_spare"].direction == "worsening"
    s = compute_storage_risk(
        _hist([_disk(nvme_percentage_used=90, nvme_spare_pct=40)]),
        None,
        disk_series=series,
        trends=trends,
    )
    assert "compensation_breach" in _coords(s)["flags"]


def test_multiplier_policy_two_triggers_take_max_not_product():
    """chain-stage-2 (x1.25) + acceleration (x1.4) triggered together must
    apply ONLY the higher one (x1.4), never x1.25*x1.4 (T2.4: 'НЕ произведение')."""
    series = _accel_series([1, 2, 4, 8, 16, 32])
    trends = compute_trends([], [], disk_series=series)
    assert trends["smart_pending"].accelerating is True

    disk = _disk(smart_predict_fail=True)  # +70 D, flat axis base; no other rule fires
    chain = _FakeChain(stage=2)
    s = compute_storage_risk(_hist([disk]), None, disk_series=series, trends=trends, chain=chain)
    # base(70) clamped=70, x1.4 (max of 1.25 chain-stage2 / 1.4 accel) = 98, not x1.25*1.4=87.5
    assert s.value == 98.0


# --------------------------------------------------------------------------- #
# chain stage 2/3, burstiness, early_events (Ф3 not wired yet -> tested via a
# fake chain object duck-typed the same shape errchain.ErrChain will have)
# --------------------------------------------------------------------------- #


def test_chain_stage_3_is_a_hard_flat_addition_not_a_multiplier():
    chain = _FakeChain(stage=3)
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    coords = _coords(s)
    assert "chain_stage3" in coords["flags"]
    assert coords["resilience_loss"] >= 45.0
    assert s.value == 45.0 + 25.0  # media_errors(D45, axis+45) + stage3(axis+25) flat, no mult


def test_chain_stage_2_is_a_multiplier():
    chain = _FakeChain(stage=2)
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    assert s.value == 45.0 * 1.25
    assert "chain_stage2" in _coords(s)["flags"]


def test_burstiness_above_threshold_fires():
    chain = _FakeChain(burstiness=2.5)
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    assert _coords(s)["resilience_loss"] >= 10.0


def test_early_events_without_damage_events_fires():
    chain = _FakeChain(counts={"early": 2, "damage": 0})
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    assert "early_events" in _coords(s)["flags"]


def test_early_events_with_damage_events_does_not_fire():
    chain = _FakeChain(counts={"early": 2, "damage": 1})
    s = compute_storage_risk(_hist([_disk(nvme_media_errors=1)]), None, chain=chain)
    assert "early_events" not in _coords(s)["flags"]
