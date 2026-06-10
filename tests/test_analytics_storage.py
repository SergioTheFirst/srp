"""W4.2 storage health engine: deterministic SMART verdict + latency confirmation.

The spec (cctodo W4.2): SMART / StorageReliabilityCounter is the *leading* signal;
disk latency is only a *confirmation* of an existing SMART problem, never a
standalone risk (causal confounding: Defender / OneDrive / BitLocker / low-RAM /
thermal all raise latency without the drive failing). Gating mirrors W0.5/W4.1:
untrusted identity withholds; no SMART data -> UNKNOWN (never a confident zero).
"""

from __future__ import annotations

from server.analytics.storage import compute_storage_risk


def _disk(**kw):
    base = {"disk": "PhysicalDisk0", "media_type": "SSD"}
    base.update(kw)
    return base


def _hist(disks):
    return {"storage": disks}


def test_no_storage_data_is_unknown():
    s = compute_storage_risk({"storage": []}, None)
    assert s.value is None
    assert s.confidence == "unknown"


def test_disk_without_any_smart_field_is_unknown():
    # a row exists but carries no SMART attribute at all -> we know nothing.
    s = compute_storage_risk(_hist([_disk()]), None)
    assert s.value is None
    assert s.confidence == "unknown"


def test_healthy_ssd_low_risk():
    s = compute_storage_risk(
        _hist([_disk(wear_pct=5.0, read_errors_total=0, write_errors_total=0)]), None
    )
    assert s.value is not None
    assert s.value < 20
    assert s.confidence in ("high", "medium")


def test_reallocated_sectors_drive_high_risk():
    s = compute_storage_risk(_hist([_disk(media_type="HDD", reallocated_sectors=150)]), None)
    assert s.value is not None and s.value >= 60
    assert s.direction == "higher_is_worse"


def test_io_errors_high_risk():
    s = compute_storage_risk(_hist([_disk(read_errors_total=5, write_errors_total=2)]), None)
    assert s.value is not None and s.value >= 40


def test_high_latency_alone_does_not_raise_risk():
    """The key W4.2 rule: latency without a SMART signal is confounded noise."""
    hb = {"disk_read_sec": 0.2, "disk_write_sec": 0.2}  # 200 ms, very high
    s = compute_storage_risk(
        _hist([_disk(wear_pct=3.0, read_errors_total=0, write_errors_total=0)]), hb
    )
    # Strict: latency must contribute EXACTLY zero when SMART is clean (not just "low").
    assert s.value == 0.0


def test_latency_confirms_existing_smart_signal():
    hb = {"disk_read_sec": 0.2}
    without = compute_storage_risk(_hist([_disk(read_errors_total=5)]), None)
    with_lat = compute_storage_risk(_hist([_disk(read_errors_total=5)]), hb)
    # confirmation may only ADD when SMART already flags a problem.
    assert with_lat.value >= without.value


def test_high_wear_ssd_moderate_risk():
    s = compute_storage_risk(_hist([_disk(wear_pct=96.0)]), None)
    assert s.value is not None and s.value >= 25


def test_temperature_raises_risk():
    hot = compute_storage_risk(_hist([_disk(wear_pct=10.0, temperature_c=75)]), None)
    cool = compute_storage_risk(_hist([_disk(wear_pct=10.0, temperature_c=35)]), None)
    assert hot.value >= cool.value


def test_worst_disk_drives_the_score():
    disks = [_disk(disk="ok", wear_pct=2.0), _disk(disk="dying", reallocated_sectors=300)]
    s = compute_storage_risk(_hist(disks), None)
    assert s.value is not None and s.value >= 60


def test_untrusted_device_withholds():
    s = compute_storage_risk(
        _hist([_disk(reallocated_sectors=200)]), None, device_trust="untrusted"
    )
    assert s.value is None
    assert s.confidence == "unknown"


def test_graded_bands_and_high_power_on_hours():
    # mid-wear (85-95), warm temp (60-70), very high power-on hours, I/O errors >100.
    s = compute_storage_risk(
        _hist(
            [_disk(wear_pct=88.0, temperature_c=65, power_on_hours=45000, read_errors_total=150)]
        ),
        None,
    )
    assert s.value is not None and s.value >= 60  # >100 I/O errors dominate
    labels = " ".join(f["label"].lower() for f in s.factors)
    assert "износ" in labels and "power-on" in labels


def test_power_on_hours_mid_band():
    s = compute_storage_risk(_hist([_disk(wear_pct=2.0, power_on_hours=30000)]), None)
    assert s.value is not None and s.value > 0  # ~4 from the 25k-40k power-on band


def test_factors_explain_the_verdict():
    s = compute_storage_risk(_hist([_disk(reallocated_sectors=300, read_errors_total=4)]), None)
    assert s.factors  # non-empty, explainable
    labels = " ".join(f["label"].lower() for f in s.factors)
    assert "переназначенных" in labels or "сектор" in labels
