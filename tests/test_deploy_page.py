"""Stage 7 (tray spec §7): the /deploy BAT-command generator page.

An admin picks an org/dept from the directory and gets a ready setup.exe command
with the right codes + server URL, but with ``<ПАРОЛЬ>``/``<ТОКЕН>`` placeholders
-- the open (auth-less) dashboard never holds real secrets. Kills code typos at
the source. Read-only: no writes, reflects org_directory + the request host.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from server import org_directory as od

pytestmark = pytest.mark.integration

_SAMPLE = {
    "organizations": [
        {
            "code": "101",
            "name": "ООО «Ромашка»",
            "departments": [{"code": "7", "name": "Бухгалтерия"}, {"code": "12", "name": "Склад"}],
        },
        {"code": "202", "name": "АО «Восход»", "departments": []},
    ]
}


def _write_dir(tmp_path: Path, data: object) -> None:
    (tmp_path / "org_directory.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _island(body: str) -> list:
    m = re.search(r'<script id="deploy-data" type="application/json">(.*?)</script>', body, re.S)
    assert m, "deploy JSON island missing"
    return json.loads(m.group(1))


def test_deploy_page_renders_with_placeholders(client):
    r = client.get("/deploy")
    assert r.status_code == 200
    body = r.text
    assert "Развёртывание" in body
    assert "setup.exe" in body
    # secrets are placeholders only -- never real secrets in the open dashboard
    assert "<ПАРОЛЬ>" in body and "<ТОКЕН>" in body


def test_deploy_lists_directory_orgs(client, tmp_path):
    _write_dir(tmp_path, _SAMPLE)
    od.get_directory().reload_if_changed()  # deterministic mtime hot-reload
    body = client.get("/deploy").text
    data = _island(body)
    assert {o["code"] for o in data} == {"101", "202"}
    org101 = next(o for o in data if o["code"] == "101")
    assert org101["name"] == "ООО «Ромашка»"
    assert [d["code"] for d in org101["departments"]] == ["12", "7"]  # sorted by code
    # the org name is shown in the server-rendered <select> (autoescaped)
    assert "ООО «Ромашка»" in body


def test_deploy_default_server_from_request_host(client):
    body = client.get("/deploy").text
    assert "testserver" in body  # form pre-fills --server from the request base URL


def test_deploy_empty_directory_still_renders(client):
    r = client.get("/deploy")  # no org_directory.json written -> empty directory
    assert r.status_code == 200
    assert _island(r.text) == []


def test_deploy_json_island_escapes_script(client, tmp_path):
    """A hostile org name must not terminate the JSON <script> island (XSS pin)."""
    _write_dir(tmp_path, {"organizations": [{"code": "1", "name": "</script><script>alert(1)//"}]})
    od.get_directory().reload_if_changed()  # deterministic mtime hot-reload
    body = client.get("/deploy").text
    assert "</script><script>alert(1)" not in body
    data = _island(body)
    assert data[0]["name"] == "</script><script>alert(1)//"  # value survives JSON parse
