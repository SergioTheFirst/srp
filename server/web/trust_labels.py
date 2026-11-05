"""Русская причина значка `untrusted` (2026-10-14).

`untrusted` = источник identity (инвентаризация агента) не прошёл гейт доверия.
Владелец читал голое слово как проблему токена -- поэтому каждая причина
называет реальный сбой и прямо говорит, что токен ни при чём. Вход -- машинные
значения из scores.risk.identity_trust (state + collector_status).
Плюс строка «Сбои сбора» карточки ПК (находка 10, 2026-10-15).
"""

from __future__ import annotations

from typing import Any, Optional

from server import db
from server.pipeline import _EMPTY_COERCIBLE_SOURCES, _KNOWN_TRUST_SOURCES

_TAIL = " С токеном не связано."

_BY_COLLECTOR = {
    "timeout": "Инвентаризация (PowerShell) не уложилась в отведённое время.",
    "empty": "PowerShell ничего не вернул при инвентаризации (антивирус или политика?).",
    "blocked": "Запуск PowerShell для инвентаризации запрещён.",
    "absent": "PowerShell не найден на ПК.",
}
# Находка 10: тот же статус сборщика, но для ЛЮБОГО источника -- коротко, в строку.
_COLLECTOR_RU = {
    "timeout": "не уложился во время",
    "blocked": "запуск запрещён",
    "absent": "PowerShell не найден",
    "partial": "неполные данные",
    "empty": "пустой ответ",
}
# Агент 0.3.5: токен причины сбоя PowerShell (машинное, англ.) -> подсказка оператору
# (русская). Модульные токены = PowerShell не грузит свои модули (Dietnew: битый .NET в
# GAC); rc_<код> = процесс упал без описания; незнакомый -- нейтрально, как есть.
PS_ENGINE_TOKENS = frozenset({"CommandNotFoundException", "CouldNotAutoloadMatchingModule"})
_RESTART = (
    ' Затем перезапустить агента: schtasks /end /tn "SRP Agent" и'
    ' schtasks /run /tn "SRP Agent" — свежие данные придут минут через пять.'
)
_STALE = "Давно нет свежей инвентаризации."
_SUSPECT = "Описание ПК в инвентаризации противоречиво."
_GENERIC = "Сервер не получил подтверждённого описания ПК (инвентаризации)."


def ps_reason_ru(code: Optional[str]) -> Optional[str]:
    """Подсказка по токену причины сбоя PowerShell; нет токена -> None."""
    if not code:
        return None
    if code in PS_ENGINE_TOKENS:
        return (
            "PowerShell на ПК не загружает свои модули — обычно повреждён .NET Framework."
            " Починить: .NET Framework Repair Tool (при ошибках диска — chkdsk)." + _RESTART
        )
    if code == "cim_hollow":
        return (
            "WMI/CIM на ПК не отвечает: проверить службу «Инструментарий управления Windows»"
            " (winmgmt)." + _RESTART
        )
    if code.startswith("rc_"):
        return (
            f"PowerShell падает без описания (код {code[3:]}): повреждённый .NET/PowerShell,"
            " антивирус/EDR или ограниченный режим языка. См. docs/agent-install.md,"
            " раздел «Диагностика»."
        )
    return f"Сбой скрипта сбора: {code}."


def identity_reason_ru(identity_trust: Optional[dict[str, Any]]) -> str:
    """Причина недоверия по-русски; неизвестный вход -> общая фраза."""
    trust = identity_trust or {}
    state = trust.get("state")
    ps_hint = ps_reason_ru(trust.get("collector_reason"))
    if state == "unavailable" and ps_hint:
        return ps_hint + _TAIL
    if state == "stale":
        text = _STALE
    elif state == "suspect":
        text = _SUSPECT
    elif state == "unavailable":
        text = _BY_COLLECTOR.get(str(trust.get("collector_status")), _GENERIC)
    else:
        text = _GENERIC
    return text + _TAIL


def collector_status_ru(status: str, reason: Optional[str] = None) -> str:
    """Сбой сборщика по-русски; незнакомое значение -- как есть (fail-open).
    С токеном причины (агент 0.3.5) -- «пустой ответ (CommandNotFoundException)»."""
    text = _COLLECTOR_RU.get(status, status)
    return f"{text} ({reason})" if reason else text


def collector_failures(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Строка «Сбои сбора»: последний сбой каждого ТЕКУЩЕГО источника (вход --
    db.get_source_trusts). Выведенный (battery) молчит, незнакомый статус виден
    (fail-open). «empty» -- сбой только там, где пустота незаконна: events/
    print_jobs/certificates законно пусты (то же правило, что в evaluate_trust) --
    если только агент не назвал причину сбоя (0.3.5): тогда это не пустота, а поломка."""
    return [
        {
            "source": src,
            "reason": collector_status_ru(row["collector_status"], row.get("collector_reason")),
            "age_sec": db.age_seconds(row["ts"]),
        }
        for src, row in sorted(rows.items())
        if src in _KNOWN_TRUST_SOURCES
        and row["collector_status"] != "ok"
        and (
            row["collector_status"] != "empty"
            or src in _EMPTY_COERCIBLE_SOURCES
            or row.get("collector_reason")
        )
    ]
