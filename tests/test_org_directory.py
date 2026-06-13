"""Stage 3 of the tray spec (§7): server-side org/department directory.

Codes live in telemetry; full names live ONLY on the server in
``org_directory.json`` and are decoded render-time (never written to the DB, so
a rename reflects across all history instantly). The file is reloaded on mtime
change; a broken file keeps the last good copy; a missing file is an empty
directory. An unknown code is shown as the code + a "not in directory" chip,
never rejected.
"""

from __future__ import annotations

import json
from pathlib import Path

from server import org_directory as od

_SAMPLE = {
    "organizations": [
        {
            "code": "101",
            "name": "ООО «Ромашка»",
            "departments": [
                {"code": "7", "name": "Бухгалтерия"},
                {"code": "12", "name": "Склад"},
            ],
        },
        {"code": "202", "name": "АО «Восход»", "departments": []},
    ]
}


def _write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Pure decode
# --------------------------------------------------------------------------- #


def test_decode_org_and_dept_names(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    assert d.org_name("101") == "ООО «Ромашка»"
    assert d.org_name("202") == "АО «Восход»"
    assert d.dept_name("101", "7") == "Бухгалтерия"
    assert d.dept_name("101", "12") == "Склад"


def test_unknown_codes_return_none(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    assert d.org_name("999") is None
    assert d.dept_name("101", "99") is None  # unknown dept in known org
    assert d.dept_name("999", "7") is None  # unknown org
    assert d.org_name("") is None
    assert d.dept_name("101", "") is None


def test_codes_are_matched_after_stripping(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    assert d.org_name(" 101 ") == "ООО «Ромашка»"
    assert d.dept_name("101", " 7 ") == "Бухгалтерия"


# --------------------------------------------------------------------------- #
# Display policy (code+chip vs legacy free-text fallback)
# --------------------------------------------------------------------------- #


def test_dept_display_known_code_shows_name_no_chip(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    label = d.dept_display("101", "7", legacy_department="старый текст")
    assert label.text == "Бухгалтерия"
    assert label.known is True


def test_dept_display_unknown_code_shows_code_with_chip(tmp_path: Path) -> None:
    # A code that is NOT in the directory: show the code, flag it (chip) -- the
    # legacy free-text is NOT used to mask a typo'd code.
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    label = d.dept_display("101", "77", legacy_department="Бухгалтерия")
    assert label.text == "77"
    assert label.known is False


def test_dept_display_no_code_falls_back_to_legacy_then_default(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    legacy = d.dept_display("101", "", legacy_department="Отдел кадров")
    assert legacy.text == "Отдел кадров"
    assert legacy.known is True  # free text is not a code -> no chip
    empty = d.dept_display("101", None, legacy_department=None)
    assert empty.text == "Без отдела"
    assert empty.known is True


def test_org_display_known_and_unknown(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    known = d.org_display("101")
    assert known.text == "ООО «Ромашка»" and known.known is True
    unknown = d.org_display("777")
    assert unknown.text == "777" and unknown.known is False
    blank = d.org_display("")
    assert blank.text == "" and blank.known is True  # nothing to flag


# --------------------------------------------------------------------------- #
# File lifecycle: mtime reload, broken file, missing file
# --------------------------------------------------------------------------- #


def test_mtime_reload_picks_up_edits(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    assert d.org_name("101") == "ООО «Ромашка»"

    edited = json.loads(json.dumps(_SAMPLE))
    edited["organizations"][0]["name"] = "ООО «Ромашка-2»"
    _write(path, edited)
    import os

    future = path.stat().st_mtime + 10
    os.utime(path, (future, future))  # guarantee a distinct mtime

    d.reload_if_changed()
    assert d.org_name("101") == "ООО «Ромашка-2»"


def test_broken_json_keeps_last_good_copy(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    d = od.OrgDirectory(path)
    assert d.org_name("101") == "ООО «Ромашка»"

    path.write_text("{ this is not json", encoding="utf-8")
    import os

    future = path.stat().st_mtime + 10
    os.utime(path, (future, future))

    d.reload_if_changed()  # must NOT crash
    assert d.org_name("101") == "ООО «Ромашка»"  # last good copy survives


def test_missing_file_is_empty_directory(tmp_path: Path) -> None:
    d = od.OrgDirectory(tmp_path / "does_not_exist.json")
    assert d.org_name("101") is None
    assert d.dept_name("101", "7") is None
    label = d.dept_display("101", "7", legacy_department="X")
    assert label.text == "7" and label.known is False  # no directory -> unknown code


def test_none_path_is_empty_directory() -> None:
    d = od.OrgDirectory(None)
    assert d.org_name("101") is None
    d.reload_if_changed()  # no-op, no crash


# --------------------------------------------------------------------------- #
# Malformed entries are skipped, never crash
# --------------------------------------------------------------------------- #


def test_malformed_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(
        path,
        {
            "organizations": [
                "not-a-dict",
                {"name": "no code -> skipped"},
                {"code": "300", "name": "ОК", "departments": ["bad", {"code": "9", "name": "Цех"}]},
                {"code": "301"},  # no name -> code maps to None name but org exists
            ]
        },
    )
    d = od.OrgDirectory(path)
    assert d.dept_name("300", "9") == "Цех"
    assert d.org_name("300") == "ОК"
    assert d.org_name("301") is None  # present but unnamed -> treated as no name


def test_non_object_root_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, ["unexpected", "list"])
    d = od.OrgDirectory(path)
    assert d.org_name("101") is None


# --------------------------------------------------------------------------- #
# Module singleton wiring
# --------------------------------------------------------------------------- #


def test_module_singleton_init_and_get(tmp_path: Path) -> None:
    path = tmp_path / "org_directory.json"
    _write(path, _SAMPLE)
    od.init_directory(path)
    assert od.get_directory().org_name("101") == "ООО «Ромашка»"


# --------------------------------------------------------------------------- #
# Render-time decode through the print API + CSV (integration)
# --------------------------------------------------------------------------- #

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _print_env(device_id: str, pages: int, *, org_code: str, dept_code: str) -> dict:
    return {
        "device_id": device_id,
        "agent_version": "0.3.0",
        "msg_type": "print_jobs",
        "org_code": org_code,
        "dept_code": dept_code,
        "payload": {
            "jobs": [
                {
                    "job_id": 1,
                    "ts": "2026-06-12T10:00:00+00:00",
                    "printer": "HP LaserJet",
                    "pages": pages,
                    "source": "events",
                }
            ],
            "window_from": None,
        },
    }


def _activate_directory(tmp_path: Path) -> None:
    """Write the sample directory at the client fixture's path + hot-reload it."""
    _write(tmp_path / "org_directory.json", _SAMPLE)
    od.get_directory().reload_if_changed()


@pytest.mark.integration
def test_analytics_decodes_known_department(client: TestClient, tmp_path: Path) -> None:
    _activate_directory(tmp_path)
    assert (
        client.post(
            "/api/v1/ingest", json=_print_env("d1", 5, org_code="101", dept_code="7")
        ).status_code
        == 200
    )
    body = client.get("/api/v1/fleet/print/analytics?days=0").json()
    acct = next(d for d in body["departments"] if d["dept"] == "Бухгалтерия")
    assert acct["known"] is True
    assert acct["pages"] == 5


@pytest.mark.integration
def test_analytics_unknown_code_is_flagged_not_rejected(client: TestClient, tmp_path: Path) -> None:
    _activate_directory(tmp_path)
    # dept_code 999 is not in the directory -> telemetry still stored, shown as code+chip.
    assert (
        client.post(
            "/api/v1/ingest", json=_print_env("d2", 3, org_code="101", dept_code="999")
        ).status_code
        == 200
    )
    body = client.get("/api/v1/fleet/print/analytics?days=0").json()
    bucket = next(d for d in body["departments"] if d["dept"] == "999")
    assert bucket["known"] is False
    assert bucket["pages"] == 3


@pytest.mark.integration
def test_analytics_merges_same_label_across_devices(client: TestClient, tmp_path: Path) -> None:
    _activate_directory(tmp_path)
    client.post("/api/v1/ingest", json=_print_env("d3", 4, org_code="101", dept_code="7"))
    client.post("/api/v1/ingest", json=_print_env("d4", 6, org_code="101", dept_code="7"))
    body = client.get("/api/v1/fleet/print/analytics?days=0").json()
    accts = [d for d in body["departments"] if d["dept"] == "Бухгалтерия"]
    assert len(accts) == 1  # one merged bucket
    assert accts[0]["pages"] == 10
    assert accts[0]["devices_count"] == 2


@pytest.mark.integration
def test_csv_export_carries_decoded_org_and_dept_names(client: TestClient, tmp_path: Path) -> None:
    _activate_directory(tmp_path)
    client.post("/api/v1/ingest", json=_print_env("d5", 7, org_code="101", dept_code="12"))
    csv_text = client.get("/api/v1/fleet/print/export.csv?days=0").text
    header = csv_text.splitlines()[0]
    assert "org_name" in header and "dept_name" in header
    assert "ООО «Ромашка»" in csv_text
    assert "Склад" in csv_text


# --------------------------------------------------------------------------- #
# PATCH meta: comment supersedes the deprecated free-text department
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_patch_meta_comment_is_stored_and_reflected(client: TestClient, tmp_path: Path) -> None:
    client.post("/api/v1/ingest", json=_print_env("dc1", 1, org_code="101", dept_code="7"))
    r = client.patch("/api/v1/devices/dc1/meta", json={"comment": "касса №3"})
    assert r.status_code == 200
    dev = client.get("/api/v1/devices/dc1").json()
    assert dev["comment"] == "касса №3"


@pytest.mark.integration
def test_patch_meta_department_still_accepted_but_deprecated(
    client: TestClient, tmp_path: Path
) -> None:
    client.post("/api/v1/ingest", json=_print_env("dc2", 1, org_code="101", dept_code="7"))
    r = client.patch("/api/v1/devices/dc2/meta", json={"department": "Старый отдел"})
    assert r.status_code == 200
    dev = client.get("/api/v1/devices/dc2").json()
    assert dev["department"] == "Старый отдел"
