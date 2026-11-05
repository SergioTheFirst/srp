"""Calibration D2, engine half (T1): software aging flags only a real, significant leak.

Rows are written oldest-first and handed to the engine newest-first (the
db.get_recent_heartbeats shape).
"""

from __future__ import annotations

from server.analytics.software_aging import compute_software_aging_risk


def _row(uptime, handles=None, *, user=None, mem=None, pagefile=None):
    row: dict = {"uptime_hours": uptime, "user_present": user}
    if handles is not None:
        row["handle_count_total"] = handles
    if mem is not None:
        row["mem_avail_mb"] = mem
    if pagefile is not None:
        row["pagefile_pct"] = pagefile
    return row


def _risk(rows_oldest_first):
    return compute_software_aging_risk(list(reversed(rows_oldest_first)))


def _flags(s):
    return (s.source_lineage.get("coords") or {}).get("flags", [])


def _labels(s):
    return [f["label"] for f in s.factors]


def test_prelogon_rows_dropped_when_session_has_logon():
    prelogon = [_row(h, 42000, user=False) for h in (0.3, 4.3, 8.3)]
    logged_on = [_row(12.3 + 4 * k, 98000, user=True) for k in range(19)]  # 3 days, flat
    s = _risk(prelogon + logged_on)
    assert s.source_lineage["session_points"] == 19
    assert "aging_leak" not in _flags(s)
    assert not any("дескрипторов" in label for label in _labels(s))


def test_fast_startup_resume_prelogon_rows_dropped():
    rows = []
    for day in range(5):  # one session: Fast Startup keeps uptime running overnight
        rows.append(_row(4.0 + 24 * day, 42000, user=False))  # morning resume, before logon
        rows += [_row(8.0 + 24 * day + 3 * k, 98000, user=True) for k in range(5)]
    s = _risk(rows)
    assert s.source_lineage["session_points"] == 25
    assert "aging_leak" not in _flags(s)
    assert not any("дескрипторов" in label for label in _labels(s))


def test_kiosk_session_keeps_rows():
    hours = [4.0 + 4 * k for k in range(19)]  # 3 days, nobody ever logs on
    s = _risk([_row(h, 42000 + 1000 * h, user=False) for h in hours])
    assert s.source_lineage["session_points"] == 19
    assert any("утечка дескрипторов" in label for label in _labels(s))
    assert "aging_leak" in _flags(s)


def test_warmup_rows_under_4h_dropped():
    ramp = [_row(0.3, 28000)]  # the post-logon application ramp
    usable = [_row(4.3 + 4 * k, 42000) for k in range(4)]
    assert _risk(ramp + usable).source_lineage["session_points"] == 4
    assert _risk(ramp + usable[:3]).value is None  # 3 usable rows: UNKNOWN, not 4


def test_leak_needs_48h_span():
    def leak(span_h):
        hours = [4.0 + span_h * k / 7 for k in range(8)]
        return _risk([_row(h, 42000 + 1000 * h) for h in hours])

    assert not any("дескрипторов" in label for label in _labels(leak(30.0)))
    assert any("утечка дескрипторов" in label for label in _labels(leak(60.0)))


def test_context_factors_alone_never_set_aging_leak():
    rows = [_row(300.0 + 12 * k, 50000, pagefile=9 if k < 3 else 11) for k in range(6)]
    s = _risk(rows)  # 360 h of uptime, flat handles, pagefile 9 -> 11
    assert "aging_leak" not in _flags(s)
    assert not any("pagefile" in label for label in _labels(s))
    assert any("без перезагрузки" in label for label in _labels(s))  # context stays visible


def test_accelerating_needs_both_sessions_above_watch():
    def sessions(prev_rate, cur_rate):
        hours = [4.0 + 8 * k for k in range(8)]
        prev = [_row(h, 40000 + prev_rate * h) for h in hours]
        cur = [_row(h, 40000 + cur_rate * h) for h in hours]
        return _risk(prev + cur)

    assert "aging_accelerating" not in _flags(sessions(50, 120))
    assert "aging_accelerating" in _flags(sessions(150, 400))


def test_session_too_young_for_any_slope_is_never_high_confidence():
    def session(n):  # logged on, +400 handles/h, every row past the warm-up
        return _risk([_row(4.0 + 4 * k, 42000 + 1600 * k, user=True) for k in range(n)])

    young = session(6)  # 20 h: neither slope is assessable, so the leak is invisible
    assert (young.value, young.confidence) == (0.0, "medium")
    assert session(14).confidence == "high"  # 52 h: the handle slope is assessed


def test_filtered_session_under_four_points_is_unknown():
    s = _risk([_row(h, 42000) for h in (0.3, 1.5, 2.5, 3.5, 4.5, 8.5, 12.5)])
    assert s.value is None
    assert s.band == "unknown"
    assert any("короче 4 показаний" in m for m in s.missing_evidence)
