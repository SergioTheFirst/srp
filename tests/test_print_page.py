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
