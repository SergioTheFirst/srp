"""security_flags (агент 0.3.8): null = неизвестно -> чипа нет; пороги -- константы модуля."""

from __future__ import annotations

from datetime import date

import pytest
from server.web.security_flags import PATCH_WARN_DAYS, UPTIME_WARN_DAYS, security_flags

pytestmark = pytest.mark.unit

_TODAY = date(2026, 10, 27)


def _texts(inv, hb=None):
    return [f["text"] for f in security_flags(inv, hb, _TODAY)]


def test_all_unknown_gives_no_chips():
    assert security_flags({"model": "X"}, None, _TODAY) == []
    assert security_flags(None, None, _TODAY) == []


def test_healthy_machine_gives_no_chips():
    inv = {
        "last_patch_date": "2026-10-20",
        "antivirus": [{"name": "D", "enabled": True, "up_to_date": True}],
        "bitlocker_system": "on",
        "tpm_present": True,
        "tpm_version": "2.0",
        "secure_boot": True,
        "windows_activated": True,
    }
    assert security_flags(inv, {"uptime_hours": 5}, _TODAY) == []


def test_uptime_threshold():
    assert _texts({}, {"uptime_hours": UPTIME_WARN_DAYS * 24 - 1}) == []
    assert _texts(None, {"uptime_hours": 40 * 24}) == ["без перезагрузки 40 дн"]


def test_patch_threshold_and_garbage():
    assert _texts({"last_patch_date": "2026-09-01"}) == []
    old = _texts({"last_patch_date": "2026-01-01"})
    assert old == ["обновлений Windows не было 299 дн"]
    assert PATCH_WARN_DAYS == 60
    assert _texts({"last_patch_date": "garbage"}) == []


def test_antivirus_levels():
    assert _texts({"antivirus": []}) == ["антивирус не найден"]
    off = [{"name": "D", "enabled": False, "up_to_date": True}]
    assert _texts({"antivirus": off}) == ["антивирус выключен"]
    stale = [{"name": "D", "enabled": True, "up_to_date": False}]
    assert _texts({"antivirus": stale}) == ["базы антивируса устарели"]


def test_bitlocker_laptop_warn_desktop_accent():
    lap = security_flags({"bitlocker_system": "off", "chassis": "laptop"}, None, _TODAY)
    desk = security_flags({"bitlocker_system": "off", "chassis": "desktop"}, None, _TODAY)
    assert lap[0]["level"] == "warn" and desk[0]["level"] == "accent"
    assert _texts({"bitlocker_system": "unknown"}) == []


def test_win11_readiness():
    assert _texts({"tpm_present": True, "tpm_version": "1.2"}) == ["не готов к Windows 11"]
    assert _texts({"tpm_present": True, "tpm_version": "2.0", "secure_boot": True}) == []
    flag = security_flags({"tpm_present": False, "secure_boot": False}, None, _TODAY)[0]
    assert flag["title"] == "нет TPM 2.0, Secure Boot выключен"


def test_activation():
    assert _texts({"windows_activated": False}) == ["Windows не активирована"]
    assert _texts({"windows_activated": None}) == []
