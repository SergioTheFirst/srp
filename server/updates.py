"""PUBLIC STUB replacement for ``server/updates.py``.

The private/commercial module validates and serves a fleet auto-update
package (SHA-256 + HMAC checked against ``server/updates/manifest.json``)
-- fleet auto-update is a Full-edition feature, see the public README's
«Ограничения бесплатной версии». The public/Free edition never offers an
update package, so this stub always reports "nothing staged".

Every caller already treats that as a first-class, tested state, not an
error: both ``/api/v1/agent/update*`` routes in ``server/api.py`` raise a
plain 404 "no update package" when ``get_update_info`` returns ``None``,
and the dashboard's outdated-agents/version KPIs and ``deploy.html`` all
fall back to their "no updates_dir configured" branch. No caller needs to
change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def reset_cache() -> None:
    """No-op -- the stub has nothing to cache. Kept for test-fixture parity
    with the real module (some tests call this in a fixture)."""
    return None


def get_update_info(updates_dir: Path, token: str = "") -> Optional[dict]:  # nosec B107 -- empty = no token
    """Always report "no update package offered" -- see module docstring."""
    return None
