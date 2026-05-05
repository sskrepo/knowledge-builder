---
title: Module — FA semantic graph (spec §4.5)
source: docs/raw/knowledge-builder-framework-spec.md (§4.5)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [module, graph, fa, phase-4]
status: current
---

# Module — FA semantic graph

> **Status: graph-based; Dave's POC integrates here.** Phase 4 ships this. Sits inside the converged Autonomous DB per [ADR-002](adr/ADR-002-storage-shape.md).

## Sources
- **FA schema definitions** — object types, fields, relationships
- **Business rules** — referential constraints, validity rules, dependency declarations

## Ingestion (rule-driven, deterministic — spec §2.3)
- Rule-driven extraction into a **property graph**: nodes = FA objects, edges = relationships, JSON properties = constraints
- LLM only used to generate human-readable summaries on `node_summaries` (which are then embedded for "find starting node" via vector search)

## Storage
- **`kb_fa_semantic` schema** in Autonomous DB
- Oracle Property Graph (PG) — native vertex tables + edge indexes layered on relational data
- Vector embeddings on `node_summaries` — entry points for "I don't know the exact node name" queries

## Retrieval
- **Vector search → graph traverse** pattern (spec §4.5):
  1. `vector_search(corpus="fa_semantic_summaries", query)` — find candidate starting node(s)
  2. `graph_traverse(start_entity, edge_types, depth)` — deterministic walk from there
- Direct graph queries via PG/Cypher when caller knows the entity URN

## Sample queries
- "If I add object X with rule Y, which existing constraints are violated?" (impact analysis)
- "What downstream objects depend on table T?" (blast radius)
- "Show all objects that participate in the order-fulfillment chain" (path finding)

## Acceptance criteria (Phase 4)
- Round-trip `vector_search → graph_traverse` returns cited paths in <1s p95
- Graph re-ingest is idempotent on FA schema unchanged
- Constraint-violation queries return both the rule and the conflicting object IDs

## Open items
- **Integration with Dave's POC** — current location of POC code/data; how to merge into framework's `kb_fa_semantic`. Dave to brief at Phase 4 kickoff.
- **Sync cadence** — FA schema definitions evolve; need webhook or scheduled diff against the source-of-truth schema repo.
- **Phase 1/2 priority** — graph isn't on the Phase 1 critical path; do not let Phase 4 work jump the queue.
