---
title: Data Model
source: spec §6.1 + ADR-002 + ADR-003 + ADR-008
compiled_at: 2026-05-05T00:00:00Z
created: 2026-05-05
owner: architect
tags: [data-model, framework]
status: current
related: [ADR-002, ADR-003, ADR-008, ADR-009]
---

# Data Model

The framework's content model in three pieces: `ContentItem`, `Chunk`, `Edge`. Multi-axis dimensions (per ADR-008) are first-class typed fields on `ContentItem`. URN scheme follows ADR-009.

## ContentItem

```python
@dataclass
class ContentItem:
    # Identity
    id: str                       # sha256(source : source_id : schema_version)
    source: str                   # "confluence" | "jira" | "git_wiki" | "udap" | "code"
    source_id: str                # vendor-canonical id (page id, issue key)
    path: str                     # human-readable canonical path
    title: str
    body: str                     # raw or summarized; bodies for wikis live in git, body field is the summary

    # Multi-axis dimensions (ADR-008) — typed, indexed
    persona: str                  # single (e.g. "ops_eng")
    primary_axis_kind: str        # "functional_area" | "service_id" | "feature_or_release" | "program"
    primary_axis_value: str       # e.g. "refresh" or "adp" or "25.01"
    functional_area_all: list[str]  # multi-valued for cross-cutting (often len==1)
    resources: list[str]          # urns or ids (e.g. ["pod","poddb"])
    services: list[str]           # urns or ids (e.g. ["adp"])
    kind: str                     # single (e.g. "design", "runbook", "incident_history")

    # Cross-cutting metadata (spec §10) — required
    metadata: ContentMetadata

    # Children
    chunks: list[Chunk]
    edges: list[Edge]
```

```python
@dataclass
class ContentMetadata:
    # ACL placeholder (Phase 4 enforces)
    persona_visibility: list[str]
    owner: str
    classification: str           # "public" | "internal" | "restricted"

    # Versioning
    source_sha: str               # content hash of raw source at ingest time
    parser_version: str           # framework parser version
    schema_version: int           # extraction schema version

    # Time
    created_at: datetime
    updated_at: datetime
    last_reviewed: datetime | None

    # Provenance
    extracted_by: str             # adapter mode that produced this (e.g. "confluence:native")
    extraction_schema: str        # path to JSON-Schema used
    metadata_drift: bool          # True if extracted values weren't in shim_faaas vocab

    # Open extension
    extra: dict
```

### Invariants
1. `id` is deterministic from `(source, source_id, schema_version)` — re-running ingestion is a no-op.
2. `metadata.persona_visibility`, `owner`, `classification`, `source_sha`, `parser_version`, `schema_version` are all REQUIRED. Missing → `MissingMetadataError` at upsert.
3. `persona` is single-valued; cross-persona overlap manifests as multiple `ContentItem`s (per ADR-004).
4. `functional_area_all` MAY be empty (for personas where FA doesn't apply, e.g., PM).
5. `resources` values MUST be in `shim_faaas.resources[].id`. Out-of-vocab → warn + `metadata_drift: true` (v1); fail (v4).
6. Every `Chunk` belongs to exactly one `ContentItem`.

## Chunk

```python
@dataclass
class Chunk:
    id: str                       # f"{content_id}#chunk_{ord}"
    content_id: str
    ord: int                      # 0-based position
    text: str
    heading_path: list[str]       # ["Section A", "Subsection 1"]
    embedding: list[float] | None # 3072 dims (text-embedding-3-large)
    metadata: dict                # inherits ContentItem.metadata + chunk-specific (page_no, span)
```

### Notes
- Embeddings are computed lazily for KBs with `kind: vector`. For wiki KBs, embeddings are optional (built only if hybrid retrieval is configured).
- `heading_path` lets retrievers rerank by structural depth (a hit deep in a section is sometimes better than a top-level mention).

## Edge

```python
@dataclass
class Edge:
    src: str                      # URN
    dst: str                      # URN
    rel: str                      # "owns" | "depends_on" | "references" | "resolves" |
                                  # "contains" | "runs_on" | "uses" | ...
    metadata: dict                # weight, timestamp, source-of-edge, etc.
```

Edges are written into:
- `kb_<persona>_kb_*` schemas (incident → service → owner → tenant for Aira's KB)
- `kb_fa_semantic` (resource ontology + cross-references from ContentItems)

## URN scheme (per ADR-009)

```
urn:faaas:{kind}:{id}                                # canonical entity reference
```

Examples:
| URN | Meaning |
|---|---|
| `urn:faaas:resource:pod` | the POD resource type |
| `urn:faaas:service:adp` | the ADP service |
| `urn:faaas:functional_area:refresh` | the REFRESH workstream |
| `urn:faaas:persona:ops_eng` | the Ops Engineer persona |
| `urn:faaas:content:incidents:INC-12345` | a specific ContentItem |
| `urn:faaas:content:incidents:INC-12345#chunk_3` | a chunk within it |
| `urn:faaas:tenant:tenant-99` | a tenant (data plane reference) |

The framework's `core/urns.py` module owns parsing/rendering URNs.

## Storage mapping (per ADR-002)

| Class | Where |
|---|---|
| `ContentItem` (top-level fields) | `<schema>.content_items` row |
| `ContentItem.body` (for wiki KBs) | git repo (`kb-wiki/...`); DB stores SHA + path |
| `ContentItem.metadata` | `<schema>.content_items.metadata_json` (Oracle 23ai JSON) |
| `Chunk` | `<schema>.chunks` row + `embeddings` VECTOR(3072) |
| `Edge` | `<schema>.edges` row, also rendered into `kb_fa_semantic` PG |

## Multi-axis indexes (per ADR-008)

On `<schema>.content_items`:
- B-tree: `persona`, `primary_axis_kind`, `primary_axis_value`, `kind`
- JSON path indexes: `functional_area_all`, `resources`, `services`
- Composite: `(persona, primary_axis_value, kind)` for the most common filter pattern

On `<schema>.chunks`:
- HNSW on `embedding` (cosine) where applicable
- Composite: `(content_id, ord)` for ordered fetch
- JSON path indexes inheriting common filter fields

## Validation pipeline

```
RawItem
  ↓ Parser.parse()
ContentItem (in-memory)
  ↓ ContentItem.validate()                 # checks metadata, dimension vocab, body length
  ↓ may raise: MissingMetadataError, VocabDriftWarning
ContentItem (validated)
  ↓ Store.upsert()                          # idempotency, versioning checks
  ↓ may short-circuit on source_sha match
Persisted
```

`validate()` is in `core/content.py`. It's called from `Store.upsert()` first thing; bypass = bug.

## Versioning rules (operational)

| Change | Action |
|---|---|
| Add a new optional field to extraction schema | Schema patch; no re-ingest required (v1 rule) |
| Rename a required field | New schema version `v{N+1}.json`; persona-builder bumps `schema_version`; reingest impacted KB |
| Change embedding model | Reindex all `chunks.embedding`; bump `schema_version` of every vector KB; pin model name in `kb_shim.sources` |
| Add a new resource to `shim_faaas` | Architect PR; reseed `kb_fa_semantic` resource nodes |
| Add a new persona | New `persona_builders/{persona}.yaml`; new schemas under `parsers/schemas/{persona}/`; new gold set |

Old rows are tagged `superseded` rather than deleted, so eval can compare across versions.

## Reading the schemas

- Spec for fields: `parsers/schemas/{persona}/{kb_name}/v{N}.json` (one per LLM-driven KB)
- Examples in [persona-knowledge-builder.md](persona-knowledge-builder.md) and the per-persona configs under `framework/persona_builders/*.yaml`.
- Core types in code: `framework/core/content.py`.
