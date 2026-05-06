---
title: ADR-008 — Functional-area & resource dimensions
status: accepted
created: 2026-05-05
owner: architect
tags: [adr, ontology, metadata, phase-1]
related: [ADR-002, ADR-006, ADR-009, PDD]
---

# ADR-008 — Functional-area & resource dimensions

## Status
Accepted (2026-05-05). Formalizes the multi-axis content organization from PDD §6.

## Context
A persona's content lives at the intersection of multiple independent dimensions. Forcing a single hierarchy (e.g., persona → functional_area → resource → kind) creates brittle folders and forces duplication of cross-cutting content. This ADR locks the dimension contract.

## Decision

### Four orthogonal dimensions on every ContentItem
| Dimension | Type | Cardinality | Source |
|---|---|---|---|
| `persona` | enum (single) | 1 | derived from producing builder; never multi |
| `functional_area` | enum (multi-valued) | 0..N | `shim_faaas.functional_areas` |
| `resources` | enum (multi-valued) | 0..N | `shim_faaas.resources` |
| `kind` | enum (single) | 1 | `shim_faaas.kinds_of_knowledge` |

Plus **services** (multi-valued) and **time fields** (created/updated/last_reviewed) which are universal but not dimension-axes for routing.

### Functional area is NOT a persona
A functional area (REFRESH, PROVISIONING, PATCHING, DR, DB_INFRA_PATCHING, UPDATE) is a workstream that crosses personas. A "REFRESH" question typically activates Eng Mgr + Ops Eng + Service Owner skills in parallel. If we made REFRESH a persona, every cross-persona retrieval would be ambiguous. Verdict: dimension, not persona.

### Resources are faceted, NOT folder-primary
Pages reference 0..N resources. Forcing a `pod/`, `poddb/`, `exadata/` folder hierarchy duplicates content for any page that touches >1 resource. Verdict: store flat; multi-valued tag; auto-build resource cards as derived view.

### Primary axis per persona — the folder layout choice
Each persona declares a **primary_axis** in its config (per ADR-004). The other dimensions become tags.

| Persona | primary_axis | Justification |
|---|---|---|
| Eng Manager | functional_area | Eng mgrs reason in workstreams ("REFRESH status?") |
| Ops Manager | functional_area | Same |
| Ops Engineer | functional_area | Same |
| Service Owner | service_id | Service owners reason in services ("ADP ownership?") |
| Architect | functional_area | Cross-cutting designs land in workstream folders |
| Developer | service_id | Devs operate per-service / per-repo |
| PM | feature_or_release | PMs reason in features and releases |
| TPM | program | TPMs track programs/initiatives |

### Storage layout (per ADR-002 schemas)
Indexed columns on `content_items` (and equivalent on `chunks` where useful):
- `persona` (single)
- `functional_area_primary` (single — the "lives in this folder" axis when applicable)
- `functional_area_all` (JSON array — multi-valued for cross-cutting)
- `resources` (JSON array)
- `services` (JSON array)
- `kind` (single)
- `service_id_primary` (single — when persona's primary_axis is service_id)
- `feature_or_release_primary` (single — for PM)
- `program_primary` (single — for TPM)

JSON path indexes on the array fields for fast filtering.

### Cross-cutting pages
A page about "POD refresh during PATCHING" has `functional_area_all = [refresh, patching]`. It physically lives in `kb-wiki/eng-mgr/refresh/` (its primary). Retrieval finds it under either filter. The wiki's frontmatter declares both:

```yaml
---
title: POD refresh during PATCHING
persona: eng-mgr
functional_area_primary: refresh
functional_area_all: [refresh, patching]
resources: [pod, poddb]
kind: design
---
```

### Resource cards (derived view)
A CI step scans every ContentItem's `resources` field and builds:
```
kb-resources/
├── pod/_card.md           # auto-generated; lists every page mentioning POD
├── poddb/_card.md         # cross-persona view: eng-mgr, ops-eng, architect, ...
└── exadata/_card.md
```
Cards are not canonical storage; they're queryable as wiki pages but rebuilt from canonical content on every commit. Authority lives in the persona-scoped pages.

### Retrieval filter contract
Every retrieval tool accepts (optional, all multi-valued except `kind`):
- `functional_area`
- `resources`
- `services`
- `kind`
- `time_window`

Filters are AND-combined across dimensions; OR-combined within a multi-valued dimension. Empty filter = no constraint.

### Vocabulary management
- `shim_faaas.yaml` is the controlled vocabulary for all four dimensions.
- Persona-builder configs MAY declare a subset (`functional_areas: [refresh, dr]`) but cannot introduce new values.
- Out-of-vocab values during ingestion → parser warning + content marked `metadata_drift: true`. Not a hard fail in v1; weekly drift report flags new values for Architect to add to `shim_faaas.yaml`.
- Phase 4 hardens to fail-on-drift.

### Resource ontology (referenced; detailed in ADR-009)
Resources have parent/child relationships (POD contains PODDB; PODDB runs on EXADATA). The resource graph lives in `kb_fa_semantic` (Oracle Property Graph). Retrieval can widen a resource filter through the graph (e.g., a query about POD optionally widens to its contained resources).

## Considered alternatives
- **Functional area as persona** — rejected; cross-cutting questions become orphaned.
- **Hierarchy = persona → service → functional_area** — rejected; rigid, breaks for cross-service workstreams.
- **No primary axis; flat storage everywhere** — rejected; loses browsability for human review of wiki content.
- **Single multi-valued primary axis** — rejected; primary axis informs git folder layout and at most one folder makes sense per page.

## Consequences
- ContentItem schema gains 5–6 indexed dimension columns + 2–3 array columns.
- Persona-builder config gets a required `primary_axis:` field.
- Parser implementations must extract dimension values into the right columns from the LLM output (schema-driven).
- Resource cards are a small CI job; failure to rebuild them is a warning, not a blocker.
- Cross-persona, cross-FA queries are first-class; orchestrator routes by intent, retrieval narrows by filters.

## References
- [PDD §6, §14](../pdd/PDD-Knowledge-Builder-Framework.md)
- [ADR-002 — Storage shape](ADR-002-storage-shape.md)
- [ADR-006 — Two-shim architecture](ADR-006-two-shim-architecture.md)
- [ADR-009 — Resource ontology](ADR-009-resource-ontology.md)
