"""P2-2 Ch3: source-staleness loop glue in server/main.py must self-guard a
transient cycle error, same invariant as the printer/netdisco loops (see
tests/test_netdisco_loops.py, same pattern).
"""

from __future__ import annotations

import pytest
from server import main
from server.config import ServerConfig

_CFG = ServerConfig()


def test_source_staleness_glue_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_stale_after_sec: object) -> dict:
        raise RuntimeError("staleness cycle blew up")

    monkeypatch.setattr(main.trust_staleness, "run_staleness_cycle", boom)
    main._run_source_staleness(_CFG)  # must not raise


def test_source_staleness_glue_logs_on_transitions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main.trust_staleness,
        "run_staleness_cycle",
        lambda _stale_after_sec: {"checked": 10, "updated": 2},
    )
    main._run_source_staleness(_CFG)  # exercises the success/log branch


def test_server_config_has_source_staleness_defaults() -> None:
    cfg = ServerConfig()
    assert cfg.source_stale_after_sec == 43200
    assert cfg.source_stale_reeval_interval_sec == 3600
