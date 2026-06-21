"""Phase 10 -- §3.13 change detection + ghost lifecycle (RED first).

``diff`` compares two topology snapshots into a deterministic delta list (device
appeared/disappeared, link added/removed, reclassified, ip_changed). Because nodes
are keyed by their stable nid, a DHCP IP change is ``ip_changed`` -- never a fake
appeared+disappeared pair. ``stale_lifecycle`` ages devices to ``missing`` only after
sustained absence (the device-ghost lesson: one missed cycle is not "disappeared").
"""

from __future__ import annotations

from datetime import datetime, timedelta

from server.netdisco import changes

_NOW = "2026-06-22T12:00:00+00:00"


def _ago(seconds: int) -> str:
    return (datetime.fromisoformat(_NOW) - timedelta(seconds=seconds)).isoformat()


def _snap(nodes, links):
    return {"nodes": nodes, "links": links}


def test_appeared_and_disappeared_by_nid():
    prev = _snap([{"nid": "a"}], [])
    curr = _snap([{"nid": "b"}], [])
    kinds = {(d.kind, d.device_nid) for d in changes.diff(prev, curr)}
    assert kinds == {("appeared", "b"), ("disappeared", "a")}


def test_link_added_and_removed():
    prev = _snap([{"nid": "a"}, {"nid": "b"}], [{"a": "a", "b": "b"}])
    curr = _snap([{"nid": "a"}, {"nid": "b"}], [{"a": "b", "b": "a"}, {"a": "a", "b": "c"}])
    kinds = {d.kind for d in changes.diff(prev, curr)}
    assert kinds == {"link_added"}  # a-b is the same undirected link (no churn); a-c added


def test_reclassified_when_dev_type_changes():
    prev = _snap([{"nid": "x", "dev_type": "unknown"}], [])
    curr = _snap([{"nid": "x", "dev_type": "switch"}], [])
    deltas = changes.diff(prev, curr)
    assert [d.kind for d in deltas] == ["reclassified"]
    assert deltas[0].detail == {"from": "unknown", "to": "switch"}


def test_dhcp_ip_change_is_not_appeared_or_disappeared():
    prev = _snap([{"nid": "nd-mac-AA", "ip": "10.0.0.5"}], [])
    curr = _snap([{"nid": "nd-mac-AA", "ip": "10.0.0.9"}], [])
    kinds = [d.kind for d in changes.diff(prev, curr)]
    assert kinds == ["ip_changed"]  # same identity, new lease -> not appeared/disappeared


def test_diff_is_deterministic_regardless_of_order():
    prev = _snap([{"nid": "a"}, {"nid": "b"}], [])
    curr = _snap([{"nid": "b"}, {"nid": "a"}, {"nid": "c"}, {"nid": "d"}], [])
    assert changes.diff(prev, curr) == changes.diff(prev, curr)
    assert [d.device_nid for d in changes.diff(prev, curr)] == ["c", "d"]  # sorted appeared


def test_stale_lifecycle_one_miss_is_not_missing():
    devices = [{"device_nid": "fresh", "last_seen": _ago(900), "status": "up"}]
    assert (
        changes.stale_lifecycle(devices, now=_NOW, stale_after_sec=2700, purge_after_sec=86400)
        == []
    )


def test_stale_lifecycle_marks_missing_then_eligible_purge():
    devices = [
        {"device_nid": "stale", "last_seen": _ago(4500), "status": "up"},
        {"device_nid": "ancient", "last_seen": _ago(90000), "status": "missing"},
    ]
    out = changes.stale_lifecycle(devices, now=_NOW, stale_after_sec=2700, purge_after_sec=86400)
    assert out == [("ancient", "eligible_purge"), ("stale", "missing")]
