"""D9: prune_aged архивирует сырые окна (zlib) до удаления; архив читается."""

from __future__ import annotations

from server import db


def _seed_old_heartbeat(device_id: str, days_ago: int) -> None:
    with db._connect() as conn:  # тестовый сид: состарить received_at напрямую
        conn.execute(
            "UPDATE heartbeats SET received_at = datetime('now', ?) WHERE device_id = ?",
            (f"-{days_ago} days", device_id),
        )


def test_prune_archives_before_delete(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    _seed_old_heartbeat(did, days_ago=120)

    deleted = db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)

    assert deleted.get("heartbeats", 0) >= 1
    archived = db.get_raw_archive(did, "heartbeats", days=730)
    assert archived, "удалённые сырые строки обязаны лежать в raw_archive"
    assert archived[0]["device_id"] == did  # строки распаковываются в исходные dict


def test_prune_second_run_is_idempotent(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    _seed_old_heartbeat(did, days_ago=120)
    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)
    first = db.get_raw_archive(did, "heartbeats", days=730)

    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)

    assert db.get_raw_archive(did, "heartbeats", days=730) == first


def test_raw_archive_rows_die_with_device(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    _seed_old_heartbeat(did, days_ago=120)
    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)
    assert db.get_raw_archive(did, "heartbeats", days=730)

    db.delete_device(did)

    assert db.get_raw_archive(did, "heartbeats", days=730) == []
