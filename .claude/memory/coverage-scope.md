---
name: coverage-scope
description: Порог покрытия 80% считается только по server + shared; агент — через живой E2E
metadata:
  type: project
---

Покрытие (`fail_under = 80`) считается **только по `server` и `shared`** — это
аналитическое ядро. Агент (`client/`) намеренно вне юнит-покрытия.

**Why:** агент шеллит в PowerShell и читает реестр Windows — это проверяется живым
прогоном (`smoke.py` для E2E без сети, реальный запуск на Windows), а не моками.
Тянуть агент под юнит-покрытие провоцировало бы мокать всё подряд без сигнала. Связано
с [[agent-stdlib-only]].

**How to apply:** не понижай порог 80% и не добавляй `client/` в `[tool.coverage.run]
source`. Тесты сервера/контракта идут на Windows в CI (матрица py3.9/3.11/3.12) —
winreg/PowerShell живут там. Новой логике в `server`/`shared` нужны тесты до коммита.
