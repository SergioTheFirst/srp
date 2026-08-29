"""Tray config.json field parsing (KodSR L1, U1-f).

``int()``/``bool()`` applied directly to whatever JSON holds crashed the tray
at startup on a malformed value ("abc") and silently mis-parsed a string
"false" as True (``bool("false")`` is True in plain Python). Per-field
fallback to the spec §3 default fixes both without changing the happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

from client.tray.__main__ import _int_field, _read_cert_config, _str_bool

# --------------------------------------------------------------------------- #
# _int_field
# --------------------------------------------------------------------------- #


def test_int_field_non_numeric_string_falls_back_to_default() -> None:
    assert _int_field({"n": "abc"}, "n", 14) == 14


def test_int_field_valid_number_passes_through() -> None:
    assert _int_field({"n": 30}, "n", 14) == 30


def test_int_field_missing_key_uses_default() -> None:
    assert _int_field({}, "n", 14) == 14


def test_int_field_zero_falls_back_to_default() -> None:
    """Pre-existing behaviour (falsy value -> default), kept as-is."""
    assert _int_field({"n": 0}, "n", 14) == 14


# --------------------------------------------------------------------------- #
# _str_bool
# --------------------------------------------------------------------------- #


def test_str_bool_string_false_is_false() -> None:
    assert _str_bool("false") is False


def test_str_bool_string_true_variants_are_true() -> None:
    assert _str_bool("true") is True
    assert _str_bool("TRUE") is True
    assert _str_bool("1") is True
    assert _str_bool("yes") is True


def test_str_bool_real_bool_passes_through() -> None:
    assert _str_bool(True) is True
    assert _str_bool(False) is False


def test_str_bool_other_types_default_false() -> None:
    assert _str_bool(None) is False
    assert _str_bool(1) is False  # not a bool instance -- int 1 is not True here
    assert _str_bool("garbage") is False


# --------------------------------------------------------------------------- #
# _read_cert_config -- end to end
# --------------------------------------------------------------------------- #


def _write_config(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_read_cert_config_missing_file_returns_spec_defaults(tmp_path: Path) -> None:
    result = _read_cert_config(tmp_path / "nope.json")
    assert result == (14, 4, False, "")


def test_read_cert_config_malformed_int_field_degrades_to_default(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path / "config.json",
        {"tray_cert_warn_days": "abc", "tray_notify_hours": 8, "tray_require_cert": True},
    )
    warn_days, notify_hours, require_cert, _helpdesk = _read_cert_config(cfg)
    assert warn_days == 14  # fell back
    assert notify_hours == 8  # unaffected field passes through
    assert require_cert is True


def test_read_cert_config_string_false_require_cert(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "config.json", {"tray_require_cert": "false"})
    _warn, _notify, require_cert, _helpdesk = _read_cert_config(cfg)
    assert require_cert is False


def test_read_cert_config_real_bool_require_cert(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "config.json", {"tray_require_cert": True})
    _warn, _notify, require_cert, _helpdesk = _read_cert_config(cfg)
    assert require_cert is True
