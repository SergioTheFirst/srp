"""Plotly отдаёт сам сервер J-Space, а не cdn.plot.ly.

Зачем: дашборд живёт в офисной сети, часто без выхода в интернет, — с внешним
CDN графики там просто не строились. Для публичного стенда то же самое ещё и
правовой вопрос: скрипт с американского CDN отдаёт IP посетителя за границу.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "server" / "web" / "templates"
PLOTLY_URL = "/static/plotly-2.35.2.min.js"


def test_server_serves_plotly(client):
    resp = client.get(PLOTLY_URL)
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert resp.content.startswith(b"/**\n* plotly.js v2.35.2")


def test_templates_do_not_load_plotly_from_cdn():
    offenders = [t.name for t in TEMPLATES.glob("*.html") if "cdn.plot.ly" in t.read_text("utf-8")]
    assert not offenders, f"шаблоны тянут Plotly с CDN: {offenders}"


def test_static_dir_does_not_expose_parent(client):
    """StaticFiles не должен выпускать за пределы своего каталога."""
    assert client.get("/static/../main.py").status_code == 404
    assert client.get("/static/%2e%2e/main.py").status_code == 404


def test_static_is_gzipped_under_vercel_limit(client):
    """Vercel не отдаёт из функции ответ больше 4,5 МБ — сырой Plotly крупнее."""
    resp = client.get(PLOTLY_URL, headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"
    # gzip идёт потоком без Content-Length — меряем байты, реально ушедшие по сети.
    assert resp.num_bytes_downloaded < 4_500_000


def test_static_dir_holds_only_vendored_js():
    """Всё, что лежит в static/, публично без токена — базе или конфигу там не место."""
    static = TEMPLATES.parent / "static"
    assert sorted(p.name for p in static.iterdir()) == ["plotly-2.35.2.min.js"]
