"""P0-6 (stoperrors.md): ``get_fleet_cohort_stats`` percentages must exclude
devices that never reported a field from the denominator, not count them as
"healthy" (0.0). A device with no ``bugchecks_30d``/``kernel_power_41_30d``/
``reliability_stability_index`` in its historical payload (old agent, or the
field genuinely never sampled) carries no evidence either way.

The plan's own suggested fix (``AND json_extract(...) IS NOT NULL`` inside the
``CASE WHEN``) turned out to be a no-op verified against a live SQLite: SQL
three-valued logic already sends a NULL comparison to ``ELSE`` on its own, so
the guard changes nothing. The real fix drops the trailing ``ELSE`` so the
``CASE`` itself returns SQL NULL for a non-reporting device, which ``AVG()``
then genuinely excludes.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _seed(db_init, device_id: str, model: str, site_code: str, payload: dict) -> None:
    db_init.upsert_device(
        device_id, "2026-07-01T00:00:00Z", "1.0.0", model=model, site_code=site_code
    )
    db_init.store_historical(device_id, "2026-07-01T00:00:00Z", payload)


def _seed_trajectory(db_init, device_id: str, model: str, trajectory: dict) -> None:
    """B6 6.3: a device with a model + one persisted trend snapshot."""
    db_init.upsert_device(device_id, "2026-07-01T00:00:00Z", "1.0.0", model=model)
    db_init.store_scores(device_id, "2026-07-01T00:00:00Z", {"risk": {"trajectory": trajectory}})


def _worsening(slope: float) -> dict:
    return {"direction": "worsening", "slope_per_day": slope}


# --------------------------------------------------------------------------- #
# B6 6.3: get_cohort_slope_verdict -- "worse than most of the model cohort".
# Slopes already ride inside scores.risk.trajectory (pipeline.py's
# compute_trends result via db.store_scores); this only reads that existing
# data, no new storage.
# --------------------------------------------------------------------------- #
def test_cohort_slope_verdict_true_when_faster_than_majority(db_init):
    _seed_trajectory(db_init, "me", "OptiPlex", {"storage_wear": _worsening(5.0)})
    for i, slope in enumerate([1.0, 1.0, 1.0, 10.0]):  # 3 of 4 slower than me
        _seed_trajectory(db_init, f"peer{i}", "OptiPlex", {"storage_wear": _worsening(slope)})

    verdict = db_init.get_cohort_slope_verdict("OptiPlex", {"storage_wear": _worsening(5.0)})

    assert verdict == {"storage_wear": True}


def test_cohort_slope_verdict_absent_when_not_faster_than_majority(db_init):
    """Slower than (or equal to) most of the cohort -> key omitted, never False."""
    _seed_trajectory(db_init, "me", "OptiPlex", {"storage_wear": _worsening(1.0)})
    for i, slope in enumerate([5.0, 5.0, 5.0, 10.0]):  # all 4 faster than me
        _seed_trajectory(db_init, f"peer{i}", "OptiPlex", {"storage_wear": _worsening(slope)})

    verdict = db_init.get_cohort_slope_verdict("OptiPlex", {"storage_wear": _worsening(1.0)})

    assert verdict == {}


def test_cohort_slope_verdict_thin_cohort_is_absent_not_false(db_init):
    """Fewer than _COHORT_SLOPE_MIN_N comparable cohort points -> no signal,
    not a false "no" (UNKNOWN over false confidence)."""
    _seed_trajectory(db_init, "me", "OptiPlex", {"storage_wear": _worsening(5.0)})
    _seed_trajectory(db_init, "peer0", "OptiPlex", {"storage_wear": _worsening(0.1)})

    verdict = db_init.get_cohort_slope_verdict("OptiPlex", {"storage_wear": _worsening(5.0)})

    assert verdict == {}


def test_cohort_slope_verdict_ignores_non_worsening_metrics(db_init):
    trajectory = {"storage_wear": {"direction": "flat", "slope_per_day": 5.0}}
    for i in range(3):
        _seed_trajectory(db_init, f"peer{i}", "OptiPlex", {"storage_wear": _worsening(0.1)})

    verdict = db_init.get_cohort_slope_verdict("OptiPlex", trajectory)

    assert verdict == {}  # not worsening for THIS device -> never compared


def test_cohort_slope_verdict_skips_zero_slope_worsening_counter(db_init):
    """A counter step is 'worsening' with slope_per_day 0 (calibration contract
    C1): there is no rate to compare, so it must never read as 'faster than the
    cohort' -- a sign() of 0 used to rank every positive-slope peer as slower."""
    _seed_trajectory(db_init, "me", "OptiPlex", {"smart_pending": _worsening(0.0)})
    for i in range(4):
        _seed_trajectory(db_init, f"peer{i}", "OptiPlex", {"smart_pending": _worsening(1.0)})

    verdict = db_init.get_cohort_slope_verdict("OptiPlex", {"smart_pending": _worsening(0.0)})

    assert verdict == {}


def test_cohort_slope_verdict_survives_a_malformed_cohort_row(db_init):
    """One neighbour's corrupted risk JSON must not cost every metric on the
    page its cohort comparison ([[sqlite-json-extract-throws]] -- side-stepped
    by parsing in Python via _load_risk, never json_extract in SQL)."""
    from server import db

    _seed_trajectory(db_init, "me", "OptiPlex", {"storage_wear": _worsening(5.0)})
    for i, slope in enumerate([1.0, 1.0, 1.0]):
        _seed_trajectory(db_init, f"peer{i}", "OptiPlex", {"storage_wear": _worsening(slope)})
    db_init.upsert_device("bad-peer", "2026-07-01T00:00:00Z", "1.0.0", model="OptiPlex")
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO scores (device_id, ts, risk) VALUES (?,?,?)",
            ("bad-peer", "2026-07-01T00:00:00Z", "{not valid json"),
        )

    verdict = db_init.get_cohort_slope_verdict("OptiPlex", {"storage_wear": _worsening(5.0)})

    assert verdict == {"storage_wear": True}  # the 3 good peers still decide it


# --------------------------------------------------------------------------- #
# security-review MEDIUM (fix-round-1, same shape as the boot-p90 fix above):
# the cohort sample must be the freshest scores, not whichever 200 devices
# sort first alphabetically by device_id.
# --------------------------------------------------------------------------- #
def test_cohort_slope_verdict_samples_freshest_peers_not_alphabetical_order(db_init):
    # 101 "worse" peers (slope 1.0 < my 5.0) + 99 "not worse" peers (slope 10.0)
    # + "zzz" (not worse, 10.0) = 201 total, cap 200. The FIRST-inserted (oldest
    # by scores.id) peer is a WORSE one; "zzz" (not worse) sorts alphabetically
    # last. This flips the >0.5 majority threshold between the two orderings.
    _seed_trajectory(db_init, "peer-worse-000", "OptiPlex", {"storage_wear": _worsening(1.0)})
    for i in range(1, 101):
        _seed_trajectory(
            db_init, f"peer-worse-{i:03d}", "OptiPlex", {"storage_wear": _worsening(1.0)}
        )
    for i in range(99):
        _seed_trajectory(
            db_init, f"peer-fast-{i:03d}", "OptiPlex", {"storage_wear": _worsening(10.0)}
        )
    _seed_trajectory(db_init, "zzz", "OptiPlex", {"storage_wear": _worsening(10.0)})

    verdict = db_init.get_cohort_slope_verdict("OptiPlex", {"storage_wear": _worsening(5.0)})

    # Alphabetical-200 (drops "zzz", a not-worse peer): 101/200 = 0.505 -> True
    # (the old bug). Freshest-200 (drops the oldest insertion, a worse peer):
    # 100/200 = 0.50, not > 0.5 -> {}.
    assert verdict == {}


def test_cohort_slope_verdict_no_model_is_empty(db_init):
    assert db_init.get_cohort_slope_verdict(None, {"storage_wear": _worsening(5.0)}) == {}


def test_bsod_pct_excludes_non_reporting_devices(db_init):
    _seed(db_init, "d1", "OptiPlex", "site-a", {"bugchecks_30d": 3})  # reports, bad
    for i in range(3):
        # old agent -- field genuinely absent from the payload, not 0
        _seed(db_init, f"d-old-{i}", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", None)

    assert stats["cohort_size"] == 4  # all 4 devices have SOME historical data
    # Buggy pre-fix result: 1/4 = 0.25 (non-reporters wrongly counted healthy).
    assert stats["cohort_bsod_pct"] == 1.0  # 1 of 1 REPORTING device is bad


def test_kp41_pct_excludes_non_reporting_devices_cohort_and_site(db_init):
    _seed(db_init, "d1", "OptiPlex", "site-a", {"kernel_power_41_30d": 5})
    for i in range(3):
        _seed(db_init, f"d-old-{i}", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", "site-a")

    assert stats["cohort_kp41_pct"] == 1.0
    assert stats["site_kp41_pct"] == 1.0


def test_rsi_low_pct_excludes_non_reporting_devices(db_init):
    """The plan cites this metric as the ALREADY-correct reference pattern to
    copy -- it turned out to have the identical no-op bug, verified live."""
    _seed(db_init, "d1", "OptiPlex", "site-a", {"reliability_stability_index": 2.0})
    for i in range(3):
        _seed(db_init, f"d-old-{i}", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", None)

    assert stats["cohort_rsi_low_pct"] == 1.0


# --------------------------------------------------------------------------- #
# KodSR L14: _cohort_boot_p90 must sample the freshest readings, not whichever
# 200 devices happen to sort first alphabetically by device_id.
# --------------------------------------------------------------------------- #
def test_cohort_boot_p90_samples_freshest_readings_not_alphabetical_order(db_init):
    # 200 devices "dev-000".."dev-199" seeded FIRST (oldest by insertion/h.id),
    # each with a distinct avg_boot_ms; "zzz" seeded LAST (freshest) sorts after
    # all of them alphabetically, so an ORDER BY device_id LIMIT 200 always
    # drops it regardless of how recently it reported.
    for i in range(200):
        _seed(db_init, f"dev-{i:03d}", "OptiPlex", "site-a", {"avg_boot_ms": (i + 1) * 100})
    _seed(db_init, "zzz", "OptiPlex", "site-a", {"avg_boot_ms": 20100})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", None)

    # Freshest-200 (excludes the oldest insertion "dev-000", keeps "zzz"):
    # sorted values are 200..20000 (dev-001..dev-199) + 20100 (zzz); p90 (rank
    # 180 of 200) lands on 18100. Alphabetical-200 (excludes "zzz" instead)
    # would give 18000 -- the old, wrong answer.
    assert stats["boot_p90_ms"] == 18100.0


def test_mixed_reporting_and_non_reporting_devices(db_init):
    """Sanity: normal mixed-fleet math still works once non-reporters are
    excluded (not just the all-or-nothing edge cases above)."""
    _seed(db_init, "d-bad", "OptiPlex", "site-a", {"bugchecks_30d": 2})
    _seed(db_init, "d-good", "OptiPlex", "site-a", {"bugchecks_30d": 0})
    _seed(db_init, "d-old", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", None)

    assert stats["cohort_size"] == 3
    assert stats["cohort_bsod_pct"] == 0.5  # 1 bad of 2 REPORTING devices


def test_no_cohort_devices_is_zero_not_error(db_init):
    stats = db_init.get_fleet_cohort_stats("NoSuchModel", None)
    assert stats["cohort_size"] == 0
    assert stats["cohort_bsod_pct"] == 0.0


def _cohort_sql(monkeypatch) -> list[str]:
    """Собрать SQL, который когортные запросы реально выполняют."""
    import contextlib

    from server import db

    seen: list[str] = []
    real_connect = db._connect

    class _Rec:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a, **kw):
            seen.append(sql)
            return self._c.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._c, name)

    @contextlib.contextmanager
    def _wrapped():
        with real_connect() as conn:
            yield _Rec(conn)

    monkeypatch.setattr(db, "_connect", _wrapped)
    return seen


def test_cohort_query_uses_model_index(tmp_path, monkeypatch) -> None:
    """o5-D7: производная таблица `MAX(id) GROUP BY device_id` строит свод по ВСЕЙ
    истории парка ради когорты из нескольких машин, и `devices.model` не
    проиндексирован. Ведущей должна быть devices по индексу."""
    from server import db

    db.init_db(str(tmp_path / "cohort.db"))
    with db._connect() as conn:
        for i in range(50):
            did = f"c{i}"
            conn.execute(
                "INSERT INTO devices (device_id, hostname, model, site_code) VALUES (?,?,?,?)",
                (did, did, "OptiPlex", "S1"),
            )
            conn.executemany(
                "INSERT INTO historical (device_id, ts, payload, received_at) VALUES (?,?,?,?)",
                [
                    (did, f"2026-03-01T10:{j:02d}:00+00:00", '{"avg_boot_ms": 20000}', "2026-03-01")
                    for j in range(50)
                ],
            )
        conn.commit()

    seen = _cohort_sql(monkeypatch)
    stats = db.get_fleet_cohort_stats("OptiPlex", "S1")
    assert stats["cohort_size"] == 50  # регрессионный якорь: значения не изменились

    sqls = [q for q in seen if "d.model = ?" in q or "d.site_code = ?" in q]
    assert sqls, "когортные запросы не выполнялись"
    for sql in sqls:
        assert "GROUP BY device_id" not in sql, "свод по всей истории парка остался"
    with db._connect() as conn:
        plans = []
        for sql in sqls:
            args = tuple(["OptiPlex"] * sql.count("?"))  # число плейсхолдеров разное
            plans += [str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
    plan = " ".join(plans)
    assert "idx_devices_model" in plan or "idx_devices_site" in plan


# --------------------------------------------------------------------------- #
# H15 review: a device never sets its own slow-boot bar, nor does a gate-failed peer
# --------------------------------------------------------------------------- #
def _seed_boot(db, did: str, boot: int) -> None:
    db.upsert_device(did, "2026-07-01T00:00:00Z", "1.0.0", model="OptiPlex", site_code="s")
    db.store_historical(did, "2026-07-01T00:00:00Z", {"avg_boot_ms": boot})


def test_cohort_boot_p90_excludes_the_scored_device_and_implausible_peers(db_init):
    from server.scoring.scores import boot_threshold_ms

    for i, boot in enumerate([120_000] * 5 + [7_200_000]):  # > 600 s: boot_time is SUSPECT
        _seed_boot(db_init, f"peer{i}", boot)
    _seed_boot(db_init, "me", 400_000)
    stats = db_init.get_fleet_cohort_stats("OptiPlex", None, exclude_device_id="me")
    assert (stats["boot_p90_ms"], stats["boot_n"]) == (120_000.0, 5)
    assert boot_threshold_ms(stats) == 180_000.0  # 400 s >= 2T -- very slow


def test_the_cohort_boot_bound_is_the_boot_time_gate(db_init):
    from server.trust.validators import BOOT_MAX_MS

    assert db_init._BOOT_PLAUSIBLE_MAX_MS == BOOT_MAX_MS


def test_slowest_pc_of_a_five_pc_cohort_is_still_flagged(client, monkeypatch):
    """Four peers boot in 120 s, the scored PC in 400 s with RSI 4.5: h1, not a false h0
    (with itself in the sample the p90 was its own 400 s and T 600 s)."""
    from server import limits
    from server.pipeline import recompute_scores

    # five PCs: the public demo build caps ingest at 3 (export patch), the cohort needs 5
    monkeypatch.setattr(limits, "MAX_DEVICES", None)

    from tests.conftest import envelope, healthy

    def ingest(did: str, boot: int, rsi: float) -> None:
        for mt in ("inventory", "historical", "heartbeat"):
            p = healthy(mt)
            if mt == "inventory":  # five PCs, five names: X1 folds namesakes into one vote
                p["hostname"] = did
            if mt == "historical":
                p.update(avg_boot_ms=boot, reliability_stability_index=rsi)
            assert client.post("/api/v1/ingest", json=envelope(did, mt, p)).status_code == 200

    for i in range(4):
        ingest(f"peer{i}", 120_000, 9.6)
    ingest("me", 400_000, 4.5)
    s = recompute_scores("me")
    assert s["risk"]["score100"]["os_degradation_risk"]["value"] == 34.0  # RSI 22 + boot 12
    assert s["performance"] == 85.0
    assert s["risk"]["health"]["state"] == "h1"


def test_bsod_fleet_cause_counts_machines_not_size_times_share(db_init):
    """H13: a failed historical cycle (payload {}) is in cohort_size but not in the BSOD
    share -- 2 BSOD machines among 4 reporters of 10 are 2, not 10 x 0.5 = 5."""
    from server.analytics.stability import bsod_cause

    for i, bc in enumerate([2, 1, 0, 0]):
        _seed(db_init, f"r{i}", "OptiPlex", "S1", {"bugchecks_30d": bc})
    for i in range(6):
        _seed(db_init, f"s{i}", "OptiPlex", "S1", {})  # a failed historical cycle
    stats = db_init.get_fleet_cohort_stats("OptiPlex", None, exclude_device_id="r0")
    assert (stats["cohort_size"], stats["cohort_bsod_pct"], stats["cohort_bsod_n"]) == (10, 0.5, 2)
    assert bsod_cause([], stats, None) == "unknown"
    _seed(db_init, "s0", "OptiPlex", "S1", {"bugchecks_30d": 3})  # a third BSOD machine
    assert bsod_cause([], db_init.get_fleet_cohort_stats("OptiPlex", None), None) == "fleet_likely"


# --------------------------------------------------------------------------- #
# X1 (2026-11-02): a row superseded by a reinstall is no cohort peer -- one PC, one vote
# --------------------------------------------------------------------------- #
def _seed_reinstalled_me(db) -> None:
    """'me' (fresh) supersedes 'me-old' (same hostname, silent for 2 h, born 30 d ago),
    whose stale slow boot, BSOD and disk error would otherwise count as a peer's."""
    for did, payload in (
        ("me-old", {"avg_boot_ms": 590_000, "bugchecks_30d": 2}),
        ("me", {"avg_boot_ms": 400_000, "bugchecks_30d": 0}),
    ):
        ts = "2026-07-01T00:00:00Z"
        db.upsert_device(did, ts, "1.0.0", hostname="PC-ME", model="OptiPlex", site_code="s")
        db.store_historical(did, ts, payload)
    bad_block = {"ts": "2026-07-01T00:00:00Z", "log": "System", "source": "disk", "event_id": 7}
    db.store_events("me-old", [{**bad_block, "level": 2}])
    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET first_seen=datetime('now','-30 days'),"
            " last_seen=datetime('now','-2 hours') WHERE device_id='me-old'"
        )


def test_cohort_skips_rows_superseded_by_a_reinstall(db_init):
    from server.scoring.scores import boot_threshold_ms

    for i in range(5):
        _seed_boot(db_init, f"peer{i}", 120_000)
    _seed_reinstalled_me(db_init)
    assert db_init.superseded_ids() == frozenset({"me-old"})
    stats = db_init.get_fleet_cohort_stats("OptiPlex", "s", exclude_device_id="me")
    sizes = (stats["cohort_size"], stats["site_size"])
    counts = (stats["cohort_bsod_n"], stats["cohort_disk_err_n"])
    assert (sizes, counts) == ((6, 6), (0, 0))
    # H15: its own ghost no longer sets the PC's bar (nearest-rank p90 of 6 was the ghost)
    assert (stats["boot_p90_ms"], stats["boot_n"]) == (120_000.0, 5)
    assert boot_threshold_ms(stats) == 180_000.0  # 400 s >= 2T -- very slow, not hidden


def test_cohort_slope_verdict_skips_superseded_rows(db_init):
    """A ghost's frozen fast slope is no cohort point: with it 'me' (5.0) beats 2 of 4 (no
    verdict), without it 2 of 3 -- faster than most of the real cohort."""
    for did, slope in (("slow0", 0.1), ("slow1", 0.1), ("fast0", 9.0)):
        _seed_trajectory(db_init, did, "OptiPlex", {"disk_fill": _worsening(slope)})
    _seed_reinstalled_me(db_init)
    db_init.store_scores(
        "me-old", "2026-07-01T00:00:00Z", {"risk": {"trajectory": {"disk_fill": _worsening(9.0)}}}
    )
    verdict = db_init.get_cohort_slope_verdict("OptiPlex", {"disk_fill": _worsening(5.0)})
    assert verdict == {"disk_fill": True}
