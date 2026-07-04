"""T2 (nbtstat): agent-side NetBIOS naming of LAN neighbors.

The agent is the only host L2-adjacent to a remote site's LAN, so it is the
only vantage point that can name those neighbors -- NBNS (UDP/137) does not
route off-subnet. This module used to speak NBNS directly over a user-space
UDP socket; live debugging proved that unreliable on Windows (NetBT owns
UDP/137, a user-space socket gets no reply even to the box's own name). The
Windows-native ``nbtstat -A <ip>`` tool DOES get answers (live-verified
against real LAN hosts: MEDPOST/SKPD3/I3), so this module now shells out to
it. ``_parse_nbtstat`` is the pure text->name parser (locale-independent:
reads only the numeric ``<20>`` suffix marker, never the UNIQUE/GROUP/
Registered words that localize); ``resolve_netbios_names`` is the bounded,
RFC1918-only, fail-closed thread-pooled collector around it. The subprocess
runner is injected so this suite never spawns a real process.
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable, Dict, List

from client.collectors import lan_names

# --------------------------------------------------------------------------- #
# _parse_nbtstat -- pure text parser                                          #
# --------------------------------------------------------------------------- #

# Real `nbtstat -A` output, as captured by the architect during live recon.
_REAL_OUTPUT = """
NetBIOS Remote Machine Name Table

   MEDPOST        <00>  UNIQUE      Registered
   MEDPOST        <20>  UNIQUE      Registered
   WORKGROUP      <00>  GROUP       Registered

   MAC Address = D4-3D-7E-3C-12-88
"""


def _nbtstat_text(name: str) -> str:
    """A synthetic-but-realistic `nbtstat -A` body naming *name* at <20>."""
    return (
        "\nNetBIOS Remote Machine Name Table\n\n"
        f"   {name}        <00>  UNIQUE      Registered\n"
        f"   {name}        <20>  UNIQUE      Registered\n"
        "   WORKGROUP      <00>  GROUP       Registered\n"
    )


def test_parse_extracts_name_from_suffix_0x20_line():
    assert lan_names._parse_nbtstat(_REAL_OUTPUT) == "MEDPOST"


def test_parse_is_locale_independent_of_status_words():
    """UNIQUE/GROUP/Registered localize on non-English consoles (the
    architect observed mojibake on a Russian console) -- replacing them with
    another language, or outright garbage, must not change the parsed name,
    since the parser never looks past the <20> marker on the matching line."""
    localized = (
        "\n   MEDPOST        <00>  УНИКАЛЬНЫЙ   Зарегистрирован\n"
        "   MEDPOST        <20>  УНИКАЛЬНЫЙ   Зарегистрирован\n"
        "   WORKGROUP      <00>  ГРУППА       Зарегистрирован\n"
    )
    assert lan_names._parse_nbtstat(localized) == "MEDPOST"

    garbled = "   MEDPOST        <20>  ���      ���\n"
    assert lan_names._parse_nbtstat(garbled) == "MEDPOST"


def test_parse_fail_closed_without_suffix_0x20():
    text = (
        "   MEDPOST        <00>  UNIQUE      Registered\n"
        "   MEDPOST        <1C>  GROUP       Registered\n"
    )
    assert lan_names._parse_nbtstat(text) is None


def test_parse_fail_closed_on_group_only_output():
    assert lan_names._parse_nbtstat("   WORKGROUP      <00>  GROUP       Registered\n") is None


def test_parse_fail_closed_on_unclean_name():
    assert lan_names._parse_nbtstat("   BAD!NAME       <20>  UNIQUE      Registered\n") is None


def test_parse_fail_closed_on_overlong_name():
    text = "   WAYTOOLONGANAME12345 <20>  UNIQUE      Registered\n"
    assert lan_names._parse_nbtstat(text) is None


def test_parse_fail_closed_on_host_not_found():
    assert lan_names._parse_nbtstat("Host not found.\n") is None


def test_parse_fail_closed_on_empty_output():
    assert lan_names._parse_nbtstat("") is None


def test_parse_takes_first_suffix_0x20_line_when_several_present():
    text = (
        "   FIRSTHOST      <20>  UNIQUE      Registered\n"
        "   SECONDHOST     <20>  UNIQUE      Registered\n"
    )
    assert lan_names._parse_nbtstat(text) == "FIRSTHOST"


# --------------------------------------------------------------------------- #
# resolve_netbios_names -- bounded thread-pooled collector (injected runner)  #
# --------------------------------------------------------------------------- #


def _canned(table: Dict[str, str]) -> Callable[[str], str]:
    """A runner stub: each ip's canned nbtstat text, else a not-found stub."""
    return lambda ip: table.get(ip, "Host not found.\n")


def _recording(calls: List[str], *, text: str = "") -> Callable[[str], str]:
    """A runner stub that just records which ips it was asked to resolve."""

    def runner(ip: str) -> str:
        calls.append(ip)
        return text

    return runner


def test_resolve_returns_name_for_responder():
    out = lan_names.resolve_netbios_names(
        ["10.0.0.3"], runner=_canned({"10.0.0.3": _nbtstat_text("MEDPOST")})
    )
    assert out == {"10.0.0.3": "MEDPOST"}


def test_resolve_never_queries_public_ip():
    calls: List[str] = []
    out = lan_names.resolve_netbios_names(["8.8.8.8"], runner=_recording(calls))
    assert out == {}
    assert calls == []  # the runner is never even invoked for an all-public batch


def test_resolve_filters_public_from_mixed_batch():
    calls: List[str] = []
    lan_names.resolve_netbios_names(["8.8.8.8", "10.0.0.5", "1.1.1.1"], runner=_recording(calls))
    assert calls == ["10.0.0.5"]


def test_resolve_cap_bounds_fanout():
    ips = [f"10.0.0.{i}" for i in range(1, 251)]  # 250 distinct RFC1918 ips
    calls: List[str] = []
    lan_names.resolve_netbios_names(ips, cap=5, runner=_recording(calls))
    assert len(calls) == 5


def test_resolve_no_reply_yields_no_entry():
    out = lan_names.resolve_netbios_names(["10.0.0.5"], runner=_canned({}))
    assert out == {}


def test_resolve_dedupes_repeated_ip():
    calls: List[str] = []
    lan_names.resolve_netbios_names(["10.0.0.5", "10.0.0.5", "10.0.0.5"], runner=_recording(calls))
    assert calls == ["10.0.0.5"]


def test_resolve_multiple_responders():
    table = {
        "192.168.9.6": _nbtstat_text("MEDPOST"),
        "192.168.9.25": _nbtstat_text("SKPD3"),
    }
    out = lan_names.resolve_netbios_names(["192.168.9.6", "192.168.9.25"], runner=_canned(table))
    assert out == {"192.168.9.6": "MEDPOST", "192.168.9.25": "SKPD3"}


def test_resolve_skips_malformed_ip_strings():
    calls: List[str] = []
    lan_names.resolve_netbios_names(["not-an-ip", "10.0.0.5"], runner=_recording(calls))
    assert calls == ["10.0.0.5"]


def test_resolve_empty_input_returns_empty_without_calling_runner():
    calls: List[str] = []
    out = lan_names.resolve_netbios_names([], runner=_recording(calls))
    assert out == {}
    assert calls == []


def test_resolve_runner_exception_for_one_host_leaves_others_resolved():
    def runner(ip: str) -> str:
        if ip == "10.0.0.5":
            raise TimeoutError("nbtstat hung")
        return _nbtstat_text("OKHOST")

    out = lan_names.resolve_netbios_names(["10.0.0.5", "10.0.0.6"], runner=runner)
    assert out == {"10.0.0.6": "OKHOST"}


def test_resolve_malformed_reply_yields_no_entry_not_a_crash():
    out = lan_names.resolve_netbios_names(
        ["10.0.0.5"], runner=_canned({"10.0.0.5": "garbage\x00\x01 no marker here"})
    )
    assert out == {}


def test_resolve_overall_deadline_bounds_wall_clock_and_keeps_partial_results():
    """A slow host must not make the whole call hang past ``overall_deadline``
    -- the fast host's result is kept, the slow one is simply abandoned."""

    def runner(ip: str) -> str:
        if ip == "10.0.0.9":
            time.sleep(0.6)
            return _nbtstat_text("SLOWHOST")
        return _nbtstat_text("FASTHOST")

    start = time.monotonic()
    out = lan_names.resolve_netbios_names(
        ["10.0.0.8", "10.0.0.9"], runner=runner, overall_deadline=0.15, max_workers=2
    )
    elapsed = time.monotonic() - start
    assert out == {"10.0.0.8": "FASTHOST"}
    assert elapsed < 0.45  # well under the slow host's 0.6s -- deadline, not the sleep, won


def test_resolve_default_runner_shells_out_to_nbtstat_with_arg_list(monkeypatch):
    """Exercises the DEFAULT runner path (no injected ``runner``) through a
    stubbed ``subprocess.run`` -- proves it calls `nbtstat -A <ip>` as an
    argument list (no shell) with a timeout and no console window, without
    this test itself spawning a real process."""
    captured = {}

    class _FakeCompleted:
        stdout = _nbtstat_text("STUBHOST").encode("utf-8")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = lan_names.resolve_netbios_names(["10.0.0.7"], timeout=1.5)
    assert out == {"10.0.0.7": "STUBHOST"}
    assert captured["argv"] == ["nbtstat", "-A", "10.0.0.7"]
    assert captured["kwargs"]["timeout"] == 1.5
    assert captured["kwargs"]["creationflags"] == lan_names.NO_WINDOW
    assert "shell" not in captured["kwargs"]  # never shell=True


def test_resolve_default_runner_is_fail_closed_on_subprocess_error(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("nbtstat.exe not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = lan_names.resolve_netbios_names(["10.0.0.7"])
    assert out == {}


def test_resolve_default_runner_is_fail_closed_on_subprocess_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = lan_names.resolve_netbios_names(["10.0.0.7"])
    assert out == {}
