---
name: agent-service-task-scheduler
description: Агент под LocalSystem запускается Планировщиком заданий, не nssm/sc; конфиг пишется без BOM
metadata:
  type: project
---

Боевой запуск агента под **LocalSystem (SYSTEM)** — через **Планировщик заданий**
(`client/deploy/install-service.ps1`: AtStartup, `-UserId SYSTEM -RunLevel Highest`,
рестарт 3×1мин, без лимита времени), а **не** nssm/sc.

**Why:** настоящая Windows-служба требует процесс, отвечающий на сообщения SCM; чистый
Python-цикл этого не умеет, а единственный нормальный способ — `pywin32` — запрещён
инвариантом [[agent-stdlib-only]]. Планировщик даёт реальную цель (непрерывный запуск
как SYSTEM, автостарт, рестарт) нативно и без сторонних бинарей. SYSTEM нужен, чтобы
разблокировать привилегированные сборщики (SMART/`StorageReliabilityCounter`/часть WMI),
иначе слой доверия помечает их источники UNKNOWN.

**How to apply:**
- Не предлагать «сделать настоящую службу» без отказа от stdlib-инварианта — это осознанный размен.
- PowerShell, пишущий `client/config.json`, ОБЯЗАН писать UTF-8 **без BOM**
  (`[System.IO.File]::WriteAllText(..., New-Object System.Text.UTF8Encoding($false))`):
  `Set-Content -Encoding utf8` в Windows PowerShell 5.1 добавляет BOM → `json.loads` падает.
- Установка валидирует одним проходом `python -m client.agent --once` **без** `--server`
  (читает только что записанный конфиг — тот же путь, что у задачи).
- `--log-file` включает stdlib `RotatingFileHandler` (у SYSTEM-задачи нет консоли);
  стартовая строка лога пишет `user=` и редактирует `user:pass@` из `server_url`.
- Скрипты `.ps1` не покрываются CI (Linux) — только AST-parse + ручная проверка ([[coverage-scope]]).
