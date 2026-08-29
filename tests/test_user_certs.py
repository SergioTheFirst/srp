"""Stage 8: tray personal-cert spool writer + the agent's strict spool reader.

The spool is written by a non-admin user, so ``read_user_certs`` is tested as a
hostile-input parser: bad JSON, oversized files, missing/!int fields, dedup, the
hard total cap, the freshness window, and epoch->ISO conversion. ``write_spool``
round-trips through it. Pure stdlib, off-Windows.
"""

from __future__ import annotations

import json
from pathlib import Path

from client.collectors import user_certs as uc
from client.tray import spool
from client.tray.certs import CertInfo

_NOW = 1_718_000_000.0
_FUTURE = int(_NOW + 90 * 86_400)


def _write(spool_dir: Path, name: str, doc: object) -> None:
    spool_dir.mkdir(parents=True, exist_ok=True)
    (spool_dir / name).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _doc(owner: str, certs: list, written: float = _NOW) -> dict:
    return {"owner": owner, "written_at": int(written), "certs": certs}


def _cert(thumb: str = "ABC123", not_after: int = _FUTURE) -> dict:
    return {"subject": "CN=Иван", "issuer": "CN=CA", "thumbprint": thumb, "not_after": not_after}


# --------------------------------------------------------------------------- #
# read_user_certs — happy path + epoch->ISO + owner attribution
# --------------------------------------------------------------------------- #


def test_read_valid_spool_attaches_owner_and_iso(tmp_path: Path) -> None:
    _write(tmp_path, "usercerts-jdoe.json", _doc("jdoe", [_cert()]))
    out = uc.read_user_certs(tmp_path, now=_NOW)
    assert len(out) == 1
    c = out[0]
    assert c["owner"] == "jdoe" and c["thumbprint"] == "ABC123"
    assert c["not_after"].startswith("20") and c["not_after"].endswith("+00:00")  # epoch -> ISO


def test_absent_dir_is_empty(tmp_path: Path) -> None:
    assert uc.read_user_certs(tmp_path / "nope", now=_NOW) == []


def test_only_matching_glob_is_read(tmp_path: Path) -> None:
    _write(tmp_path, "usercerts-a.json", _doc("a", [_cert("AA")]))
    _write(tmp_path, "other.json", _doc("x", [_cert("BB")]))  # wrong name -> ignored
    _write(tmp_path, "usercerts-b.json.tmp", _doc("b", [_cert("CC")]))  # tmp -> ignored
    owners = {c["owner"] for c in uc.read_user_certs(tmp_path, now=_NOW)}
    assert owners == {"a"}


# --------------------------------------------------------------------------- #
# read_user_certs — hostile input is skipped, never raised
# --------------------------------------------------------------------------- #


def test_bad_json_is_skipped(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "usercerts-bad.json").write_text("{ not json", encoding="utf-8")
    assert uc.read_user_certs(tmp_path, now=_NOW) == []


def test_oversized_file_is_skipped(tmp_path: Path) -> None:
    big = _doc("big", [_cert(f"T{i}", _FUTURE) for i in range(5)])
    big["padding"] = "x" * (70 * 1024)  # > 64 KiB cap
    _write(tmp_path, "usercerts-big.json", big)
    assert uc.read_user_certs(tmp_path, now=_NOW) == []


def test_entry_without_thumbprint_or_bad_date_is_dropped(tmp_path: Path) -> None:
    certs = [
        {"subject": "CN=x", "not_after": _FUTURE},  # no thumbprint
        {"thumbprint": "NODATE"},  # no not_after
        {"thumbprint": "BADDATE", "not_after": "soon"},  # non-int epoch
        _cert("GOOD"),
    ]
    _write(tmp_path, "usercerts-mix.json", _doc("u", certs))
    out = uc.read_user_certs(tmp_path, now=_NOW)
    assert [c["thumbprint"] for c in out] == ["GOOD"]


def test_non_dict_doc_and_certs_not_list_are_empty(tmp_path: Path) -> None:
    _write(tmp_path, "usercerts-list.json", ["not", "a", "dict"])
    _write(tmp_path, "usercerts-str.json", {"owner": "u", "certs": "nope"})
    assert uc.read_user_certs(tmp_path, now=_NOW) == []


def test_stale_file_ages_out(tmp_path: Path) -> None:
    old = _NOW - 40 * 86_400  # older than the 30-day freshness window
    _write(tmp_path, "usercerts-gone.json", _doc("gone", [_cert("OLD")], written=old))
    assert uc.read_user_certs(tmp_path, now=_NOW) == []


def test_dedup_by_owner_and_thumbprint(tmp_path: Path) -> None:
    _write(tmp_path, "usercerts-dup.json", _doc("u", [_cert("SAME"), _cert("SAME")]))
    assert len(uc.read_user_certs(tmp_path, now=_NOW)) == 1


def test_total_cap_enforced(tmp_path: Path) -> None:
    certs = [_cert(f"T{i}") for i in range(uc._MAX_TOTAL + 20)]
    _write(tmp_path, "usercerts-many.json", _doc("u", certs))
    assert len(uc.read_user_certs(tmp_path, now=_NOW)) == uc._MAX_TOTAL


# --------------------------------------------------------------------------- #
# build_spool / write_spool — metadata only, safe names, round-trip
# --------------------------------------------------------------------------- #


def _ci(thumb: str = "ABC123") -> CertInfo:
    return CertInfo(
        subject="CN=Иван", issuer="CN=CA", thumbprint=thumb, not_before=1, not_after=_FUTURE
    )


def test_build_spool_is_metadata_only() -> None:
    doc = spool.build_spool("jdoe", [_ci()], _NOW)
    assert doc["owner"] == "jdoe" and doc["written_at"] == int(_NOW)
    blob = json.dumps(doc).lower()
    for forbidden in ("private", "key", "pfx", "pkcs"):
        assert forbidden not in blob  # never any key material


def test_spool_path_sanitizes_username(tmp_path: Path) -> None:
    p = spool.spool_path(tmp_path, r"DOMAIN\bad/../name")
    assert p.parent == tmp_path
    assert p.name.startswith("usercerts-") and "/" not in p.name and "\\" not in p.name


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    assert spool.write_spool(tmp_path, "jdoe", [_ci("RT")], now=_NOW) is True
    out = uc.read_user_certs(tmp_path, now=_NOW)
    assert len(out) == 1 and out[0]["owner"] == "jdoe" and out[0]["thumbprint"] == "RT"
    # the temp file is gone (atomic replace) and isn't read as a cert
    assert not list(tmp_path.glob("*.tmp"))


def test_publish_none_is_noop(tmp_path: Path) -> None:
    spool.publish_user_certs(None)  # PS failed -> must not raise / must not write
