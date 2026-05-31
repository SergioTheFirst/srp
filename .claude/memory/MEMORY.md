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
