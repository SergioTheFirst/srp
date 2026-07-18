"""``setup.exe --update`` logic (agent-auto-update plan T3, 2026-07-03).

Pure, off-Windows tests for the update mode of ``client.deploy.setup``: the
--update flag/auto-quiet/validate skip, the two new privileged-command argv
builders, the language-independent "process is dead" probe (never parses
tasklist output), and the ``run_update`` orchestration (stop -> wait -> copy
-> ACL -> recreate task -> cleanup) with ``_run`` monkeypatched so nothing
actually shells out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from client.deploy import setup as su

# --------------------------------------------------------------------------- #
# --update flag: auto-quiet + validate skip
# --------------------------------------------------------------------------- #


def test_parse_args_update_flag_implies_quiet() -> None:
    opts = su.parse_args(["--update"])
    assert opts.update is True
    assert opts.quiet is True


def test_validate_update_skips_org_server_requirement() -> None:
    su.validate(su.SetupOptions(update=True))  # must not raise -- no org/server given


# --------------------------------------------------------------------------- #
# new privileged command argv (data, never executed)
# --------------------------------------------------------------------------- #


def test_taskkill_agent_cmd() -> None:
    assert su.taskkill_agent_cmd() == ["taskkill", "/im", su.AGENT_EXE, "/f"]


def test_update_task_delete_cmd() -> None:
    assert su.UPDATE_TASK_NAME == "SRP Agent Update"
    assert su.update_task_delete_cmd() == ["schtasks", "/delete", "/tn", "SRP Agent Update", "/f"]


# --------------------------------------------------------------------------- #
# _file_unlocked / _wait_files_unlocked -- language-independent dead-process probe
# --------------------------------------------------------------------------- #


def test_file_unlocked_missing_file_is_true(tmp_path: Path) -> None:
    assert su._file_unlocked(tmp_path / "nope.exe") is True


def test_file_unlocked_ordinary_file_is_true(tmp_path: Path) -> None:
    p = tmp_path / "some.exe"
    p.write_bytes(b"stub")
    assert su._file_unlocked(p) is True


def test_wait_files_unlocked_returns_true_immediately_without_sleeping() -> None:
    def no_sleep(_seconds: float) -> None:
        raise AssertionError("must not sleep when the first probe already succeeds")

    result = su._wait_files_unlocked([Path("whatever.exe")], probe=lambda _p: True, sleep=no_sleep)
    assert result is True


def test_wait_files_unlocked_times_out_without_hanging() -> None:
    sleeps = []
    result = su._wait_files_unlocked(
        [Path("whatever.exe")],
        timeout_sec=3,
        probe=lambda _p: False,
        sleep=sleeps.append,  # records instead of actually pausing
    )
    assert result is False
    assert len(sleeps) == 3  # polled once/sec for 3s, then gave up -- bounded, not infinite


# --------------------------------------------------------------------------- #
# run_update orchestration (subprocess.run itself never called -- _run is patched)
# --------------------------------------------------------------------------- #


def _tag(cmd: list[str]) -> str:
    """Collapse an argv list to a short subcommand/image tag for order assertions."""
    if cmd[0] == "schtasks":
        return f"schtasks:{cmd[1]}"
    if cmd[0] == "taskkill":
        return f"taskkill:{cmd[2]}"
    return cmd[0]


def _fake_run(rc_overrides: dict):
    calls: list = []

    def fake(cmd: list[str], *, dest: Optional[str] = None, label: str = "") -> int:
        calls.append(cmd)
        return rc_overrides.get(_tag(cmd), 0)

    return fake, calls


def _staged_payload(tmp_path: Path) -> Path:
    """A staging\\payload with dummy content, sibling to a VERSION file."""
    staging = tmp_path / "staging"
    payload = staging / "payload"
    payload.mkdir(parents=True)
    (payload / "srp-agent.exe").write_text("stub", encoding="utf-8")
    (staging / "VERSION").write_text("0.2.0", encoding="utf-8")
    return payload


def _existing_dest(tmp_path: Path) -> Path:
    """A C:\\SRP-like dest that already has the task XML robocopy would refresh."""
    dest = tmp_path / "SRP"
    dest.mkdir()
    (dest / su.TASK_XML).write_text(
        '<?xml version="1.0" encoding="UTF-8"?><Task/>', encoding="utf-8"
    )
    return dest


def test_run_update_orchestration_order_and_exit_ok(tmp_path: Path, monkeypatch) -> None:
    payload = _staged_payload(tmp_path)
    dest = _existing_dest(tmp_path)
    fake_run, calls = _fake_run({})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: True)

    rc = su.run_update(su.SetupOptions(update=True), payload=payload, dest=str(dest))

    assert rc == su.EXIT_OK
    assert [_tag(c) for c in calls] == [
        "schtasks:/end",
        "taskkill:srp-agent.exe",
        "taskkill:srp-tray.exe",
        "icacls",
        "robocopy",
        "schtasks:/create",
        "schtasks:/run",
        "schtasks:/delete",
    ]


def test_run_update_files_never_unlock_is_copy_acl_and_skips_robocopy(
    tmp_path: Path, monkeypatch
) -> None:
    payload = _staged_payload(tmp_path)
    dest = _existing_dest(tmp_path)
    fake_run, calls = _fake_run({})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: False)

    rc = su.run_update(su.SetupOptions(update=True), payload=payload, dest=str(dest))

    assert rc == su.EXIT_COPY_ACL
    assert "robocopy" not in [c[0] for c in calls]


def test_run_update_robocopy_failure_is_copy_acl_before_schtasks_create(
    tmp_path: Path, monkeypatch
) -> None:
    payload = _staged_payload(tmp_path)
    dest = _existing_dest(tmp_path)
    fake_run, calls = _fake_run({"robocopy": su._ROBOCOPY_OK_MAX + 1})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: True)

    rc = su.run_update(su.SetupOptions(update=True), payload=payload, dest=str(dest))

    assert rc == su.EXIT_COPY_ACL
    assert "schtasks:/create" not in [_tag(c) for c in calls]


def test_run_update_icacls_failure_is_copy_acl(tmp_path: Path, monkeypatch) -> None:
    payload = _staged_payload(tmp_path)
    dest = _existing_dest(tmp_path)
    fake_run, calls = _fake_run({"icacls": 1})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: True)

    rc = su.run_update(su.SetupOptions(update=True), payload=payload, dest=str(dest))

    assert rc == su.EXIT_COPY_ACL
    assert "schtasks:/create" not in [_tag(c) for c in calls]


def test_run_update_schtasks_create_failure_is_task_autostart(tmp_path: Path, monkeypatch) -> None:
    payload = _staged_payload(tmp_path)
    dest = _existing_dest(tmp_path)
    fake_run, calls = _fake_run({"schtasks:/create": 1})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: True)

    rc = su.run_update(su.SetupOptions(update=True), payload=payload, dest=str(dest))

    assert rc == su.EXIT_TASK_AUTOSTART
    assert "schtasks:/run" not in [_tag(c) for c in calls]  # never started the recreated task


# --------------------------------------------------------------------------- #
# main() --update branch: no interactive prompt, code passed through
# --------------------------------------------------------------------------- #


def test_main_update_branch_skips_interactive_and_returns_run_update_code(
    tmp_path: Path, monkeypatch
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    monkeypatch.setattr(su, "_payload_dir", lambda: payload)
    received = {}

    def fake_run_update(opts: su.SetupOptions, *, payload: Path, dest: str = su.DEST) -> int:
        received["opts"] = opts
        received["payload"] = payload
        return 42

    monkeypatch.setattr(su, "run_update", fake_run_update)
    monkeypatch.setattr(
        su,
        "_interactive",
        lambda opts: (_ for _ in ()).throw(AssertionError("must not prompt for --update")),
    )

    assert su.main(["--update"]) == 42
    assert received["payload"] == payload
    assert received["opts"].update is True


def test_main_update_missing_payload_dir_is_exit_bad_params(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(su, "_payload_dir", lambda: tmp_path / "missing")
    assert su.main(["--update"]) == su.EXIT_BAD_PARAMS


# --------------------------------------------------------------------------- #
# run_uninstall: агент обязан быть выгружен из памяти (owner-fix 2026-07-12)
# --------------------------------------------------------------------------- #


def test_run_uninstall_unloads_agent_and_tray_before_deregistering(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "SRP"
    dest.mkdir()
    fake_run, calls = _fake_run({})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: True)

    rc = su.run_uninstall(su.SetupOptions(uninstall=True), dest=str(dest))

    assert rc == su.EXIT_OK
    assert [_tag(c) for c in calls] == [
        "schtasks:/end",
        "taskkill:srp-agent.exe",
        "taskkill:srp-tray.exe",
        "schtasks:/delete",
        "reg",
    ]


def test_run_uninstall_purge_proceeds_even_after_unlock_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "SRP"
    dest.mkdir()
    (dest / "config.json").write_text("{}", encoding="utf-8")
    fake_run, calls = _fake_run({})
    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su, "_wait_files_unlocked", lambda *a, **k: False)

    rc = su.run_uninstall(su.SetupOptions(uninstall=True, purge=True), dest=str(dest))

    assert rc == su.EXIT_OK
    assert "taskkill:srp-agent.exe" in [_tag(c) for c in calls]
    assert not dest.exists()
