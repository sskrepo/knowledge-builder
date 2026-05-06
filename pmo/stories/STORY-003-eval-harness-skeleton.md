---
title: STORY-003 — Eval harness skeleton
status: drafted
phase: 1
size: M
owner: qa
---
## User story
As QA, I want recall@k / latency / cost / faithfulness metrics computable from a gold-set JSONL so PRs can be gated.

## Acceptance criteria
- [x] `framework/eval/runner.py` runs gold-set against orchestrator
- [x] Metrics: recall@5, MRR, recency-weighted recall, latency percentiles
- [x] Markdown + JSON report rendering
- [ ] Faithfulness via Ragas (Phase 2 wiring; placeholder now)
