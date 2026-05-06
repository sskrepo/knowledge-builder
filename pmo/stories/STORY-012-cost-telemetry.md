---
title: STORY-012 — Cost telemetry pipeline
status: drafted
phase: 1
size: M
owner: dev
---
## Acceptance criteria
- [x] `LLMClient` emits `CostEvent` per call
- [x] Pricing table in `framework/eval/prices.yaml`
- [ ] Sink to `kb_shim.cost_log` (needs ADB)
- [ ] Daily $/persona/operation rollup query
- [ ] Alarm on outlier (>3σ above 7-day moving avg)
