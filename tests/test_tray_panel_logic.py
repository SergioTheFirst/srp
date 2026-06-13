"""Pure panel-row formatting (tray spec §2). The tkinter panel only places these
strings into labels; the text + warning flags are decided here so they can be
tested without a display.
"""

from __future__ import annotations

import json
from pathlib import Path

from client.tray import state as st

_DOC = {
    "ts": 1_000_000,
    "agent_version": "0.3.0",
    "last_send_ok_ts": 999_970,  # 30 s before now=1_000_000 in tests
    "hostname": "PC-042",
    "ips": ["192.168.1.42"],
    "org_code": "101",
    "dept_code": "7",
    "print_today_pages": 14,
    "print_month_pages": 312,
    "print_mode": "counter",
    "disk_free_gb": 41.2,
    "uptime_days": 3.5,
}


def _view(**overrides: object) -> st.StatusView:
    doc = {**_DOC, **overrides}
    return st.StatusView(raw=doc, age_sec=0.0, link_age_sec=None)


def test_org_dept_shows_codes_not_names() -> None:
    assert st.fmt_org_dept(_view()) == "Организация 101 · отдел 7"
    # missing codes render as a dash, never blank
    assert st.fmt_org_dept(_view(org_code="", dept_code="")) == "Организация — · отдел —"


def test_ip_first_or_dash() -> None:
    assert st.fmt_ip(_view()) == "192.168.1.42"
    assert st.fmt_ip(_view(ips=[])) == "—"


def test_print_line_translates_mode() -> None:
    assert st.fmt_print(_view()) == "сегодня 14 стр. · за месяц 312 стр. (счётчик)"
    assert "(журнал)" in st.fmt_print(_view(print_mode="events"))


def test_link_recent_vs_stale() -> None:
    assert "отправка" in st.fmt_link(_view(), now=1_000_000)  # 30 s ago -> ok
    assert "нет связи" in st.fmt_link(_view(), now=1_000_000 + 7200)  # >1 h -> stale
    assert "ещё не было" in st.fmt_link(_view(last_send_ok_ts=None), now=1_000_000)


def test_disk_warns_only_when_low() -> None:
    text, warn = st.fmt_disk(_view())
    assert "41" in text and warn is False
    _, warn_low = st.fmt_disk(_view(disk_free_gb=4.0))
    assert warn_low is True
    text_na, warn_na = st.fmt_disk(_view(disk_free_gb=None))
    assert text_na == "неизвестно" and warn_na is False


def test_uptime_soft_hint_past_two_weeks() -> None:
    _, hint = st.fmt_uptime(_view(uptime_days=3.5))
    assert hint is False
    _, hint_long = st.fmt_uptime(_view(uptime_days=21.0))
    assert hint_long is True


def test_view_survives_partial_status(tmp_path: Path) -> None:
    # a truncated status (only ts) must not crash any formatter
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"ts": 1_000_000}), encoding="utf-8")
    view = st.read_status(p, now=1_000_010)
    assert view is not None
    assert st.fmt_org_dept(view) == "Организация — · отдел —"
    assert st.fmt_ip(view) == "—"
    assert st.fmt_disk(view) == ("неизвестно", False)
    assert st.support_clipboard(view).count("\n") == 0
