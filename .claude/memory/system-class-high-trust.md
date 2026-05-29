---
name: system-class-high-trust
description: SRP — high-trust degradation detection platform (не «AI предсказывает отказы»); под неопределённостью → UNKNOWN, не угадывание
metadata:
  type: project
---

Класс системы зафиксирован как **high-trust degradation detection platform**, а НЕ «AI
предсказывает отказы ПК». Это смена класса, не маркетинг.

**Why:** предсказуемость отказов доменно-ограничена — реально прогнозируемо: износ
накопителя, батарея, заполнение диска, тренд boot-time, fleet-anomaly, throttle, driver-
regression; остальное (random BSOD, VRM/PSU/мать, intermittent, док) — постфактум. Главный
риск проекта — строить ML поверх недостоверной телеметрии. Поэтому telemetry-trust — P0 до
любой аналитики (см. [[telemetry-trust-rules]]), а ML/survival/петля меток — гейтованные
фазы (Phased), не ядро (см. [[bayesian-weights-uncalibrated]]).

**How to apply:** прайм-директива — быть правым в том, что знаешь, и честным в том, чего не
знаешь: под неопределённостью система ОТКАЗЫВАЕТСЯ от утверждения (UNKNOWN — first-class
исход), не угадывает. Не строить unified ML-оракул / «general PC health AI» / random-death
prediction. Роадмап развития — `cctodo.md`.
