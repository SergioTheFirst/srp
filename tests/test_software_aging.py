"""ssd3 Ф4 (T4.3/T4.4): software-aging engine -- session-scoped leak detection.

K2 is the whole point of this module: a handle/memory leak is Resilience-loss,
never Damage -- a reboot returns the resource in full, so nothing irreversible
happened. Every fixture below is newest-first (the real db.get_recent_heartbeats
shape); ``_series()`` lets the test bodies write rows oldest-first for
readability and reverses them once.
"""

from __future__ import annotations

from server.analytics.software_aging import compute_software_aging_risk


def _series(rows_oldest_first):
    return list(reversed(rows_oldest_first))


def _row(uptime, handles=None, mem=None, pagefile=None):
    row: dict = {"uptime_hours": uptime}
    if handles is not None:
        row["handle_count_total"] = handles
    if mem is not None:
        row["mem_avail_mb"] = mem
    if pagefile is not None:
        row["pagefile_pct"] = pagefile
    return row


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #


def test_untrusted_withholds():
    rows = _series([_row(h, handles=1000 + h * 500) for h in range(6)])
    s = compute_software_aging_risk(rows, device_trust="untrusted")
    assert s.value is None
    assert s.band == "unknown" and s.confidence == "unknown"


def test_fewer_than_4_points_is_unknown():
    rows = _series([_row(h, handles=1000) for h in range(3)])
    s = compute_software_aging_risk(rows)
    assert s.value is None
    assert s.band == "unknown"


def test_missing_uptime_rows_do_not_count_toward_session():
    rows = _series([_row(0, handles=1000), {"handle_count_total": 9999}, _row(1, handles=1100)])
    # Only 2 rows carry uptime_hours -> current session has 2 points -> UNKNOWN.
    s = compute_software_aging_risk(rows)
    assert s.value is None


# --------------------------------------------------------------------------- #
# Linear leak -> factors + band
# --------------------------------------------------------------------------- #


def test_linear_severe_handle_leak_hits_watch_or_bad():
    # +400 handles/hour over a 5h session -> severe (>300/h).
    rows = _series([_row(h, handles=1000 + h * 400) for h in range(6)])
    s = compute_software_aging_risk(rows)
    assert s.value == 45.0
    assert s.band == "bad"
    assert "aging_leak" in s.source_lineage["coords"]["flags"]
    assert any("утечка дескрипторов" in f["label"] for f in s.factors)


def test_watch_band_handle_growth_below_severe():
    # +150 handles/hour -> watch tier (>100/h, <=300/h).
    rows = _series([_row(h, handles=1000 + h * 150) for h in range(6)])
    s = compute_software_aging_risk(rows)
    assert s.value == 25.0
    assert s.band == "watch"
    assert "aging_leak" in s.source_lineage["coords"]["flags"]


def test_falling_memory_hits_factor():
    rows = _series([_row(h, mem=8000 - h * 80) for h in range(6)])  # -80 MB/h
    s = compute_software_aging_risk(rows)
    assert s.value == 20.0
    assert any("свободной памяти" in f["label"] for f in s.factors)


def test_two_weeks_uptime_factor():
    rows = _series([_row(300 + h * 10, handles=1000) for h in range(6)])  # up to 350h
    s = compute_software_aging_risk(rows)
    assert any("без перезагрузки" in f["label"] for f in s.factors)


def test_pagefile_confirmation_factor():
    # pagefile p95 clearly higher in the session's second half.
    rows = _series(
        [_row(h, pagefile=10) for h in range(4)] + [_row(h, pagefile=60) for h in range(4, 8)]
    )
    s = compute_software_aging_risk(rows)
    assert any("pagefile" in f["label"] for f in s.factors)


def test_no_signal_is_healthy_zero():
    rows = _series([_row(h, handles=1000, mem=8000, pagefile=10) for h in range(6)])
    s = compute_software_aging_risk(rows)
    assert s.value == 0.0
    assert s.band == "good"
    assert s.source_lineage["coords"]["flags"] == []


# --------------------------------------------------------------------------- #
# Sessions: uptime reset cuts history, reboot healing, acceleration
# --------------------------------------------------------------------------- #


def test_uptime_reset_cuts_session_old_leak_does_not_bleed_through():
    session1 = [_row(h, handles=1000 + h * 400) for h in range(6)]  # severe leak, then reboot
    session2 = [_row(h, handles=2000) for h in range(6)]  # flat, healthy new session
    s = compute_software_aging_risk(_series(session1 + session2))
    assert s.value == 0.0  # only the CURRENT session is judged
    assert s.source_lineage["sessions_seen"] == 2
    assert s.source_lineage["session_points"] == 6


def test_reboot_restores_flag_when_new_session_starts_healthier():
    # Session 1 leaks handles up to 3000, then a reboot; session 2 starts at
    # 1000 (well below session 1's last value) and stays flat.
    session1 = [_row(h, handles=1000 + h * 400) for h in range(6)]
    session2 = [_row(h, handles=1000) for h in range(6)]
    s = compute_software_aging_risk(_series(session1 + session2))
    assert "reboot_restores" in s.source_lineage["coords"]["flags"]
    assert any(
        f["label"] == "перезагрузка возвращает ресурс — утечка программная" and f["delta"] == 0.0
        for f in s.factors
    )


def test_reboot_restores_absent_when_no_previous_leak():
    session1 = [_row(h, handles=1000) for h in range(6)]  # flat, no leak
    session2 = [_row(h, handles=900) for h in range(6)]
    s = compute_software_aging_risk(_series(session1 + session2))
    assert "reboot_restores" not in s.source_lineage["coords"]["flags"]


def test_reboot_restores_flag_via_memory_leak_too():
    # Session 1 bleeds free memory down to ~7500 MB, then a reboot; session 2
    # starts back up near 8000 MB (healthier) and stays flat.
    session1 = [_row(h, mem=8000 - h * 100) for h in range(6)]  # -100 MB/h leak
    session2 = [_row(h, mem=8000) for h in range(6)]
    s = compute_software_aging_risk(_series(session1 + session2))
    assert "reboot_restores" in s.source_lineage["coords"]["flags"]


def test_acceleration_flag_when_current_session_leaks_twice_as_fast():
    session1 = [_row(h, handles=1000 + h * 50) for h in range(6)]  # 50/h, sub-watch
    session2 = [_row(h, handles=1000 + h * 150) for h in range(6)]  # 150/h >= 2x
    s = compute_software_aging_risk(_series(session1 + session2))
    assert "aging_accelerating" in s.source_lineage["coords"]["flags"]
    assert any("ускоряется" in f["label"] for f in s.factors)


def test_acceleration_absent_when_previous_session_was_flat():
    session1 = [_row(h, handles=1000) for h in range(6)]  # 0/h
    session2 = [_row(h, handles=1000 + h * 150) for h in range(6)]  # 150/h, still watch
    s = compute_software_aging_risk(_series(session1 + session2))
    assert "aging_accelerating" not in s.source_lineage["coords"]["flags"]


# --------------------------------------------------------------------------- #
# K2 structural pin
# --------------------------------------------------------------------------- #


def test_k2_pin_coords_carry_only_flags_never_damage():
    """Software aging is PURELY Resilience (K2): its coords dict has no
    damage/resilience_loss split, unlike storage.py's worst-disk coords --
    the whole axis value already IS the resilience-loss number."""
    rows = _series([_row(h, handles=1000) for h in range(6)])
    s = compute_software_aging_risk(rows)
    coords = s.source_lineage["coords"]
    assert set(coords.keys()) == {"flags"}
    assert "damage" not in coords
    assert "resilience_loss" not in coords
