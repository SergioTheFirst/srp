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
