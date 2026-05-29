---
name: agent-stdlib-only
description: Агент client/ обязан оставаться на чистом стандартном Python без сторонних пакетов
metadata:
  type: project
---

Агент (`client/`) — **чистый стандартный Python**: `urllib`, `subprocess`, `json`,
`winreg`. Ни один пакет из `requirements.txt` (fastapi/pydantic/uvicorn/jinja2) не
должен импортироваться в `client/`.

**Why:** офисные ПК часто без установленного Python и без доступа к pip; агент должен
ставиться копированием файла (или сборкой в `.exe` через PyInstaller вне рамок MVP).
Любая зависимость ломает массовое развёртывание.

**How to apply:** при правках в `client/` проверяй импорты; контракт сообщений дублируй
вручную или импортируй только из stdlib, не из `shared/schema.py` с pydantic. Связано с
[[coverage-scope]] (поэтому агент не покрыт юнит-тестами, а проверяется `smoke.py`/E2E).
