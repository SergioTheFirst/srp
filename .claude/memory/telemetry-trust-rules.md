---
name: telemetry-trust-rules
description: Контракт слоя доверия телеметрии (W0.3) — state=gate/weight=modulation, collector⊥semantic, materiality, UNKNOWN first-class
metadata:
  type: project
---

Слой telemetry-trust (P0) решает, может ли система вообще делать утверждение по сигналу.
Полный контракт — `telemetry-trust-contract.md`; имплемент-план (Plan 1, Trust Core) —
`telemetry-trust-plan.md` (корень репо). Load-bearing правила:

- **`state` = authoritative gate, `weight` = только модуляция.** weight НИКОГДА не
  реанимирует gate-failed источник (unavailable/stale/suspect весом не компенсируются).
  Предотвращает probabilistic soup и «компенсацию неизвестности» соседними сигналами.
- **collector-trust ⊥ semantic-trust.** «Данные пришли» ≠ «данные правдивы» (пример: OEM
  thermal-zone = свежая фейк-константа → collector HIGH, semantic LOW). collector ← агент
  (факты сбора), semantic ← сервер (суждение, эволюционирует — не в агенте).
- **Materiality governor:** semantic-валидация ТОЛЬКО для decision-material сигналов
  (SMART/battery/free_space/RSI/boot — да; CPU%/queue/raw perf — нет; thermal — только
  frozen-check). Иначе строим «оценку качества телеметрии», а не «систему деградации ПК».
- **UNKNOWN — first-class исход** (HEALTHY/DEGRADED/AT RISK/UNKNOWN). required-источник
  деградировал → домен UNKNOWN, не «здоров», не сворачивается в оптимистичный агрегат.
- **Scope ceiling:** стоп на state/weight/collector_trust/semantic_trust/freshness. НЕ
  строить nested confidence / uncertainty trees / evidence DAGs / confidence calculus.

**How to apply:** любую правку trust-слоя сверять с §13 контракта (scope ceiling) и
прайм-директивой [[system-class-high-trust]]. Связано: [[coverage-scope]] (агент = факты,
E2E), [[language-independence]] (доступность источников зависит от локали/прав).
