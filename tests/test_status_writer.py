"""Stage 1 of the tray spec: agent-side status.json + print daily counters.

The tray process reads ``status.json`` (one-way IPC, spec §1). These tests pin:
the document contract, the no-secrets invariant, atomic writes, RFC1918-only IP
filtering, and the rolling today/month print counters kept in print_state.json.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from client.collectors.print_jobs import accumulate_daily, read_print_counters
from client.config import ClientConfig
from client.status_writer import (
    build_status,
    candidate_ips,
    filter_lan_ips,
    publish_status,
    write_status,
)

# --------------------------------------------------------------------------- #
# IP filtering (pure)
# --------------------------------------------------------------------------- #


def test_filter_lan_ips_keeps_rfc1918_order_and_dedups() -> None:
    candidates = [
        "127.0.0.1",  # loopback -> drop
        "10.1.2.3",  # keep
        "8.8.8.8",  # public -> drop
        "192.168.1.5",  # keep
        "169.254.1.7",  # link-local -> drop
        "fe80::1",  # IPv6 -> drop (panel shows IPv4 LAN addresses)
        "172.16.0.9",  # keep
        "10.1.2.3",  # duplicate -> dropped, order preserved
    ]
    assert filter_lan_ips(candidates) == ["10.1.2.3", "192.168.1.5", "172.16.0.9"]


def test_filter_lan_ips_ignores_garbage() -> None:
    assert filter_lan_ips(["not-an-ip", "", "999.1.1.1", "192.168.0.2"]) == ["192.168.0.2"]


def test_candidate_ips_never_raises() -> None:
    # Unroutable TEST-NET target and an empty URL must both degrade to a list.
    assert isinstance(candidate_ips("http://203.0.113.1:9"), list)
    assert isinstance(candidate_ips(""), list)


# --------------------------------------------------------------------------- #
# build_status (pure assembly)
# --------------------------------------------------------------------------- #


def _cfg(**kw: object) -> ClientConfig:
    base = {
        "server_url": "http://user:pass@192.168.1.10:8000",
        "device_id": "dev-1",
        "org_code": "101",
        "dept_code": "7",
        "ingest_token": "super-secret-token",
        "config_password_hash": "pbkdf2:sha256:260000:aa:bb",
    }
    base.update(kw)
    return ClientConfig(**base)  # type: ignore[arg-type]


def _doc() -> dict:
    return build_status(
        cfg=_cfg(),
        now=1781234567.9,
        hostname="PC-042",
        ips=["192.168.1.42"],
        last_ok_ts=1781234500.2,
        last_error="",
        buffer_depth=3,
        print_counters={"today": 14, "month": 312, "mode": "events"},
        disk_free_gb=41.2,
        uptime_days=3.5,
    )


def test_build_status_has_contract_fields() -> None:
    doc = _doc()
    assert doc["ts"] == 1781234567
    assert doc["last_send_ok_ts"] == 1781234500
    assert doc["last_send_error"] == ""
    assert doc["buffer_depth"] == 3
    assert doc["hostname"] == "PC-042"
    assert doc["ips"] == ["192.168.1.42"]
    assert doc["org_code"] == "101"
    assert doc["dept_code"] == "7"
    assert doc["print_today_pages"] == 14
    assert doc["print_month_pages"] == 312
    assert doc["print_mode"] == "events"
    assert doc["disk_free_gb"] == 41.2
    assert doc["uptime_days"] == 3.5
    assert isinstance(doc["agent_version"], str) and doc["agent_version"]


def test_build_status_never_leaks_secrets() -> None:
    raw = json.dumps(_doc(), ensure_ascii=False)
    assert "super-secret-token" not in raw
    assert "pbkdf2" not in raw
    assert "user:pass" not in raw  # userinfo from server_url must not appear
    assert "ingest_token" not in raw
    assert "password" not in raw


# --------------------------------------------------------------------------- #
# write_status (atomic, never raises)
# --------------------------------------------------------------------------- #


def test_write_status_atomic_and_parseable(tmp_path: Path) -> None:
    target = tmp_path / "status.json"
    write_status(target, {"ts": 1, "hostname": "PC"})
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["hostname"] == "PC"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "status.json"]
    assert leftovers == []  # no tmp debris


def test_write_status_swallows_oserror(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    # Parent path is a *file*: mkdir/replace must fail -> swallowed, no raise.
    write_status(blocker / "status.json", {"ts": 1})


# --------------------------------------------------------------------------- #
# Print daily counters (pure, owned by print_jobs state)
# --------------------------------------------------------------------------- #


def test_accumulate_daily_sums_today_and_prunes() -> None:
    state = {"daily": {"2026-06-11": 5, "2026-03-01": 99}}
    jobs = [{"pages": 3}, {"pages": 4}, {"pages": None}]
    out = accumulate_daily(state, jobs, "2026-06-12")
    assert out["daily"]["2026-06-12"] == 7
    assert out["daily"]["2026-06-11"] == 5
    assert "2026-03-01" not in out["daily"]  # older than 62 days -> pruned
    assert state["daily"] == {"2026-06-11": 5, "2026-03-01": 99}  # input not mutated


def test_accumulate_daily_adds_to_existing_today() -> None:
    out = accumulate_daily({"daily": {"2026-06-12": 10}}, [{"pages": 2}], "2026-06-12")
    assert out["daily"]["2026-06-12"] == 12


def test_read_print_counters_month_boundary(tmp_path: Path) -> None:
    state_path = tmp_path / "print_state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_sweep_ts": "2026-06-12T08:00:00+00:00",
                "daily": {"2026-05-31": 5, "2026-06-01": 7, "2026-06-12": 14},
            }
        ),
        encoding="utf-8",
    )
    counters = read_print_counters(state_path, today=date(2026, 6, 12))
    assert counters["today"] == 14
    assert counters["month"] == 21  # May entry excluded
    assert counters["mode"] == "events"  # default until stage 2 introduces modes


def test_read_print_counters_missing_file(tmp_path: Path) -> None:
    counters = read_print_counters(tmp_path / "absent.json", today=date(2026, 6, 12))
    assert counters == {"today": 0, "month": 0, "mode": "events"}


# --------------------------------------------------------------------------- #
# publish_status (orchestrator)
# --------------------------------------------------------------------------- #


def test_publish_status_writes_next_to_buffer(tmp_path: Path) -> None:
    cfg = _cfg(buffer_path=str(tmp_path / "buffer.jsonl"))
    transport = SimpleNamespace(last_ok_ts=1781234500.0, last_error="", buffer_depth=lambda: 2)
    publish_status(cfg, transport, tmp_path / "print_state.json")
    raw = (tmp_path / "status.json").read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert doc["buffer_depth"] == 2
    assert doc["hostname"]  # real hostname of the test box
    # Pin the full written file, not just the in-memory doc: a future cfg.*
    # field added to build_status must not leak any of these.
    for forbidden in ("super-secret-token", "pbkdf2", "user:pass", "ingest_token", "password"):
        assert forbidden not in raw, f"secret leaked to status.json: {forbidden!r}"
