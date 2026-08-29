"""Personal-certificate reminder engine for the tray (tray spec §3).

The SYSTEM agent (W1.2) can only see its own ``LocalMachine`` / SYSTEM stores, so
a user's signing certificate in ``Cert:\\CurrentUser\\My`` is invisible to it.
The tray runs *inside the user's session*, so it checks that store itself and
nags about an expiring/expired signature.

Everything decision-shaped lives here as pure functions (testable off-Windows);
only :func:`query_certs` touches PowerShell. Design choices:

* **Epoch, never localised dates** -- the snippet emits ``ToUnixTimeSeconds()``
  so we never parse a localised date string (``[[language-independence]]``).
* **Metadata only, never key material** -- we read the boolean ``HasPrivateKey``
  to pick *signing* certs, never the private key (privacy invariant, W1.2).
* **Subject-CN grouping** -- a renewal is a new cert of the same subject; a valid
  successor silences reminders about the old one (before *and* after expiry) and
  announces itself once, so "renewed" never reads as "noise" or a false alarm.
* **UNKNOWN over red** -- a PowerShell failure yields ``"unknown"``, not an alert:
  not being able to look is not proof the certificate is bad.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from client.collectors.ps import PsResult, as_list, run_ps
from client.tray.state import CertLevel, IconState

# Schedule constants (spec §3 table).
_RED_DAYS = 7  # valid but <= this many days left -> red
_EXPIRED_GRACE_DAYS = 7  # keep nagging at most this long after expiry, then go quiet
_DAY = 86_400.0

_MISSING_KEY = "_missing"  # tray_state key for the require_cert "no cert at all" nag
_TITLE = "Сертификат SRP"

_LEVEL_RANK: dict[str, int] = {"ok": 0, "unknown": 1, "warn": 1, "alert": 2}
_CATEGORY_LEVEL: dict[str, IconState] = {
    "ok": "ok",
    "warn": "warn",
    "critical": "alert",
    "expired_recent": "alert",
    "expired_old": "alert",
    "missing": "alert",
}

# PowerShell 5.1 snippet: the user's personal signing certs as epoch seconds.
# Reads HasPrivateKey (boolean) to select signing certs; never the key itself.
_CERT_SCRIPT = r"""
Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
  Where-Object { $_.HasPrivateKey } |
  Select-Object -First 200 |
  ForEach-Object {
    [ordered]@{
      subject = "$($_.Subject)"
      issuer = "$($_.Issuer)"
      thumbprint = "$($_.Thumbprint)"
      not_before = ([DateTimeOffset]$_.NotBefore).ToUnixTimeSeconds()
      not_after = ([DateTimeOffset]$_.NotAfter).ToUnixTimeSeconds()
      has_private_key = $true
    }
  } | ConvertTo-Json -Depth 3 -Compress
"""


@dataclass(frozen=True)
class CertInfo:
    """Metadata for one certificate -- never any private-key material."""

    subject: str
    issuer: str
    thumbprint: str
    not_before: int  # epoch seconds
    not_after: int  # epoch seconds
    has_private_key: bool = True


@dataclass(frozen=True)
class Balloon:
    """A notification the tray should raise this cycle."""

    title: str
    message: str
    level: IconState  # NIIF flag: ok=info, warn, alert


@dataclass(frozen=True)
class CertEvaluation:
    """Result of one cert check: icon colour, panel row, balloons, new state."""

    level: CertLevel
    panel_text: str
    balloons: tuple[Balloon, ...] = ()
    state: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _cert_from_dict(d: dict[str, Any]) -> Optional[CertInfo]:
    tp = d.get("thumbprint")
    if not tp:
        return None
    try:
        not_after = int(d["not_after"])
        not_before = int(d.get("not_before", 0))
    except (KeyError, TypeError, ValueError):
        return None
    return CertInfo(
        subject=str(d.get("subject", "")),
        issuer=str(d.get("issuer", "")),
        thumbprint=str(tp),
        not_before=not_before,
        not_after=not_after,
        has_private_key=bool(d.get("has_private_key", True)),
    )


def _certs_from_obj(obj: Any) -> list[CertInfo]:
    out: list[CertInfo] = []
    for entry in as_list(obj):
        if isinstance(entry, dict):
            cert = _cert_from_dict(entry)
            if cert is not None:
                out.append(cert)
    return out


def parse_certs(raw: str) -> list[CertInfo]:
    """Parse the PowerShell JSON dump into certs; tolerate junk (-> empty)."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return _certs_from_obj(obj)


# --------------------------------------------------------------------------- #
# subject grouping
# --------------------------------------------------------------------------- #


def subject_cn(subject: str) -> str:
    """Normalised Subject CN used as the grouping key.

    Falls back to the whole subject when there is no CN= component, so a cert
    still groups deterministically.
    """
    for part in subject.split(","):
        stripped = part.strip()
        if stripped[:3].upper() == "CN=":
            return stripped[3:].strip().casefold()
    return subject.strip().casefold()


def group_by_subject(certs: list[CertInfo]) -> dict[str, list[CertInfo]]:
    """Group signing certs by normalised Subject CN (key-less certs ignored)."""
    groups: dict[str, list[CertInfo]] = {}
    for cert in certs:
        if not cert.has_private_key:
            continue
        groups.setdefault(subject_cn(cert.subject), []).append(cert)
    return groups


def best_cert(group: list[CertInfo]) -> CertInfo:
    """The most current cert of a subject group (latest expiry = the successor)."""
    return max(group, key=lambda c: c.not_after)


# --------------------------------------------------------------------------- #
# classification + schedule (pure)
# --------------------------------------------------------------------------- #


def classify(best: CertInfo, *, now: float, warn_days: int) -> str:
    """Bucket the group's best cert: ok|warn|critical|expired_recent|expired_old."""
    days_left = (best.not_after - now) / _DAY
    if days_left > 0:
        if days_left > warn_days:
            return "ok"
        if days_left > _RED_DAYS:
            return "warn"
        return "critical"
    age_days = (now - best.not_after) / _DAY
    return "expired_recent" if age_days <= _EXPIRED_GRACE_DAYS else "expired_old"


def cert_level_for(best: CertInfo, *, now: float, warn_days: int) -> CertLevel:
    """Icon colour for the group's best cert."""
    return _CATEGORY_LEVEL[classify(best, now=now, warn_days=warn_days)]


def should_nag(
    category: str,
    group_state: dict[str, Any],
    *,
    now: float,
    notify_hours: int,
    today: str,
) -> tuple[bool, dict[str, Any]]:
    """Decide whether to raise a balloon now; return (fire?, updated group state).

    * warn/critical -> every ``notify_hours`` (boundary inclusive).
    * expired_recent/missing -> once per *calendar* day (first check of a new day).
    * ok/expired_old -> never (icon still shows the state; we just stop nagging).
    """
    gs = dict(group_state)
    if category in ("warn", "critical"):
        last = gs.get("last_nag_epoch")
        if last is None or now - float(last) >= notify_hours * 3600:
            gs["last_nag_epoch"] = now
            return True, gs
        return False, gs
    if category in ("expired_recent", "missing"):
        if gs.get("last_nag_date") != today:
            gs["last_nag_date"] = today
            gs["last_nag_epoch"] = now
            return True, gs
        return False, gs
    return False, gs


# --------------------------------------------------------------------------- #
# text (RU operator prose; tech terms stay as is)
# --------------------------------------------------------------------------- #


def _local_date(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _fmt_date(epoch: int) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(epoch))


def _days_left_ceil(best: CertInfo, now: float) -> int:
    return max(0, math.ceil((best.not_after - now) / _DAY))


def _with_helpdesk(msg: str, helpdesk: str) -> str:
    return f"{msg} Поддержка: {helpdesk}" if helpdesk else msg


def _panel_text(best: CertInfo, category: str, now: float) -> str:
    date = _fmt_date(best.not_after)
    if category == "ok":
        return f"действует до {date}"
    if category in ("warn", "critical"):
        return f"истекает через {_days_left_ceil(best, now)} дн. ({date})"
    return f"истёк {date}"


def _expiry_balloon(best: CertInfo, category: str, now: float, helpdesk: str) -> Balloon:
    date = _fmt_date(best.not_after)
    if category == "warn":
        msg = f"Сертификат истекает через {_days_left_ceil(best, now)} дн. ({date})."
    elif category == "critical":
        msg = (
            f"Сертификат истекает через {_days_left_ceil(best, now)} дн. ({date})! "
            "Продлите ЭЦП заранее."
        )
    else:  # expired_recent
        msg = f"Сертификат ИСТЁК {date}. Работа с подписью невозможна."
    return Balloon(_TITLE, _with_helpdesk(msg, helpdesk), _CATEGORY_LEVEL[category])


# --------------------------------------------------------------------------- #
# evaluate -- orchestration
# --------------------------------------------------------------------------- #


def _worse(a: CertLevel, b: CertLevel) -> CertLevel:
    return a if _LEVEL_RANK[a] >= _LEVEL_RANK[b] else b


def _evaluate_empty(
    new_state: dict[str, Any],
    *,
    now: float,
    today: str,
    notify_hours: int,
    require_cert: bool,
    helpdesk: str,
) -> CertEvaluation:
    """No signing cert in the store at all (spec §3 require_cert clause)."""
    if not require_cert:
        return CertEvaluation("ok", "нет личного сертификата", (), {})
    fire, gs = should_nag(
        "missing", new_state.get(_MISSING_KEY, {}), now=now, notify_hours=notify_hours, today=today
    )
    balloons: tuple[Balloon, ...] = ()
    if fire:
        msg = _with_helpdesk("Личный сертификат не установлен.", helpdesk)
        balloons = (Balloon(_TITLE, msg, "alert"),)
    # prune: only the "no cert" bookkeeping survives an empty store
    return CertEvaluation("alert", "сертификат не установлен", balloons, {_MISSING_KEY: gs})


def _announce_successor(
    group: list[CertInfo], best: CertInfo, now: float, new_state: dict[str, Any]
) -> Optional[Balloon]:
    """One-time info balloon when a renewal (valid successor) appears."""
    if len(group) < 2 or best.not_after <= now:
        return None
    gs = dict(new_state.get(best.thumbprint, {}))
    if gs.get("new_cert_announced"):
        return None
    gs["new_cert_announced"] = True
    new_state[best.thumbprint] = gs
    return Balloon(
        _TITLE, f"Обнаружен новый сертификат, действует до {_fmt_date(best.not_after)}.", "ok"
    )


def evaluate(
    certs: Optional[list[CertInfo]],
    state: dict[str, Any],
    *,
    now: float,
    warn_days: int = 14,
    notify_hours: int = 4,
    require_cert: bool = False,
    helpdesk: str = "",
) -> CertEvaluation:
    """Full cert check: worst-of icon level, panel row, balloons, new tray state."""
    if certs is None:
        return CertEvaluation("unknown", "не удалось проверить сертификат", (), dict(state))

    new_state = dict(state)
    today = _local_date(now)
    groups = group_by_subject(certs)
    if not groups:
        return _evaluate_empty(
            new_state,
            now=now,
            today=today,
            notify_hours=notify_hours,
            require_cert=require_cert,
            helpdesk=helpdesk,
        )

    balloons: list[Balloon] = []
    worst: CertLevel = "ok"
    panel_best: Optional[CertInfo] = None
    panel_category = "ok"
    seen: set[str] = set()
    for group in groups.values():
        best = best_cert(group)
        category = classify(best, now=now, warn_days=warn_days)
        level = _CATEGORY_LEVEL[category]
        seen.add(best.thumbprint)

        info = _announce_successor(group, best, now, new_state)
        if info is not None:
            balloons.append(info)

        fire, gs = should_nag(
            category,
            new_state.get(best.thumbprint, {}),
            now=now,
            notify_hours=notify_hours,
            today=today,
        )
        new_state[best.thumbprint] = gs
        if fire:
            balloons.append(_expiry_balloon(best, category, now, helpdesk))

        worst = _worse(worst, level)
        if panel_best is None or _is_more_urgent(level, best, panel_category, panel_best):
            panel_best, panel_category = best, category

    # prune: keep only current certs with real bookkeeping (no unbounded growth)
    pruned = {k: v for k, v in new_state.items() if k in seen and v}
    panel = _panel_text(panel_best, panel_category, now) if panel_best else "—"
    return CertEvaluation(worst, panel, tuple(balloons), pruned)


def _is_more_urgent(
    level: IconState, best: CertInfo, panel_category: str, panel_best: CertInfo
) -> bool:
    """Pick the panel cert: worst level, then soonest expiry."""
    cur = _LEVEL_RANK[level]
    shown = _LEVEL_RANK[_CATEGORY_LEVEL[panel_category]]
    if cur != shown:
        return cur > shown
    return best.not_after < panel_best.not_after


# --------------------------------------------------------------------------- #
# PowerShell adapter
# --------------------------------------------------------------------------- #


def query_certs(
    run_ps_fn: Callable[[str, int], PsResult] = run_ps, *, timeout: int = 30
) -> Optional[list[CertInfo]]:
    """Read ``Cert:\\CurrentUser\\My``; None on a PS failure (UNKNOWN, not red).

    An *empty* store is a fact (``[]``), distinct from a failed look (``None``).
    """
    res = run_ps_fn(_CERT_SCRIPT, timeout)
    if res.status == "ok":
        return _certs_from_obj(res.data)
    if res.status == "empty":
        return []
    return None
