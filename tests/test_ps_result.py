"""run_ps status mapping (client/collectors/ps.py) -- no real PowerShell.

subprocess.run is monkeypatched so these stay fast and deterministic; they pin
the contract that a failed PowerShell call reports *why* (absent/timeout/blocked/
empty/partial) instead of collapsing to None.
"""

from __future__ import annotations

import subprocess

import pytest
from client.collectors.ps import PsResult, run_ps

pytestmark = pytest.mark.unit


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _patch_run(monkeypatch, *, raises=None, stdout=b"", rc=0, stderr=b""):
    def fake_run(*args, **kwargs):
        if raises is not None:
            raise raises
        return _Proc(stdout, rc, stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_absent_when_powershell_missing(monkeypatch):
    _patch_run(monkeypatch, raises=FileNotFoundError())
    assert run_ps("x").status == "absent"


def test_timeout(monkeypatch):
    _patch_run(monkeypatch, raises=subprocess.TimeoutExpired(cmd="ps", timeout=1))
    assert run_ps("x").status == "timeout"


def test_blocked_on_oserror(monkeypatch):
    _patch_run(monkeypatch, raises=OSError("policy"))
    assert run_ps("x").status == "blocked"


def test_empty_output(monkeypatch):
    _patch_run(monkeypatch, stdout=b"   ")
    assert run_ps("x").status == "empty"


def test_partial_on_bad_json(monkeypatch):
    _patch_run(monkeypatch, stdout=b"not json {")
    assert run_ps("x").status == "partial"


def test_ok_with_parsed_data(monkeypatch):
    _patch_run(monkeypatch, stdout=b'{"a": 1}')
    result = run_ps("x")
    assert result.status == "ok"
    assert result.data == {"a": 1}


def test_psresult_default_data_is_none():
    assert PsResult("empty").data is None


def test_run_ps_spawns_windowless(monkeypatch):
    """Every PowerShell spawn must be invisible -- no console window may flash on
    the user's desktop (agent sweeps run as SYSTEM, but the per-user tray cert
    check runs in the interactive session). CREATE_NO_WINDOW on Windows; 0
    (harmless default) on other platforms.
    """
    seen: dict = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return _Proc(b"{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_ps("x")
    assert seen.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# Агент 0.3.5: причина пустого вывода (байты -- из живых прогонов 2026-10-24)
# --------------------------------------------------------------------------- #
def test_legit_empty_with_rc0_has_no_reason(monkeypatch):
    _patch_run(monkeypatch, stdout=b"", rc=0)
    result = run_ps("x")
    assert (result.status, result.rc, result.reason) == ("empty", 0, None)


@pytest.mark.parametrize(
    ("stderr", "rc", "reason"),
    [
        (b"SRPERR:CommandNotFoundException\r\n", 1, "CommandNotFoundException"),
        (
            b"#< CLIXML\r\nSRPERR:PathNotFound,Microsoft.PowerShell.Commands.GetItemCommand\r\n<Objs",
            1,
            "PathNotFound",
        ),  # маркер не на нулевом смещении, если до него был показан warning
        (b"SRPERR:\r\n", 1, "rc_1"),  # native exe / -EA Ignore: $Error пуст
        (b"", 1, "rc_1"),  # CLM / parse error: маркера нет
        ("SRPERR:бум, x\r\n".encode("utf-8"), 1, "rc_1"),  # throw с кириллицей
        (b"Unhandled Exception: System.BadImageFormatException", -532462766, "rc_-532462766"),
    ],
)
def test_broken_run_reports_reason_token(monkeypatch, stderr, rc, reason):
    _patch_run(monkeypatch, stdout=b"", rc=rc, stderr=stderr)
    result = run_ps("x")
    assert (result.status, result.reason) == ("empty", reason)


def test_false_marker_in_clixml_echo_is_not_a_token(monkeypatch):
    """PS эхом печатает текст скрипта в CLIXML -- там «'SRP'+'ERR:'», не «SRPERR:»."""
    echo = b"#< CLIXML\r\n<Objs><S S=\"Error\">...WriteLine('SRP'+'ERR:'+$Error[0]...</S></Objs>"
    _patch_run(monkeypatch, stdout=b"", rc=1, stderr=echo)
    assert run_ps("x").reason == "rc_1"


def test_valid_json_with_nonzero_rc_stays_ok(monkeypatch):
    """rc != 0 рядом с валидным JSON -- не сбой (живой прогон S-i)."""
    _patch_run(monkeypatch, stdout=b'{"a": 1}', rc=1, stderr=b"SRPERR:PathNotFound\r\n")
    result = run_ps("x")
    assert (result.status, result.data, result.reason) == ("ok", {"a": 1}, None)


def test_postamble_wraps_every_script(monkeypatch):
    import base64

    from client.collectors.ps import _POSTAMBLE, _PREAMBLE

    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["script"] = base64.b64decode(argv[-1]).decode("utf-16-le")
        return _Proc(b"{}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_ps("@{a=1} | ConvertTo-Json # хвостовой комментарий")
    assert seen["script"].startswith(_PREAMBLE)
    assert seen["script"].endswith(_POSTAMBLE)
    assert _POSTAMBLE.startswith("\n")  # иначе комментарий съест постамбулу
    assert "SRPERR:" not in _POSTAMBLE  # маркер собирается в рантайме
