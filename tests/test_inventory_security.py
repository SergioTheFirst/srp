"""Агент 0.3.8: «маленькие победы» в inventory -- патчи, антивирус, BitLocker, TPM,
Secure Boot, активация. Сбой любого блока = null («неизвестно»), не «плохо»."""

from __future__ import annotations

import pytest
from client.collectors import inventory
from client.collectors.ps import PsResult
from shared.schema import InventoryPayload

pytestmark = pytest.mark.unit

_BASE = {"hostname": "PC1", "model": "OptiPlex", "os_caption": "Windows 10 Pro", "disks": []}


def _collect(monkeypatch, **raw):
    data = {**_BASE, **raw}
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: PsResult("ok", data))
    payload = inventory.collect_inventory().payload
    assert payload is not None
    return payload


def test_security_fields_parsed(monkeypatch):
    p = _collect(
        monkeypatch,
        last_patch_date="2026-09-10",
        antivirus=[{"name": "Windows Defender", "state": 0x61100}],
        bitlocker_status=1,
        tpm_present=True,
        tpm_spec="2.0, 0, 1.38",
        secure_boot=1,
        license_status=[1],
    )
    assert p["last_patch_date"] == "2026-09-10"
    assert p["antivirus"] == [{"name": "Windows Defender", "enabled": True, "up_to_date": True}]
    assert p["bitlocker_system"] == "on"
    assert p["tpm_present"] is True and p["tpm_version"] == "2.0"
    assert p["secure_boot"] is True
    assert p["windows_activated"] is True
    InventoryPayload.model_validate(p)


def test_av_disabled_and_outdated_bits(monkeypatch):
    p = _collect(monkeypatch, antivirus=[{"name": "X", "state": 0x60110}])
    assert p["antivirus"] == [{"name": "X", "enabled": False, "up_to_date": False}]


def test_av_snoozed_defender_is_not_enabled(monkeypatch):
    """0x062100 -- живое значение выключенного Defender (Get-MpComputerStatus: AV=False)."""
    p = _collect(monkeypatch, antivirus=[{"name": "Windows Defender", "state": 0x62100}])
    assert p["antivirus"][0]["enabled"] is False


def test_failed_blocks_are_unknown_not_bad(monkeypatch):
    p = _collect(monkeypatch)  # PS не вернул ни одного нового блока
    for key in (
        "last_patch_date",
        "antivirus",
        "bitlocker_system",
        "tpm_present",
        "tpm_version",
        "secure_boot",
        "windows_activated",
    ):
        assert p[key] is None, key


def test_garbage_values_become_unknown(monkeypatch):
    p = _collect(
        monkeypatch,
        last_patch_date="16.10.2025",
        antivirus="oops",
        bitlocker_status=7,
        tpm_present=False,
        tpm_spec=None,
        secure_boot="yes",
        license_status=[],
    )
    assert p["last_patch_date"] is None
    assert p["antivirus"] is None
    assert p["bitlocker_system"] == "unknown"
    assert p["tpm_present"] is False and p["tpm_version"] is None
    assert p["secure_boot"] is None
    assert p["windows_activated"] is None  # пусто = ключ не найден, не «не активирована»


def test_av_list_clipped(monkeypatch):
    many = [{"name": "A" * 500, "state": 0x61100}] * 20
    p = _collect(monkeypatch, antivirus=many)
    assert len(p["antivirus"]) == inventory.MAX_AV
    assert len(p["antivirus"][0]["name"]) == inventory.MAX_AV_NAME
    InventoryPayload.model_validate(p)


def test_schema_rejects_oversized_av_name():
    with pytest.raises(ValueError):
        InventoryPayload.model_validate(
            {"antivirus": [{"name": "A" * (inventory.MAX_AV_NAME + 1)}]}
        )


def test_script_blocks_guarded_and_language_independent():
    s = inventory._SCRIPT
    for marker in (
        "Win32_QuickFixEngineering",
        "root/SecurityCenter2",
        "Win32_EncryptableVolume",
        "Win32_Tpm",
        "UEFISecureBootEnabled",
        "SoftwareLicensingProduct",
    ):
        assert marker in s
    assert "ToString('yyyy-MM-dd')" in s
    assert "Confirm-SecureBootUEFI" not in s  # бросает без админа/на legacy BIOS
