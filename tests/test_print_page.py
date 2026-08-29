"""/print page shell renders (printview UI rework, Phases 7-11).

The page is JS-driven (data pulled from /fleet/print/*); this pins the SSR shell:
the filter panel, hero chart, detail table and that prefilled filter values are
autoescaped (no reflected XSS via the query string).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_print_page_renders_shell(client: TestClient) -> None:
    h = client.get("/print").text
    for marker in (
        'id="f-from"',
        'id="f-device"',
        'id="f-printer"',
        'id="chart-hero"',
        'id="gran-badge"',
        'id="records-table"',
        "/api/v1/fleet/print",  # API base used by every fetch
        '"/series?"',
        '"/records?"',
        '"/summary?"',
        '"/filter-options?"',
    ):
        assert marker in h, marker


def test_print_page_has_date_presets(client: TestClient) -> None:
    h = client.get("/print").text
    assert 'data-days="30"' in h
    assert 'data-days="0"' in h  # «Всё»


def test_print_page_prefilled_date_is_escaped(client: TestClient) -> None:
    # A reflected query value lands in the date input's value attribute; autoescape
    # must neutralize an injection attempt there (no raw <script>).
    r = client.get('/print?date_from=2026-06-01"><script>alert(1)</script>')
    assert r.status_code == 200
    h = r.text
    assert "<script>alert(1)</script>" not in h
    assert "&lt;script&gt;alert(1)" in h  # escaped form is present instead


def test_print_page_csv_link_present(client: TestClient) -> None:
    h = client.get("/print").text
    assert 'id="csv-link"' in h
    assert "/fleet/print/export.csv" in h


def _post_job(client: TestClient, device_id: str, source: str) -> None:
    from datetime import datetime, timezone

    from tests.conftest import envelope

    job = {
        "job_id": None,
        "ts": datetime.now(timezone.utc).isoformat(),
        "printer": "HP",
        "pages": 2,
        "source": source,
    }
    r = client.post(
        "/api/v1/ingest",
        json=envelope(device_id, "print_jobs", {"jobs": [job], "window_from": None}),
    )
    assert r.status_code == 200


def test_print_page_banner_lists_counter_mode_devices(client: TestClient) -> None:
    """ПК только с counter-печатью (журнал выключен) виден в баннере на /print."""
    _post_job(client, "dev-ctr", "counter")
    h = client.get("/print").text
    assert "Журнал печати" in h
    assert "выключен на 1 ПК" in h


def test_print_page_no_banner_when_events_flow(client: TestClient) -> None:
    _post_job(client, "dev-ok", "events")
    h = client.get("/print").text
    assert "Журнал печати" not in h


# --------------------------------------------------------------------------- #
# o5-D1: CSV-экспорт печати ограничен по строкам и по частоте                  #
# --------------------------------------------------------------------------- #


def test_export_is_row_capped(client: TestClient) -> None:
    """o5-D1: экспорт без потолка читает всю таблицу в память сервера."""
    from server import db

    with db._connect() as conn:
        conn.executemany(
            "INSERT INTO print_jobs (device_id, job_id, ts, received_at, printer, pages)"
            " VALUES (?,?,?,?,?,?)",
            [
                ("cap-dev", None, "2026-03-01T10:00:00+00:00", "2026-03-01T10:00:00+00:00", "HP", 1)
                for _ in range(db._EXPORT_ROW_MAX + 10)
            ],
        )
        conn.commit()
    assert len(db.export_print_rows(db.PrintFilter())) == db._EXPORT_ROW_MAX


def test_export_is_rate_limited(client: TestClient, monkeypatch) -> None:
    """o5-D1: экспорт — самый дорогой чтение-эндпоинт, он должен иметь тот же
    лимит частоты, что и остальные тяжёлые операции."""
    from server import ingest_guards

    monkeypatch.setattr(ingest_guards, "_RATE_MAX_PER_WINDOW", 1)
    ingest_guards.reset_guards()
    assert client.get("/api/v1/fleet/print/export.csv").status_code == 200
    assert client.get("/api/v1/fleet/print/export.csv").status_code == 429
