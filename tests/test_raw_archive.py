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


def _seed_new_heartbeat(device_id: str) -> None:
    """Insert a fresh (not-yet-aged) heartbeat row, distinct from any prior row.

    Unlike ``_seed_old_heartbeat`` (which UPDATEs every existing row for the
    device), this INSERTs one -- needed to build a second row for the same
    device so a subsequent ``_seed_old_heartbeat`` call ages *both* into the
    archive's merge path instead of re-aging an already-archived-and-deleted
    row (a no-op).
    """
    db.store_heartbeat(device_id, ts=db._now_iso(), payload={"cpu_pct": 5.0})


def test_prune_merges_second_batch_into_same_day_archive_without_corrupting_it(
    seeded_client,
) -> None:
    """Pins the decompress-append-recompress merge path (server/db.py
    _archive_aged_rows): a second aged batch for the same (device, day) must
    merge into the existing archive blob, not replace or corrupt it. A naive
    "optimization" to raw ``existing_blob + new_blob`` concatenation would
    pass every other committed test yet silently lose the first batch's row
    here (zlib.decompress stops at the first stream's end), so this assertion
    on ``len(merged) == 2`` is load-bearing, not vacuous.
    """
    devices = seeded_client.get("/api/v1/devices").json()
    did = devices[0]["device_id"]
    _seed_old_heartbeat(did, days_ago=120)

    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)
    first = db.get_raw_archive(did, "heartbeats", days=730)
    assert len(first) == 1

    _seed_new_heartbeat(did)  # a second row for the same device
    _seed_old_heartbeat(did, days_ago=120)  # age it into the same day as the first
    db.prune_aged(heartbeat_raw_days=90, events_raw_days=90, rollup_days=730)

    merged = db.get_raw_archive(did, "heartbeats", days=730)
    assert len(merged) == 2, (
        "second prune must merge into the existing day's archive, not replace or corrupt it"
    )
    assert all(row["device_id"] == did for row in merged)
