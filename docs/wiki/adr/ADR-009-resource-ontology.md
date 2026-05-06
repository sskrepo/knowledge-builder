---
title: ADR-009 — Resource ontology in shim_faaas
status: accepted
created: 2026-05-05
owner: architect
tags: [adr, ontology, graph, phase-2]
related: [ADR-006, ADR-008, PDD]
---

# ADR-009 — Resource ontology

## Status
Accepted (2026-05-05). First-class graph for FAaaS resources, layered onto spec §4.5's FA semantic graph store.

## Context
Resources (POD, PODDB, EXADATA, BLOCK_VOLUME, NETWORK, …) appear in every persona's content. They have real relationships (POD *contains* PODDB; PODDB *runs on* EXADATA; EXADATA *uses* BLOCK_VOLUME). Without an explicit ontology, every persona-builder hardcodes its own understanding, drift accumulates, and cross-resource queries (blast radius, dependency walk) require ad-hoc joins.

## Decision

### Resource graph is part of `shim_faaas`
The resource ontology has two surfaces:
1. **Source-of-truth in `framework/config/shim_faaas.yaml`** (committed; Architect-owned). Human-readable, diffable, PR-reviewed.
2. **Mirror in `kb_fa_semantic` schema** (Oracle Property Graph). Queryable at runtime via `graph_traverse` MCP tool.

### Schema in shim_faaas.yaml
```yaml
resources:
  - id: pod
    display_name: "POD"
    description: "Customer instance unit; isolation boundary for tenants."
    contains: [poddb]
    used_by_services: [adp, facp, hcp]
    typical_functional_areas: [refresh, provisioning, patching, dr]

  - id: poddb
    display_name: "POD Database"
    description: "Per-customer FA database within a POD."
    contained_in: [pod]
    runs_on: [exadata]
    typical_functional_areas: [refresh, db_infra_patching, dr]

  - id: exadata
    display_name: "Exadata"
    description: "Engineered DB platform hosting many PODDBs."
    hosts: [poddb]
    uses: [block_volume, network]
    typical_functional_areas: [db_infra_patching, dr]

  - id: block_volume
    display_name: "Block Volume"
    description: "Persistent block storage."
    used_by: [exadata]

  - id: network
    display_name: "Network"
    description: "Networking layer (VCN, subnets, ACLs)."
    used_by: [exadata, pod]
```

### Edge types
| Edge | Semantics |
|---|---|
| `contains` / `contained_in` | Hierarchical containment (POD contains PODDBs) |
| `runs_on` / `hosts` | Compute-on-platform (PODDB runs on EXADATA) |
| `uses` / `used_by` | Dependency / consumes |
| `peers_with` | Symmetric peer (e.g., redundant block volumes in a pair) |
| `replaces` (with timestamp) | Lifecycle (new resource type replacing legacy) |

Edges are first-class with their own metadata (e.g., cardinality: one POD contains 1..N PODDBs).

### Loaded into the property graph
At deploy time (or shim refresh), `shim_faaas.yaml` is rendered into Property Graph DDL:
```sql
INSERT INTO kb_fa_semantic.nodes (urn, kind, properties)
  VALUES ('urn:faaas:resource:pod', 'resource', JSON_OBJECT('display_name': 'POD', ...));

INSERT INTO kb_fa_semantic.edges (src_urn, dst_urn, rel, properties)
  VALUES ('urn:faaas:resource:pod', 'urn:faaas:resource:poddb', 'contains', JSON_OBJECT(...));
```

Every ContentItem that references a resource adds a `references` edge from its URN to the resource URN. This makes blast-radius queries possible:
```
graph_traverse(start=urn:faaas:resource:exadata, edge_types=[hosts, references], depth=2)
  → returns: every PODDB on this EXADATA, every ContentItem referencing those PODDBs
```

### URN scheme
`urn:faaas:{kind}:{id}` — examples:
- `urn:faaas:resource:pod`
- `urn:faaas:service:adp`
- `urn:faaas:functional_area:refresh`
- `urn:faaas:persona:ops_eng`
- `urn:faaas:content:incidents:INC-12345#chunk_0`

### Consumer pattern: vector → graph hop
For "find resources related to my query, then walk their dependencies":
1. `vector_search(corpus="resource_summaries", query="storage outage")` → top-K resource URNs
2. `graph_traverse(start=<top URN>, edge_types=[uses, used_by, hosts], depth=2)` → expanded set
3. Return both retrievals as one ContextPacket.

This is spec §4.5's pattern, applied to FAaaS resources directly.

### Maintenance
- `shim_faaas.yaml` is human-edited (Architect PRs).
- A weekly job diffs `shim_faaas.yaml` against the property graph; resyncs on drift.
- Ingestion adapters that produce ContentItems reference resources by `id` (e.g., `pod`, `poddb`); the framework expands to `urn:faaas:resource:pod` at upsert time.
- New resource types require an Architect PR + `shim_faaas.yaml` update + property graph reseed.

### Bootstrap from API spec
Phase 2 includes a `kb-cli ingest-resource-ontology --from-api-spec <path>` task that:
1. Reads the FAaaS API spec (OpenAPI / proprietary)
2. Extracts entity types and relationships
3. Proposes a `shim_faaas.yaml` resources block diff
4. Architect reviews, merges, reseeds

## Considered alternatives
- **Resources as flat enum (no ontology)** — rejected; loses parent/child queries, breaks blast-radius.
- **Resource graph in a separate store** — rejected; spec §4.5 already calls for a property graph; reusing one is cheaper.
- **Only edges, no nodes** — rejected; nodes carry summaries that the vector → graph hop relies on.
- **Auto-derive ontology from ingested ContentItems** — rejected; violates spec §2.3 (deterministic rules over LLM autonomous extraction).

## Consequences
- `shim_faaas.yaml` becomes a load-bearing artifact. Treat as production config; PR-reviewed.
- Resource references in ContentItems are constrained to the enum at ingest time (warn-on-drift; harden in Phase 4).
- Phase 2 builds the bootstrap-from-API-spec tool; Phase 4 hardens fail-on-drift.
- `graph_traverse` MCP tool becomes useful immediately (Phase 1 even, for incident → service → tenant edges). Resource hierarchy enriches it in Phase 2+.

## References
- [PDD §14](../pdd/PDD-Knowledge-Builder-Framework.md)
- [ADR-002 §kb_fa_semantic](ADR-002-storage-shape.md)
- [ADR-006 — shim_faaas](ADR-006-two-shim-architecture.md)
- [ADR-008 — Functional-area + resources](ADR-008-functional-area-and-resources.md)
- Spec §4.5
