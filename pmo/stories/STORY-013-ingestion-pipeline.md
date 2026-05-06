---
title: STORY-013 — Ingestion pipeline (idempotency)
status: drafted
phase: 1
size: L
owner: dev
---
## Acceptance criteria
- [x] adapter → parser → store flow
- [x] Idempotent on `source_sha` match
- [x] Batched store writes
- [ ] Per-source error isolation (one bad item doesn't kill batch)
