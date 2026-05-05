---
title: Module — Operational incidents (spec §4.1)
source: docs/raw/knowledge-builder-framework-spec.md (§4.1)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [module, incidents, aira, phase-1]
status: current
---

# Module — Operational incidents

> **Status: proven path** — Aira's existing KB validates this approach. Phase 1 ships this module end-to-end.

## Sources
- **Jira** — incident tickets (`P2T-*`, `INC-*`, etc.)
- **Log files** — referenced from tickets; ingested as attachments or links
- **Service / pod metadata** — joined from fleet for resource context

## Ingestion (LLM-driven)
For each incident, the LLM parser produces a structured summary with:
- root cause (≤500 chars)
- impact (blast radius, duration)
- resources affected (services, pods, tenants)
- ORA error codes (if any)
- resolution summary

Raw fields (ticket title, body, comments timeline, labels, components) are indexed alongside the summary as chunk metadata so that exact-match filters still work. Schema lives at `framework/parsers/schemas/incidents/v1.json` (spec §6.2 contract).

## Storage
- **Vector store** (`kb_incidents` schema): chunked summaries + raw field metadata
- **Graph edges** (also in `kb_incidents`): `incident → service → owner → tenant`
- Embeddings: OpenAI `text-embedding-3-large` (3072 dims)

## Retrieval
- Primary tool: `vector_search(corpus="incidents", query, filters?)` with metadata filtering on service / tenant / time window.
- `get_incident_summary(incident_id)` for direct lookups.
- `graph_traverse(start_entity, edge_types, depth)` for blast-radius / dependency questions.
- **Not** LLM-driven traversal; Aira evals showed vector + filtered graph beat LLM and lexical alternatives.

## Sample queries
- "What incidents touched auth-service in the last 30 days?"
- "Show resolutions for ORA-1017 errors on tenant-123"
- "What's the blast radius if customer-events topic goes down?" (combines historical incidents + dependency graph)

## v1 acceptance criteria (subset of spec §12)
- New incident ingest → retrievable in <5 min
- `vector_search` returns top-5 with citations <500ms p95
- ≥80% recall on `eval/gold_sets/incidents.jsonl` (25 questions)
- Re-ingest = no-op when source unchanged (idempotency)

## Open items
- **Webhook source for Jira changes** — Phase 1 deliverable (spec §10 incremental updates).
- **Log-attachment ingest budget** — large logs blow token budgets; Phase 1 implements truncation + sampling rules.
