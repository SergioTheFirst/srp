"""P3-7: `_enrich_fleet` must call the org directory's `reload_if_changed()`
exactly once per poll cycle, not once per device.

`_identity_labels` (called once per device) decodes `org_code`/`dept_code` via
`OrgDirectory.org_display`/`dept_display`, each of which used to call
`self.reload_if_changed()` (an mtime `stat()`) unconditionally on every call --
N devices meant N redundant stat() calls per ~12s dashboard poll. The fix
hoists one explicit `reload_if_changed()` call before the loop and threads a
`check_reload=False` flag through `_identity_labels`/`org_display`/
`dept_display`/`org_name`/`dept_name` for the per-device calls, so they skip
their own reload check and rely on the already-fresh directory instead.

A prior version of this test only asserted `1 <= calls <= 7`, a range wide
enough that it passed identically whether the per-device calls were actually
skipped or not (3 devices x 2 lookups + 1 hoisted = 7 either way) -- it could
never have caught a fix that didn't work. This version asserts the exact
count instead.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from server.org_directory import OrgDirectory
from server.web.dashboard import _enrich_fleet


def test_enrich_fleet_calls_reload_if_changed_exactly_once_per_poll(tmp_path: Path):
    devices = [
        {
            "device_id": f"dev{i}",
            "org_code": "101",
            "dept_code": "7",
            "agent_version": "1.0.0",
        }
        for i in range(3)
    ]
    org_file = tmp_path / "org_directory.json"
    org_file.write_text(
        '{"organizations": [{"code": "101", "name": "Test", '
        '"departments": [{"code": "7", "name": "Dept"}]}]}'
    )
    directory = OrgDirectory(org_file)

    reload_calls = []
    original_reload = directory.reload_if_changed

    def tracked_reload():
        reload_calls.append(1)
        return original_reload()

    directory.reload_if_changed = tracked_reload

    with patch("server.web.dashboard.org_directory.get_directory", return_value=directory):
        out = _enrich_fleet(devices)

    # Exactly 1 -- the hoisted call. Without the check_reload=False threading,
    # this would be 1 (hoist) + 3 devices x 2 lookups (org_display/dept_display)
    # = 7. A prior, non-discriminating version of this test only asserted
    # `<= 7`, which passed either way.
    assert len(reload_calls) == 1, (
        f"expected exactly 1 reload_if_changed call (the hoisted one), got {len(reload_calls)} "
        "-- per-device calls are not actually skipping their own reload check"
    )
    # Sanity: the per-device lookups still ran and produced real labels --
    # skipping the *reload check* must not skip the *lookup* itself.
    assert out[0]["org_label"] == {"text": "Test", "known": True}
    assert out[0]["dept_label"] == {"text": "Dept", "known": True}


def test_enrich_fleet_labels_reflect_a_file_change_made_before_the_poll(tmp_path: Path):
    """The single hoisted reload still picks up an edit made before this poll
    started -- check_reload=False on the per-device calls must not mean
    "never see new data", only "don't redundantly re-check within this poll"."""
    org_file = tmp_path / "org_directory.json"
    org_file.write_text('{"organizations": [{"code": "101", "name": "Old", "departments": []}]}')
    directory = OrgDirectory(org_file)

    # Edit the file (mtime must visibly advance on all filesystems).
    time.sleep(0.01)
    org_file.write_text('{"organizations": [{"code": "101", "name": "New", "departments": []}]}')
    os.utime(org_file, None)

    devices = [{"device_id": "dev0", "org_code": "101", "agent_version": "1.0.0"}]
    with patch("server.web.dashboard.org_directory.get_directory", return_value=directory):
        out = _enrich_fleet(devices)

    assert out[0]["org_label"] == {"text": "New", "known": True}
