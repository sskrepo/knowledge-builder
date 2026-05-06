---
title: STORY-008 — vector_search + get_incident_summary MCP tools
status: drafted
phase: 1
size: M
owner: dev
---
## Acceptance criteria
- [x] `vector_search(corpus, query, filters, k, persona)` builds SQL with hard/soft filter strictness (ADR-013)
- [x] Recency tiebreaker in ORDER BY (AIRA pattern)
- [x] Score = `1 / (1 + VECTOR_DISTANCE(...))`
- [x] Coarse-then-fine: fetch 2x, app-side cut
- [x] `get_incident_summary(incident_id)` direct lookup
- [x] `citation_url` mandatory on every Result
