r"""Одна best-effort прямая отправка данных владельца на сервер (o5 блок F, F11).

Гарантия доставки лежит на спуле + SYSTEM-агенте (F8/F9): этот модуль лишь
пытается сократить задержку до следующего агентского цикла. Без буферизации и
без ретраев -- неудача просто оставляет данные ждать агента. Чистый stdlib.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from client.transport import AGENT_VERSION

_TIMEOUT_SEC = 5


def _read_config(config_path: Path) -> dict:
    """Теми же средствами, что ``panel._server_url`` -- ничего не пишет и не
    поднимает исключений на битом/отсутствующем файле."""
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def push_owner_identity(config_path: Path, full_name: str, position: str, phone: str) -> bool:
    """Отправить минимальный конверт с тремя полями; True при успехе (2xx)."""
    data = _read_config(config_path)
    server_url = str(data.get("server_url", "")).strip()
    if not server_url:
        return False
    envelope: dict[str, Any] = {
        "device_id": str(data.get("device_id", "")),
        "agent_version": AGENT_VERSION,
        # liveness, НЕ heartbeat: пустой heartbeat становится «последним» замером
        # и стирает реальную телеметрию из скоринга (замерено: performance 62.5 ->
        # 100.0 после одного клика в трее). Ветка liveness несёт те же три поля,
        # но не пишет строк телеметрии, не участвует в trust и не рескорит.
        "msg_type": "liveness",
        "ts": datetime.now(timezone.utc).isoformat(),
        "payload": {"alive": True},
        "owner_full_name": full_name or None,
        "owner_position": position or None,
        "owner_phone": phone or None,
        "idempotency_key": uuid.uuid4().hex,
    }
    body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = str(data.get("ingest_token", ""))
    if token:
        headers["X-SRP-Token"] = token
    req = urllib.request.Request(
        server_url.rstrip("/") + "/api/v1/ingest", data=body, method="POST", headers=headers
    )
    try:
        # B310: схема берётся из server_url операторского config.json, а не из
        # пользовательского ввода -- то же обоснование, что в client/transport.py.
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC):  # nosec B310
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
