# Память проекта SRP

Индекс долговременной памяти. Читается в начале каждой сессии. Одна строка на
заметку: `- [Заголовок](файл.md) — крючок`. Содержимое заметок — в самих файлах,
здесь только указатели. Подробности использования — в `CLAUDE.md`.

- [Агент — только stdlib](agent-stdlib-only.md) — `client/` без сторонних зависимостей, никогда
- [Языконезависимость сбора](language-independence.md) — CIM-классы и числовой Level вместо локализованных путей
- [Байесовские веса не калиброваны](bayesian-weights-uncalibrated.md) — ручная заглушка до survival-модели
- [Покрытие только server + shared](coverage-scope.md) — агент проверяется живым E2E, не юнит-покрытием
- [Класс системы: high-trust](system-class-high-trust.md) — degradation detection platform, не «AI предсказывает»; под неопределённостью → UNKNOWN
- [Контракт telemetry-trust](telemetry-trust-rules.md) — state=gate/weight=modulation, collector⊥semantic, materiality, scope ceiling
- [Как работать с пользователем](working-style-governors.md) — анти-сикофантия в обе стороны, анти-оверинжиниринг, строгие контракты, прозовый ко-дизайн
- [Служба агента = Планировщик заданий](agent-service-task-scheduler.md) — LocalSystem через schtasks, не nssm/sc (pywin32 запрещён); конфиг без BOM
- [Коды org/dept = ярлыки, не тенантность](identity-labels-not-tenancy.md) — изоляция/RBAC паркуются до 2-го недоверяющего клиента; глобального ingest_token достаточно
- [Батарея: вздутие ≠ возраст](battery-swelling-not-age.md) — battery_risk судит только об износе ёмкости; вздутие невидимо → уверенность ≤ medium, ёмкость ≠ безопасность
- [Заполнение диска: медиана = защита от rebound](disk-fill-median-rebound-guard.md) — disk_fill_risk судит по медиане свободного места за 14 дней; разовый спад очистки WU не сдвигает медиану → нет ложной тревоги
- [Трей = отдельный процесс в сессии пользователя](tray-split-plane.md) — SYSTEM не видит CurrentUser\My пользователя; IPC = односторонний status.json, канала команд нет
- [Сертификаты: subject-группировка + окна 14дн/4ч, 7дн/1×день](cert-subject-grouping.md) — преемник того же Subject гасит напоминания старого; после истечения 1/день × 7, потом тишина
- [Печать: авторежим events⇄counter](print-counter-fallback.md) — выбор режима каждый sweep по IsEnabled; counter = дельты TotalPagesPrinted c reset-detect; source additive
- [Справочник орг/отделов: файл, имена render-time](org-directory-render-time.md) — имена никогда не в БД; переименование мгновенно на всю историю; devices.department DEPRECATED
