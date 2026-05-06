---
title: STORY-014 — ops_eng persona skill (full ADR-007)
status: drafted
phase: 1
size: L
owner: dev
---
## Acceptance criteria
- [x] BasePersonaSkill with prompt + KB selection LLM call
- [x] Filter merge (intent → skill defaults; ADR-013)
- [x] Parallel retrieval where possible
- [x] Char-cap dedup (ADR-007 amend 1)
- [x] ContextPacket output with confidence + notes
