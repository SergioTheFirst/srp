"""Phase 1: stable network-device identity (chassis > serial > mac > ip).

The identity must survive a DHCP IP change (same MAC -> same id) so the topology
never reads a lease renewal as "device disappeared + new device appeared". A
device with no usable identifier collapses to ``nd-unknown`` (UNKNOWN over a
guessed id), exactly the precedence the printer identity already uses.
"""

from __future__ import annotations

from server.netdisco.identity import device_nid, merge_identity


def test_mac_is_normalised_into_the_id() -> None:
    assert device_nid(mac="aa:bb:cc:dd:ee:ff") == "nd-mac-AA-BB-CC-DD-EE-FF"


def test_ip_only_falls_back_to_ip_scheme() -> None:
    assert device_nid(ip="192.168.1.5") == "nd-ip-192.168.1.5"


def test_chassis_id_outranks_mac_and_ip() -> None:
    nid = device_nid(chassis_id="SwitchChassis01", mac="aa:bb:cc:dd:ee:ff", ip="10.0.0.1")
    assert nid == "nd-chassis-SWITCHCHASSIS01"


def test_serial_outranks_mac_and_is_slugified() -> None:
    assert device_nid(serial="SN-123/456", mac="aa:bb:cc:dd:ee:ff") == "nd-sn-SN-123-456"


def test_same_mac_different_ip_keeps_one_identity() -> None:
    a = device_nid(mac="aa:bb:cc:dd:ee:ff", ip="10.0.0.1")
    b = device_nid(mac="aa:bb:cc:dd:ee:ff", ip="10.0.0.99")
    assert a == b == "nd-mac-AA-BB-CC-DD-EE-FF"


def test_no_usable_identifier_is_unknown() -> None:
    assert device_nid() == "nd-unknown"
    assert device_nid(mac="not-a-mac") == "nd-unknown"  # not 12 hex -> normalize_mac None
    assert device_nid(chassis_id="   ") == "nd-unknown"  # blank token skipped
    assert device_nid(serial="!!!") == "nd-unknown"  # slug empties out


def test_merge_identity_promotes_to_the_stronger_scheme() -> None:
    assert (
        merge_identity("nd-ip-10.0.0.1", "nd-mac-AA-BB-CC-DD-EE-FF") == "nd-mac-AA-BB-CC-DD-EE-FF"
    )
    assert merge_identity("nd-mac-AA-BB-CC-DD-EE-FF", "nd-chassis-X") == "nd-chassis-X"


def test_merge_identity_keeps_the_stronger_existing_id() -> None:
    # A transient weaker observation must not demote a known device.
    assert (
        merge_identity("nd-mac-AA-BB-CC-DD-EE-FF", "nd-ip-10.0.0.1") == "nd-mac-AA-BB-CC-DD-EE-FF"
    )


def test_merge_identity_is_stable_at_equal_strength() -> None:
    # Same strength -> keep the old id (no churn / no record migration).
    assert merge_identity("nd-mac-AA-BB-CC-DD-EE-FF", "nd-mac-11-22-33-44-55-66") == (
        "nd-mac-AA-BB-CC-DD-EE-FF"
    )


def test_unparseable_ip_fallback_collapses_to_unknown() -> None:
    # nid may become a graph/DB key downstream: a non-IP string must never be
    # embedded verbatim (review MEDIUM). Only a syntactically valid address keys.
    assert device_nid(ip="../../etc/passwd") == "nd-unknown"
    assert device_nid(ip="not.an.ip") == "nd-unknown"
    assert device_nid(ip="999.999.999.999") == "nd-unknown"
    assert device_nid(ip="10.0.0.5") == "nd-ip-10.0.0.5"  # valid still keys


def test_merge_identity_keeps_old_when_new_scheme_is_unrecognised() -> None:
    # A corrupted / future-scheme nid maps to strength 0 -> never demotes a known
    # device (UNKNOWN over false confidence).
    assert merge_identity("nd-mac-AA-BB-CC-DD-EE-FF", "garbage") == "nd-mac-AA-BB-CC-DD-EE-FF"
