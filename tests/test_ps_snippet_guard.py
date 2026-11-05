"""Стражи формы PowerShell-сниппетов агента (агент 0.3.5, spec 2026-10-24-agent-0.3.5).

Различение «PowerShell сломан» и «законно пусто» держится на том, что КАЖДЫЙ сниппет
кончается безусловным ``... | ConvertTo-Json``: здоровый прогон всегда печатает JSON
(rc 0), пустой stdout = последний оператор не выполнился (живой замер: 9/9 сломанных,
0/9 здоровых). Постамбула run_ps берёт FullyQualifiedErrorId последней ошибки, поэтому
сниппет не должен сам выходить (``exit`` прячет сбой), бросать произвольный текст
(``throw`` -- текст становится FQID) или глушить ошибку мимо ``$Error`` (``-EA Ignore``).
"""

from __future__ import annotations

import re

import pytest
from client.collectors import (
    events,
    heartbeat,
    historical,
    inventory,
    network,
    print_jobs,
    printer_ports,
    smart,
)
from client.tray import certs

pytestmark = pytest.mark.unit

_SNIPPETS = {
    "events": events._SCRIPT,
    "heartbeat": heartbeat._SCRIPT,
    "inventory": inventory._SCRIPT,
    "historical": historical._SCRIPT,
    "historical_cert": historical._CERT_SCRIPT,
    "smart": smart._SMART_SCRIPT,
    "printer_ports": printer_ports._SCRIPT,
    "print_mode": print_jobs._MODE_SCRIPT,
    "print_counter": print_jobs._COUNTER_SCRIPT,
    "print_sweep_first": print_jobs._build_script(""),
    "print_sweep_since": print_jobs._build_script("2026-01-01T00:00:00Z"),
    "network": network._NET_SCRIPT,
    "network_rescan": network._NEIGHBOR_RESCAN_SCRIPT,
    "tray_cert": certs._CERT_SCRIPT,
}
_FORBIDDEN = ("exit", "throw", "-ErrorAction Ignore", "-EA Ignore", "Write-Error", "Write-Warning")


@pytest.mark.parametrize("name", sorted(_SNIPPETS))
def test_snippet_ends_with_convertto_json(name):
    assert re.search(r"[|]\s*ConvertTo-Json\b[^|;]*$", _SNIPPETS[name].rstrip()), name


@pytest.mark.parametrize("name", sorted(_SNIPPETS))
def test_snippet_has_no_construct_that_hides_the_failure_token(name):
    script = _SNIPPETS[name]
    found = [w for w in _FORBIDDEN if re.search(r"(?i)(^|[^\w-])" + re.escape(w) + r"\b", script)]
    assert not found, (name, found)


def test_agent_reason_cap_and_alphabet_match_the_contract():
    """Агент режет/чистит причину ровно как сервер (правило §5: один порог)."""
    from client.collectors import ps
    from shared import schema

    assert ps.REASON_MAX == schema.SOURCE_REASON_MAX
    assert ps._REASON_BAD_CHARS.pattern == schema._REASON_BAD_CHARS.pattern
