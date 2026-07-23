"""P3-8: printer_status_ru should return raw status code, not generic placeholder."""

from __future__ import annotations

from server.web.dashboard import printer_status_ru


def test_printer_status_ru_known_statuses():
    """Known status codes should return the mapped label."""
    assert printer_status_ru("idle", online=True) == ("good", "готов")
    assert printer_status_ru("printing", online=True) == ("accent", "печать")
    assert printer_status_ru("warmup", online=True) == ("warn", "разогрев")
    assert printer_status_ru("stopped", online=True) == ("bad", "остановлен")
    assert printer_status_ru("unknown", online=True) == ("na", "неизвестно")


def test_printer_status_ru_offline():
    """When offline or unreachable, return standard offline label."""
    assert printer_status_ru("idle", online=False) == ("bad", "недоступен")
    assert printer_status_ru("unreachable", online=True) == ("bad", "недоступен")
    assert printer_status_ru("any_status", online=False) == ("bad", "недоступен")


def test_printer_status_ru_other_and_unknown_fallback_return_raw_code():
    """Unmapped status codes (like 'other') should return the raw code.

    This aligns with net_type_ru and net_change_ru behavior:
    - net_type_ru("unknown_type") → returns the raw string
    - net_change_ru("unknown_change") → returns the raw string
    - printer_status_ru("other", online=True) should also return raw code

    Currently (RED): "other" returns ("na", "—") — generic placeholder.

    After fix (GREEN): should return ("na", "other") to match the fallback
    pattern and align with sibling functions.
    """
    # Test that "other" status (currently hardcoded to "—") should return raw code
    result_other = printer_status_ru("other", online=True)
    assert result_other[0] == "na"  # chip class stays "na"
    assert result_other[1] == "other", (
        f"Expected 'other' status to return raw code, got {result_other[1]!r}"
    )

    # Test that unmapped status codes also return raw code
    result_unmapped = printer_status_ru("custom_status", online=True)
    assert result_unmapped[0] == "na"
    assert result_unmapped[1] == "custom_status"
