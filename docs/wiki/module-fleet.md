---
title: Module — Fleet data (spec §4.2)
source: docs/raw/knowledge-builder-framework-spec.md (§4.2)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [module, fleet, sql, phase-2]
status: current
---

# Module — Fleet data

> **Status: straightforward** — no debate. Schema-defined data with instances → relational store, full stop (spec §2.6). No LLM in ingestion.

## Sources
**UDAP / Sentinel** — existing structured rows: instances, pods, properties, ops events.

## Ingestion
**None via LLM.** Data already lives in its native store. The framework wraps it read-through; no copy, no re-ingest.

## Storage
- **Existing UDAP / Sentinel** (no new tables in `kb_*` schemas)
- Allowlisted views configured in `framework/retrievers/fleet_views.yaml` (version-controlled)

## Retrieval (two MCP tools)
- `query_fleet(view, filters, projection)` — typed query for known shapes; allowlisted views only.
- `text_to_sql(nl_query)` — constrained NL→SQL; can only target the allowlisted view set; rejects DDL and any reference to base tables.

## Optional v2 enhancement
Materialize **hot rollups** (counts by patch level, top-N noisy tenants) on a schedule and promote those summaries into the wiki layer so the Context Builder doesn't need to hit fleet for every "summary" question. Ship only if eval shows latency wins.

## Sample queries
- "How many instances are on patch 24.05.1?"
- "Which pods owned by team Z had restart spikes this week?"
- "What's the global count of customers on FA release 25.01 broken down by region?"

## Acceptance criteria
- `query_fleet` with allowlisted view returns rows + `citation_url` (link to source row in UDAP/Sentinel)
- `text_to_sql` rejects any query touching non-allowlisted tables (security gate; tested per PR)
- p95 latency for typed queries <300ms (Sentinel-bound)

## Open items
- **Allowlist authoring** — who decides which views become "knowledge"? Default: each persona team proposes views in its builder config; framework merges into a global allowlist.
- **Cost tracking** — Sentinel queries don't go through the OpenAI cost path; need a separate "fleet query cost" telemetry channel.
