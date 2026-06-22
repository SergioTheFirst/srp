"""Phase 13: DPAPI credential store for non-public SNMP community (SAFETY).

A non-public SNMP community must live on disk only DPAPI-encrypted, never
plaintext, and must never leak into logs/API/dashboard. On non-Windows (no
DPAPI) the only allowed community is ``public`` (read-only, not a secret).
``public`` stays valid plaintext without any store.
"""

from __future__ import annotations

import base64
import json

import pytest
from server.netdisco.config import load_netdisco_config
from server.netdisco.credentials import (
    CredentialRef,
    CredentialStore,
    dpapi_available,
    resolve_community,
)


def _fake_protect(data: bytes) -> bytes:
    # Reversible stand-in for DPAPI on non-Windows test hosts: marker + xor 0x5a.
    return b"ENC:" + bytes(b ^ 0x5A for b in data)


def _fake_unprotect(blob: bytes) -> bytes:
    if not blob.startswith(b"ENC:"):
        raise ValueError("not a fake-DPAPI blob")
    return bytes(b ^ 0x5A for b in blob[4:])


def _store(tmp_path, available: bool = True) -> CredentialStore:
    return CredentialStore(
        tmp_path / "netdisco_secrets.json",
        protect=_fake_protect,
        unprotect=_fake_unprotect,
        available=available,
    )


def test_set_then_get_round_trips_community(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_community("core-snmp", "s3cr3t-community")
    assert store.get_community("core-snmp") == "s3cr3t-community"


def test_on_disk_secret_is_encrypted_not_plaintext(tmp_path) -> None:
    path = tmp_path / "netdisco_secrets.json"
    store = _store(tmp_path)
    store.set_community("core-snmp", "s3cr3t-community")
    raw = path.read_bytes()
    assert b"s3cr3t-community" not in raw
    # File is structured JSON of base64 blobs, never the cleartext.
    decoded = json.loads(raw.decode("utf-8"))
    blob = base64.b64decode(decoded["communities"]["core-snmp"])
    assert blob.startswith(b"ENC:")


def test_get_missing_ref_returns_none(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.get_community("nope") is None


def test_set_when_unavailable_raises(tmp_path) -> None:
    store = _store(tmp_path, available=False)
    with pytest.raises(RuntimeError):
        store.set_community("core-snmp", "s3cr3t")


def test_get_when_unavailable_returns_none(tmp_path) -> None:
    # File written earlier with a working store; later host lacks DPAPI.
    _store(tmp_path).set_community("core-snmp", "s3cr3t")
    blind = _store(tmp_path, available=False)
    assert blind.get_community("core-snmp") is None


def test_get_corrupt_base64_entry_returns_none(tmp_path) -> None:
    path = tmp_path / "netdisco_secrets.json"
    path.write_text(json.dumps({"communities": {"core-snmp": "!!!not-base64!!!"}}), "utf-8")
    store = _store(tmp_path)
    assert store.get_community("core-snmp") is None


def test_resolve_no_ref_uses_plaintext_community() -> None:
    cfg = load_netdisco_config({"snmp_community": "public"})
    assert resolve_community(cfg, store=None) == "public"


def test_resolve_non_public_plaintext_warns(caplog) -> None:
    cfg = load_netdisco_config({"snmp_community": "s3cr3t"})
    with caplog.at_level("WARNING", logger="srp.netdisco"):
        community = resolve_community(cfg, store=None)
    assert community == "s3cr3t"  # still honored, but operator is warned
    assert any("snmp_credential_ref" in r.message for r in caplog.records)
    assert "s3cr3t" not in caplog.text  # the value itself never logged


def test_resolve_with_ref_and_store_returns_secret(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_community("core-snmp", "s3cr3t-community")
    cfg = load_netdisco_config({"snmp_credential_ref": "core-snmp"})
    assert resolve_community(cfg, store=store) == "s3cr3t-community"


def test_resolve_with_ref_but_no_secret_falls_back_to_public(tmp_path) -> None:
    store = _store(tmp_path)  # nothing stored under that name
    cfg = load_netdisco_config({"snmp_credential_ref": "core-snmp"})
    assert resolve_community(cfg, store=store) == "public"


def test_resolve_with_ref_but_unavailable_store_falls_back_to_public(tmp_path) -> None:
    store = _store(tmp_path, available=False)
    cfg = load_netdisco_config({"snmp_credential_ref": "core-snmp"})
    assert resolve_community(cfg, store=store) == "public"


def test_config_parses_credential_ref() -> None:
    cfg = load_netdisco_config({"snmp_credential_ref": "core-snmp"})
    assert cfg.snmp_credential_ref == "core-snmp"


def test_config_credential_ref_defaults_empty_and_rejects_nonstr() -> None:
    assert load_netdisco_config({}).snmp_credential_ref == ""
    assert load_netdisco_config({"snmp_credential_ref": 123}).snmp_credential_ref == ""


def test_credential_ref_has_snmpv3_scaffold_defaulting_none() -> None:
    ref = CredentialRef(name="core-snmp")
    assert ref.version == "v2c"
    assert ref.user is None
    assert ref.auth_key is None
    assert ref.priv_key is None


def test_dpapi_unavailable_off_windows() -> None:
    import sys

    if sys.platform != "win32":
        assert dpapi_available() is False


def test_make_session_resolves_community_from_store(tmp_path, monkeypatch) -> None:
    # Scheduler's session factory must pull a non-public community via the
    # store, not pass the plaintext config value.
    from server.netdisco import scheduler

    store = _store(tmp_path)
    store.set_community("core-snmp", "s3cr3t-community")
    monkeypatch.setattr(scheduler, "default_store", lambda: store)
    cfg = load_netdisco_config({"snmp_credential_ref": "core-snmp"})

    session = scheduler._make_session("10.0.0.1", cfg)

    assert session.community == "s3cr3t-community"
