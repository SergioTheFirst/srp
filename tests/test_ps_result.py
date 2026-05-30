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
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout


def _patch_run(monkeypatch, *, raises=None, stdout=b""):
    def fake_run(*args, **kwargs):
        if raises is not None:
            raise raises
        return _Proc(stdout)

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
