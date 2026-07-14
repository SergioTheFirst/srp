"""W0.1 replayability: сохранённая история, пересчитанная оффлайн, детерминирована.

recompute_scores читает ТОЛЬКО из БД -- два вызова подряд на неизменном
хранилище обязаны дать одинаковые оси. Заодно пинится append-only: каждый
пересчёт добавляет строку в scores, ничего не перезаписывая.
"""

from __future__ import annotations

from server import db
from server.pipeline import recompute_scores

# risk-блоб сюда не входит: несёт time-зависимые поля (ETA, staleness-возраст),
# которые легитимно отличаются между двумя вызовами на ~секунду реального
# времени -- сравнивать его целиком означало бы пинить недетерминизм как баг.
_STABLE_AXES = ("performance", "reliability", "wear", "risk_exposure")


def test_offline_recompute_is_deterministic_and_appends(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    assert devices
    did = devices[0]["device_id"]
    rows_before = len(db.get_score_series(did, limit=1000))

    first = recompute_scores(did)
    second = recompute_scores(did)

    assert first is not None and second is not None
    assert {k: first.get(k) for k in _STABLE_AXES} == {k: second.get(k) for k in _STABLE_AXES}, (
        "оффлайн-пересчёт по одной и той же истории обязан быть детерминирован"
    )
    rows_after = len(db.get_score_series(did, limit=1000))
    assert rows_after == rows_before + 2, "каждый replay добавляет строку (append-only)"
