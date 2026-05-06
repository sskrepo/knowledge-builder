---
title: STORY-005 — IncidentVectorStore + DDL
status: drafted
phase: 1
size: L
owner: dev
---
## User story
As an ingestion worker, I want to write `ContentItem + Chunk + Edge` rows into `kb_incidents` schema with idempotency.

## Acceptance criteria
- [x] DDL in `framework/stores/sql/kb_incidents.sql` runs idempotently
- [x] `migrate()` handles ORA-00955 (already-exists)
- [x] `upsert()` MERGEs by `id` (sha256-derived)
- [x] In-DB embedding via `batch_insert_datasets_vectors_kbi` (ADR-012)
- [x] Multi-axis JSON-path indexes for filters (ADR-008)
- [ ] Integration test against real ADB (post-provisioning)
