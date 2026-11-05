"""Agent core-loop resilience: a broken Transport.send() must not kill the
loop -- same "a broken collector must not kill the loop" pattern already
applied to the collector/updater calls a few lines above each send() (P1-2).
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import urllib.error
from pathlib import Path

import pytest
from client import agent as agent_mod
from client import transport as transport_mod
from client.agent import Agent
from client.collectors.sources import CollectorResult
from client.config import ClientConfig

pytestmark = pytest.mark.unit


def _cfg(tmp_path, **overrides) -> ClientConfig:
    base = {
        "server_url": "http://127.0.0.1:9",
        "device_id": "t-dev",
        "buffer_path": str(tmp_path / "buffer.jsonl"),
    }
    base.update(overrides)
    return ClientConfig(**base)


def _raising_send(*args, **kwargs):
    # A realistic trigger per the plan: a collector payload with a value the
    # JSON encoder can't handle.
    raise TypeError("Object of type set is not JSON serializable")


def test_run_task_survives_a_broken_transport_send(tmp_path, monkeypatch):
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent._transport, "send", _raising_send)
    result = CollectorResult(payload={"cpu_pct": 1.0}, source_health={})
    agent._run_task("heartbeat", lambda: result)  # must not raise


def test_reconcile_update_survives_a_broken_transport_send(tmp_path, monkeypatch):
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent._transport, "send", _raising_send)
    monkeypatch.setattr(agent._updater, "reconcile_after_restart", lambda version: {"state": "ok"})
    agent._reconcile_update()  # must not raise


def test_run_update_check_survives_a_broken_transport_send(tmp_path, monkeypatch):
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent._transport, "send", _raising_send)
    monkeypatch.setattr(agent._updater, "check", lambda version: ({"state": "ok"}, False))
    restart = agent._run_update_check()  # must not raise
    assert restart is False


def test_run_task_still_delivers_when_transport_send_is_healthy(tmp_path, monkeypatch):
    """Regression guard: the try/except must not swallow a normal delivery."""
    agent = Agent(_cfg(tmp_path))
    calls: list[tuple] = []
    monkeypatch.setattr(agent._transport, "send", lambda *a: calls.append(a) or True)
    result = CollectorResult(payload={"cpu_pct": 1.0}, source_health={})
    agent._run_task("heartbeat", lambda: result)
    assert calls == [("heartbeat", {"cpu_pct": 1.0}, {})]


def test_liveness_result_is_debug_and_never_claims_buffered(tmp_path, monkeypatch, caplog):
    """0.3.4 #1: pings are never buffered -- an INFO 'buffered (offline)' every 5 min lied."""
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent_mod, "collect_owner_identity", lambda: {})
    monkeypatch.setattr(agent._transport, "send", lambda *a: False)
    with caplog.at_level(logging.DEBUG, logger="srp.agent"):
        agent._run_task("liveness", lambda: CollectorResult({"alive": True}, {}))
    records = [r for r in caplog.records if r.name == "srp.agent"]
    assert [r.levelno for r in records] == [logging.DEBUG]
    assert "buffered" not in records[0].getMessage()


# --------------------------------------------------------------------------- #
# 0.3.2: повтор неудачной инвентаризации (причина №2 спеки 2026-10-14: холодный
# WMI сразу после загрузки -> timeout/empty -> untrusted на целый цикл 4 ч).
# --------------------------------------------------------------------------- #
_OK = CollectorResult({"hostname": "PC1"}, {"identity": {"status": "ok"}})
_TIMEOUT = CollectorResult(None, {"identity": {"status": "timeout"}})
_EMPTY = CollectorResult(None, {"identity": {"status": "empty"}})


@pytest.mark.parametrize(
    ("result", "delivered", "expected"),
    [
        (_OK, True, True),
        (_OK, False, True),  # buffered (offline) is not a failed run -- the buffer replays it
        (_TIMEOUT, True, False),
        (_EMPTY, True, False),
        (CollectorResult(None, {}), True, False),
    ],
)
def test_run_task_reports_whether_the_run_was_clean(
    tmp_path, monkeypatch, result, delivered, expected
) -> None:
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent_mod, "collect_owner_identity", lambda: {})
    monkeypatch.setattr(agent._transport, "send", lambda *a: delivered)
    assert agent._run_task("inventory", lambda: result) is expected


def test_run_task_raising_collector_or_send_is_not_clean(tmp_path, monkeypatch) -> None:
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent_mod, "collect_owner_identity", lambda: {})

    def _boom():
        raise RuntimeError("PowerShell died")

    assert agent._run_task("inventory", _boom) is False
    monkeypatch.setattr(agent._transport, "send", _raising_send)
    assert agent._run_task("inventory", lambda: _OK) is False


def test_inventory_retry_ladder_doubles_caps_and_resets(tmp_path) -> None:
    agent = Agent(_cfg(tmp_path))

    fails = [agent._inventory_delay(False, 14400) for _ in range(7)]

    assert fails == [600, 1200, 2400, 4800, 9600, 14400, 14400]
    assert agent._inventory_delay(True, 14400) == 14400  # success: normal cadence
    assert agent._inventory_delay(False, 14400) == 600  # ...and the ladder restarts
    assert agent._inventory_delay(False, 300) == 300  # never slower than the interval


class _Clock:
    """Fake ``time`` for run_forever: sleep advances the clock; reaching *until*
    raises KeyboardInterrupt -- run_forever's own clean exit."""

    def __init__(self, until: float) -> None:
        self.now = 0.0
        self.until = until

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now >= self.until:
            raise KeyboardInterrupt


def _loop_agent(tmp_path, monkeypatch, clock: _Clock, inventory) -> Agent:
    agent = Agent(_cfg(tmp_path, inventory_interval_sec=14400))
    monkeypatch.setattr(agent_mod, "time", clock)
    monkeypatch.setattr(agent_mod.random, "uniform", lambda low, _high: low)
    monkeypatch.setattr(agent_mod, "publish_status", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "collect_owner_identity", lambda: {})
    monkeypatch.setattr(agent._transport, "flush_buffer", lambda: 0)
    monkeypatch.setattr(agent._transport, "send", lambda *a, **k: True)
    monkeypatch.setattr(agent._updater, "reconcile_after_restart", lambda _v: None)
    monkeypatch.setattr(agent._updater, "check", lambda _v: (None, False))
    agent._tasks = [("inventory", inventory, "inventory_interval_sec")]
    return agent


def test_failed_inventory_is_retried_on_a_doubling_ladder(tmp_path, monkeypatch) -> None:
    clock = _Clock(until=20_000)
    runs: list[float] = []
    outcomes = iter([_TIMEOUT, _TIMEOUT, _OK, _OK])

    def inventory() -> CollectorResult:
        runs.append(clock.now)
        return next(outcomes)

    _loop_agent(tmp_path, monkeypatch, clock, inventory).run_forever()

    # settle 60 s after start, then +10 min, +20 min, then the normal 4 h
    assert runs == [60, 660, 1860, 16260]


def test_heavy_tasks_settle_after_start_but_liveness_is_immediate(tmp_path, monkeypatch) -> None:
    """0.3.4 #8: every task was due at start -- after a morning power-on the heavy
    collectors ran during logon on cold WMI (inventory failed on 7 of 28 PCs)."""
    clock = _Clock(until=1000)
    runs: list[tuple[str, float]] = []
    agent = _loop_agent(
        tmp_path, monkeypatch, clock, lambda: runs.append(("inventory", clock.now)) or _OK
    )
    agent._tasks.append(
        ("liveness", lambda: runs.append(("liveness", clock.now)) or _OK, "liveness_interval_sec")
    )

    agent.run_forever()

    assert runs[:2] == [("liveness", 0), ("inventory", 60)]  # uniform(60, 300) -> its low end


def test_the_heartbeat_settles_after_the_historical(tmp_path, monkeypatch) -> None:
    """Agent 1.0.0 (H16): after a start the heartbeat waits for the historical, so the
    morning's first heartbeat recompute sees fresh SMART instead of a stale source."""
    clock = _Clock(until=1000)
    runs: list[tuple[str, float]] = []
    agent = _loop_agent(tmp_path, monkeypatch, clock, lambda: _OK)
    agent._tasks = [
        (
            "heartbeat",
            lambda: runs.append(("heartbeat", clock.now)) or _OK,
            "heartbeat_interval_sec",
        ),
        (
            "historical",
            lambda: runs.append(("historical", clock.now)) or _OK,
            "historical_interval_sec",
        ),
    ]

    agent.run_forever()

    assert runs[:2] == [("historical", 60), ("heartbeat", 120)]  # low end + the 60 s gap


def test_a_long_pass_still_sends_the_historical_before_the_heartbeat(tmp_path, monkeypatch) -> None:
    """H16: inventory on cold WMI (its 60 s run_ps timeout + the send) carries the pass past
    the settle gap -- both are due at one check, and the REAL task order must still send
    fresh SMART first."""
    clock = _Clock(until=1000)
    runs: list[str] = []
    order = [(m, a) for m, _, a in Agent(_cfg(tmp_path))._tasks]
    agent = _loop_agent(tmp_path, monkeypatch, clock, lambda: _OK)

    def _task(name: str):
        def run() -> CollectorResult:
            runs.append(name)
            clock.now += 61 if name == "inventory" else 0
            return _OK

        return run

    agent._tasks = [(m, _task(m), a) for m, a in order]

    agent.run_forever()

    assert runs.index("historical") < runs.index("heartbeat")


class _SleepyClock(_Clock):
    """The PC sleeps for an hour inside one loop sleep: time.monotonic
    (GetTickCount64) keeps counting, the sleep call returns late."""

    def __init__(self, until: float, nap_at: float) -> None:
        super().__init__(until)
        self.nap_at = nap_at
        self.woke_at = 0.0

    def sleep(self, seconds: float) -> None:
        if self.now >= self.nap_at and not self.woke_at:
            self.now += 3600
            self.woke_at = self.now + seconds
        super().sleep(seconds)


def test_heavy_tasks_settle_again_after_a_wake(tmp_path, monkeypatch) -> None:
    """Fast Startup keeps the SYSTEM agent alive: on wake every 4 h task is overdue."""
    clock = _SleepyClock(until=20_000, nap_at=14_000)
    runs: list[float] = []
    agent = _loop_agent(tmp_path, monkeypatch, clock, lambda: runs.append(clock.now) or _OK)

    agent.run_forever()

    assert runs == [60, clock.woke_at + 60]  # due during the nap -> 1 min after the wake


def test_run_forever_survives_a_raising_flush(tmp_path, monkeypatch) -> None:
    """F1/RG-2: flush_buffer был единственным неохраняемым вызовом цикла -- битая
    UTF-8 строка буфера убивала агента (оконный PyInstaller вешал невидимое окно)."""
    clock = _Clock(until=1000)
    runs: list[float] = []
    agent = _loop_agent(tmp_path, monkeypatch, clock, lambda: runs.append(clock.now) or _OK)

    def _torn_buffer() -> int:
        raise UnicodeDecodeError("utf-8", b"\xd0", 0, 1, "torn append")

    monkeypatch.setattr(agent._transport, "flush_buffer", _torn_buffer)

    agent.run_forever()  # must not raise

    assert runs == [60]  # the collector still ran (after the start settle) despite the flush
    assert clock.now >= 1000  # and the loop kept going


def test_run_forever_survives_any_exception_in_a_pass(tmp_path, monkeypatch) -> None:
    clock = _Clock(until=1000)
    agent = _loop_agent(tmp_path, monkeypatch, clock, lambda: _OK)

    def _status_bug(*_a, **_k) -> None:
        raise RuntimeError("status writer bug")

    monkeypatch.setattr(agent_mod, "publish_status", _status_bug)

    agent.run_forever()  # must not raise

    assert clock.now >= 1000


def test_staged_update_waits_for_setup_instead_of_exiting(tmp_path, monkeypatch) -> None:
    """AGT-HANDOFF-1/AV-2: агент выходил сразу после постановки задачи -- если
    setup.exe не стартовал (антивирус, политика), ПК оставался без агента до
    настоящей перезагрузки (Fast Startup задачу не перезапускает)."""
    clock = _Clock(until=10_000)
    agent = _loop_agent(tmp_path, monkeypatch, clock, lambda: _OK)
    sent: list[tuple[float, str]] = []

    def _send(msg_type, payload, *_rest) -> bool:
        if msg_type == "update_status":
            sent.append((clock.now, payload["state"]))
        return True

    checks = iter([({"state": "updating", "available_version": "9.9.9"}, True)])
    monkeypatch.setattr(agent._transport, "send", _send)
    monkeypatch.setattr(agent._updater, "check", lambda _v: next(checks, (None, False)))
    monkeypatch.setattr(agent._updater, "handoff_failed", lambda: {"state": "failed"})

    agent.run_forever()

    assert [state for _, state in sent] == ["updating", "failed"]
    assert sent[1][0] - sent[0][0] == agent_mod._HANDOFF_WAIT_SEC
    assert clock.now >= 10_000  # carried on collecting -- no exit


# --------------------------------------------------------------------------- #
# RG-2: точка входа srp-agent.exe -- необработанное исключение = выход 1
# --------------------------------------------------------------------------- #
def test_entry_point_turns_an_unhandled_crash_into_exit_1(monkeypatch) -> None:
    """Оконный PyInstaller показывает трейсбек окном -- в сессии 0 его некому
    закрыть: процесс висит вечно, держит мьютекс и обманывает проверку живости."""
    import srp_agent_main

    def _bug() -> None:
        raise RuntimeError("missing hidden import")

    monkeypatch.setattr(agent_mod, "main", _bug)
    with pytest.raises(SystemExit) as exc:
        srp_agent_main.run()
    assert exc.value.code == 1


def test_entry_point_keeps_deliberate_exit_codes(monkeypatch) -> None:
    import srp_agent_main

    def _bad_config() -> None:
        raise SystemExit(2)

    monkeypatch.setattr(agent_mod, "main", _bad_config)
    with pytest.raises(SystemExit) as exc:
        srp_agent_main.run()
    assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# W: watchdog pause marker (update\hold) -- held() matrix + main() gate
# --------------------------------------------------------------------------- #
_NOW = 10_000.0
_UPTIME = 5_000.0  # boot = _NOW - _UPTIME = 5_000


def _write_hold(install_dir: Path, content: bytes, mtime: float) -> Path:
    marker = install_dir / "update" / "hold"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(content)
    os.utime(marker, (mtime, mtime))
    return marker


def test_held_false_when_no_marker(tmp_path) -> None:
    assert agent_mod.held(tmp_path, now=_NOW) is False


def test_held_false_for_a_marker_from_a_previous_boot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    _write_hold(tmp_path, b"stop", mtime=4_000.0)  # before boot (5_000)
    assert agent_mod.held(tmp_path, now=_NOW) is False


def test_held_keeps_a_stop_marker_after_the_clock_steps_back(tmp_path, monkeypatch) -> None:
    """Часы перевели назад после «Выход»: пауза держит до перезагрузки, как обещано."""
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    _write_hold(tmp_path, b"stop", mtime=11_000.0)  # after now
    assert agent_mod.held(tmp_path, now=_NOW) is True


def test_held_swap_marker_far_in_the_future_expires(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    _write_hold(tmp_path, b"swap", mtime=_NOW + agent_mod._HOLD_SWAP_TTL_SEC + 60)
    assert agent_mod.held(tmp_path, now=_NOW) is False


def test_held_true_for_a_stop_marker_after_boot(tmp_path, monkeypatch) -> None:
    """«Выход» (stop) has no TTL -- it holds until the next reboot."""
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    _write_hold(tmp_path, b"stop", mtime=6_000.0)  # long after boot, no expiry
    assert agent_mod.held(tmp_path, now=_NOW) is True


def test_held_false_for_a_swap_marker_older_than_ttl(tmp_path, monkeypatch) -> None:
    """A "swap" marker older than 30 min = setup.exe died mid-swap -> ignore it."""
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    stale = _NOW - agent_mod._HOLD_SWAP_TTL_SEC - 100  # within [boot, now] but expired
    _write_hold(tmp_path, b"swap", mtime=stale)
    assert agent_mod.held(tmp_path, now=_NOW) is False


def test_held_true_for_a_fresh_swap_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    _write_hold(tmp_path, b"swap", mtime=_NOW - 100)  # well inside the TTL
    assert agent_mod.held(tmp_path, now=_NOW) is True


def test_held_false_when_uptime_is_unavailable(tmp_path, monkeypatch) -> None:
    """Fail-open: without a boot time we cannot reject a stale marker, so better
    to run than stay dark on a quirk."""
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: None)
    _write_hold(tmp_path, b"swap", mtime=_NOW - 100)
    assert agent_mod.held(tmp_path, now=_NOW) is False


def test_main_frozen_and_held_returns_before_load_config(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "srp-agent.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(agent_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(agent_mod.sys, "executable", str(exe))
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    _write_hold(tmp_path, b"swap", mtime=agent_mod.time.time())  # fresh, within [boot, now]

    def _boom() -> ClientConfig:
        raise AssertionError("load_config must not run while the pause is held")

    monkeypatch.setattr(agent_mod, "load_config", _boom)

    # Returns cleanly before load_config -- if the pause gate failed, _boom fires.
    agent_mod.main([])


def test_main_once_runs_even_when_a_hold_marker_is_present(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "srp-agent.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(agent_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(agent_mod.sys, "executable", str(exe))
    monkeypatch.setattr(agent_mod, "_uptime_sec", lambda: _UPTIME)
    _write_hold(tmp_path, b"swap", mtime=agent_mod.time.time())
    monkeypatch.setattr(agent_mod, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(agent_mod, "validate_runtime_config", lambda cfg: None)
    ran: list[bool] = []
    monkeypatch.setattr(Agent, "run_once", lambda self: ran.append(True) or True)

    agent_mod.main(["--once"])

    assert ran == [True]  # --once bypasses the pause -- the install check must run for real


# --------------------------------------------------------------------------- #
# 0.3.4 #2: --once is the installer's link check -- exit 3 when the server
# accepted nothing. run_once ignored every result, so a wrong URL, token or port
# still installed a PC that never showed up on the dashboard.
# --------------------------------------------------------------------------- #
def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/api/v1/ingest", code, "x", None, io.BytesIO(b""))


@pytest.mark.parametrize(
    ("reply", "offline", "accepted"),
    [
        (urllib.error.URLError("refused"), False, False),  # wrong URL / closed port
        (_http_error(401), False, False),  # wrong or missing token
        (_http_error(406), False, False),  # MAJOR contract mismatch -- buffered (1.0.0)
        (_http_error(429), False, True),  # token accepted: api.py checks it before the limiter
        (None, False, True),  # 2xx
        (urllib.error.URLError("unused"), True, True),  # offline_mode: no link to check
    ],
)
def test_run_once_reports_whether_the_server_accepted_anything(
    tmp_path, monkeypatch, reply, offline, accepted
) -> None:
    agent = Agent(_cfg(tmp_path, offline_mode=offline))
    agent._tasks = [("liveness", lambda: CollectorResult({"alive": True}, {}), "x")]
    monkeypatch.setattr(agent_mod, "collect_owner_identity", lambda: {})
    monkeypatch.setattr(agent_mod, "publish_status", lambda *a, **k: None)
    monkeypatch.setattr(transport_mod.time, "sleep", lambda _s: None)

    def _urlopen(*_a, **_kw):
        if reply is None:
            return contextlib.nullcontext()
        raise reply

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", _urlopen)
    assert agent.run_once() is accepted


def test_main_once_exits_3_when_the_server_accepted_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(Agent, "run_once", lambda self: False)
    with pytest.raises(SystemExit) as exc:
        agent_mod.main(["--once"])
    assert exc.value.code == 3  # setup.py: rc != 0 -> EXIT_SERVER_UNREACHABLE


# --------------------------------------------------------------------------- #
# 0.3.5: в srp-agent.log видно, КАКОЙ источник не ok и почему (Dietnew: 4 дня гаданий)
# --------------------------------------------------------------------------- #
def _sent_records(tmp_path, monkeypatch, caplog, result):
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent_mod, "collect_owner_identity", lambda: {})
    monkeypatch.setattr(agent._transport, "send", lambda *a: True)
    with caplog.at_level(logging.DEBUG, logger="srp.agent"):
        agent._run_task("inventory", lambda: result)
    return [r for r in caplog.records if r.name == "srp.agent"]


def test_broken_powershell_logs_one_warning_with_reason(tmp_path, monkeypatch, caplog):
    broken = CollectorResult(
        None, {"identity": {"status": "empty", "reason": "CommandNotFoundException"}}
    )
    records = _sent_records(tmp_path, monkeypatch, caplog, broken)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "identity=empty:CommandNotFoundException" in records[0].getMessage()


def test_clean_run_logs_plain_info_without_suffix(tmp_path, monkeypatch, caplog):
    records = _sent_records(tmp_path, monkeypatch, caplog, _OK)
    assert [r.levelno for r in records] == [logging.INFO]
    assert records[0].getMessage() == "inventory: sent"


def test_legit_empty_source_is_info_not_warning(tmp_path, monkeypatch, caplog):
    quiet = CollectorResult({"events": []}, {"events": {"status": "empty"}})
    records = _sent_records(tmp_path, monkeypatch, caplog, quiet)
    assert [r.levelno for r in records] == [logging.INFO]
    assert records[0].getMessage() == "inventory: sent [events=empty]"
