---
title: STORY-006 — Gold set expansion to 25 questions
status: drafted
phase: 1
size: S
owner: qa
---
## User story
As QA, I want the incident gold set to have 25 representative questions sourced from AIRA's existing eval harness.

## Acceptance criteria
- [ ] Contact AIRA team for ~50 query/citation pairs (ADR-005 amend 1)
- [ ] Pick 25 representative; replace placeholders in `eval/gold_sets/incidents.jsonl`
- [ ] Each question has `expected_citations`, `min_recall_at_5`, `min_faithfulness`, optional `filters_strictness` (ADR-005 amend 3)
