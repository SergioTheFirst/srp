"""Phase 5: netdisco discovery-loop glue must self-guard a transient scan error.

The background loop runs an active scan; a transient cycle failure can never be
allowed to crash startup or kill the loop (same invariant as the printer/
inventory loops). These cover the thin main.py glue around run_discovery_cycle.
"""

from __future__ import annotations

import pytest
from server import main
from server.config import ServerConfig

_CFG = ServerConfig(netdisco={"active_scan": True})


def test_discovery_cycle_glue_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_cfg: object) -> dict:
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(main.netdisco_scheduler, "run_discovery_cycle", boom)
    main._run_netdisco_discovery_cycle(_CFG)  # must not raise


def test_discovery_cycle_glue_logs_on_new_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main.netdisco_scheduler,
        "run_discovery_cycle",
        lambda _cfg: {"discovered": 2, "scanned": 5, "active": 1, "busy": 0},
    )
    main._run_netdisco_discovery_cycle(_CFG)  # exercises the success/log branch


# --- Phase 6: classify-cycle glue must self-guard too ---


def test_classify_cycle_glue_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_cfg: object) -> dict:
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(main.netdisco_scheduler, "run_classify_cycle", boom)
    main._run_netdisco_classify_cycle(_CFG)  # must not raise


def test_classify_cycle_glue_logs_on_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main.netdisco_scheduler,
        "run_classify_cycle",
        lambda _cfg: {"classified": 3, "probed": 5, "busy": 0},
    )
    main._run_netdisco_classify_cycle(_CFG)  # exercises the success/log branch
