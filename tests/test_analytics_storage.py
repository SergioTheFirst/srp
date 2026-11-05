"""W4.2 storage health engine: deterministic SMART verdict + latency confirmation.

The spec (cctodo W4.2): SMART / StorageReliabilityCounter is the *leading* signal;
disk latency is only a *confirmation* of an existing SMART problem, never a
standalone risk (causal confounding: Defender / OneDrive / BitLocker / low-RAM /
thermal all raise latency without the drive failing). Gating mirrors W0.5/W4.1:
untrusted identity withholds; no SMART data -> UNKNOWN (never a confident zero).
"""

from __future__ import annotations

import pytest
from server.analytics.disk_bus import disk_reading, fixed_disks, is_removable_disk
from server.analytics.storage import compute_storage_risk


@pytest.mark.parametrize(
    "disk",
    [{"bus_type": "USB"}, {"bus_type": "usb"}, {"bus_type": "1394"}, {"interface": "USB"}],
)
def test_removable_disk_is_only_a_positively_identified_hot_plug_bus(disk):
    assert is_removable_disk(disk)


@pytest.mark.parametrize(
    "disk",
    # SD/MMC stay counted: built-in eMMC system disks enumerate on those buses.
    [{"bus_type": "SD"}, {"bus_type": "MMC"}, {"bus_type": "NVMe"}, {"bus_type": "SATA"}]
    + [{"bus_type": None}, {}, None, "USB"],
)
def test_unknown_or_fixed_bus_counts_as_before(disk):
    assert not is_removable_disk(disk)


def test_fixed_disks_drops_removable_keeps_order_and_old_agent_rows():
    nvme, usb, legacy = {"bus_type": "NVMe"}, {"bus_type": "USB"}, {"disk": "pre-bus agent"}
    assert fixed_disks([usb, nvme, None, legacy]) == [nvme, legacy]
    assert fixed_disks(None) == []


def test_disk_reading_reads_the_counters_unpopulated_zero_as_none():
    """S2: MSFT_StorageReliabilityCounter's 0 wear / 0 degC means "not populated"."""
    raw = {"bus_type": "RAID", "wear_pct": 0, "temperature_c": 0.0, "power_on_hours": 0}
    assert disk_reading(raw) == {**raw, "wear_pct": None, "temperature_c": None}
    assert raw["wear_pct"] == 0  # the stored row is not mutated
    assert disk_reading({"wear_pct": "0", "temperature_c": "41"})["wear_pct"] is None
    real = {"wear_pct": 3, "temperature_c": 41, "nvme_percentage_used": 0}
    assert disk_reading(real) == real


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


def test_no_health_data_never_claims_smart_ok():
    """S2: «SMART в норме» needs health data (H5) -- a temperature alone proves nothing."""
    ghost = compute_storage_risk(_hist([_disk(serial_hash="p", temperature_c=41)]), None)
    assert ghost.value == 0.0
    assert ghost.reason == ""
    real = compute_storage_risk(_hist([_disk(temperature_c=41, smart_attrs={"5": 0})]), None)
    assert real.reason == "SMART в норме на всех отчитывающихся дисках"


def test_counter_zero_only_disk_stays_a_scored_phantom():
    """S2 keeps engine presence on purpose: H5 caps the phantom and H1 "lost" is not tripped
    (presence-as-H5 sent 21 NVMe 0.3.x PCs to UNKNOWN in the 2026-11-02 replay)."""
    disk = _disk(bus_type="RAID", serial_hash="r", wear_pct=0, temperature_c=0)
    s = compute_storage_risk(_hist([disk]), None)
    assert s.value == 0.0
    assert s.source_lineage["health_disk_ids"] == []


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
    hb = {"disk_read_sec": 2.0, "disk_write_sec": 2.0}  # a 2 s I/O stall (H9)
    s = compute_storage_risk(
        _hist([_disk(wear_pct=3.0, read_errors_total=0, write_errors_total=0)]), hb
    )
    # Strict: latency must contribute EXACTLY zero when SMART is clean (not just "low").
    assert s.value == 0.0


def test_latency_confirms_existing_smart_signal():
    hb = {"disk_read_sec": 2.0}  # a 2 s I/O stall (H9: whole seconds)
    without = compute_storage_risk(_hist([_disk(read_errors_total=5)]), None)
    with_lat = compute_storage_risk(_hist([_disk(read_errors_total=5)]), hb)
    # confirmation may only ADD when SMART already flags a problem.
    assert with_lat.value > without.value
    # H9: below a second never confirms (the agent cannot send it; 0.2 s was "high")
    sub = compute_storage_risk(_hist([_disk(read_errors_total=5)]), {"disk_read_sec": 0.2})
    assert sub.value == without.value


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
