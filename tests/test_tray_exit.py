"""Трей «Выход» -> elevated --stop-agent: чистые части, без Win32-вызовов.

ctypes-обвязка (ShellExecuteW / MessageBoxW) остаётся тонкой и нетестируемой --
та же политика, что в client/tray/icon.py; всё решающее инжектируется.
"""

from __future__ import annotations

from client.tray.__main__ import _parse_args, _stop_agent_params, run_stop_agent


def test_parse_args_stop_agent_flag() -> None:
    assert _parse_args(["--stop-agent"]).stop_agent is True
    assert _parse_args([]).stop_agent is False


def test_stop_agent_params_frozen_vs_dev() -> None:
    assert _stop_agent_params(True) == "--stop-agent"
    assert _stop_agent_params(False) == "-m client.tray --stop-agent"


def test_run_stop_agent_ends_task_before_taskkill_then_ok() -> None:
    calls: list[list[str]] = []

    def fail_alert(_msg: str) -> None:
        raise AssertionError("no alert expected on success")

    rc = run_stop_agent(
        runner=lambda cmd: calls.append(cmd) or 0,
        wait_unlocked=lambda: True,
        alert=fail_alert,
    )

    assert rc == 0
    assert [c[0] for c in calls] == ["schtasks", "taskkill"]
    assert calls[0][:2] == ["schtasks", "/end"]
    assert "srp-agent.exe" in calls[1]


def test_run_stop_agent_locked_exe_alerts_and_returns_1() -> None:
    alerts: list[str] = []

    rc = run_stop_agent(runner=lambda cmd: 0, wait_unlocked=lambda: False, alert=alerts.append)

    assert rc == 1
    assert alerts and "srp-agent.exe" in alerts[0]
