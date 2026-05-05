---
title: ADR-002 — Storage shape per data type
status: accepted
created: 2026-05-04
owner: architect
tags: [adr, storage, phase-0]
related: [ADR-001, DECISION-002]
---

# ADR-002 — Storage shape per data type

## Status
Accepted (2026-05-04). Source decision: [DECISION-002](../../../pmo/decisions/DECISION-002-converged-vs-polyglot.md).

## Context
Spec §2.1 requires polyglot access patterns even though DECISION-001 collapses physical deployment into one Autonomous DB. Spec §4 enumerates six data types with distinct access patterns. This ADR defines the schema layout per data type and the mapping from logical `Store` instances (§6.3) to physical schemas.

## Decision

### Logical-to-physical mapping
One Autonomous Database instance, one schema per data type. The §6.3 `Store` contract treats each schema as a distinct logical store.

| Logical Store | Schema | Primary tables |
|---|---|---|
| `IncidentVectorStore` | `kb_incidents` | `content_items`, `chunks`, `embeddings` (VECTOR(3072)), `edges` |
| `FleetReadThroughStore` | (no own schema — wraps existing UDAP/Sentinel) | views allowlisted in `query_fleet` |
| `CodeStructuralStore` | `kb_code` | `code_pages` (markdown bodies + path index), `symbols` (name + AST refs) |
| `WikiMetadataStore` | `kb_wiki_meta` | `pages` (path + frontmatter JSON + git_sha), `links` |
| `FaSemanticGraphStore` | `kb_fa_semantic` | property graph: nodes (FA objects), edges (relationships), JSON columns for rule constraints |
| `ShimIndex` | `kb_shim` | `sources` (one row per registered store), persona-visibility lookup |

Wiki *content* (markdown bodies for PM/TPM/code wikis) lives in **git**, not the DB. The DB stores frontmatter + path + git SHA so we can serve a page by `read_wiki_page(path)` without a git checkout in the request path (see ADR-001 — wiki serving).

### Per-data-type schema details

#### `kb_incidents` — vector with metadata + edges (spec §4.1)
- `content_items(id PK, source_id, source, path, title, body, metadata JSON, persona_visibility JSON, classification, owner, source_sha, parser_version, schema_version, created_at, updated_at)`
- `chunks(id PK, content_id FK, ord, text, heading_path JSON, metadata JSON, embedding VECTOR(3072))`
  - HNSW index on `embedding` (cosine distance).
  - B-tree on `(content_id, ord)`.
  - JSON path index on common metadata filters: `service`, `tenant`, `owner`, `severity`, `tags`.
- `edges(src URN, dst URN, rel, metadata JSON, PK(src,dst,rel))`
  - For incident → service → owner → tenant graphs (also queryable as a property graph view).

#### `kb_fleet_views` (read-through; spec §4.2)
- No new tables. The `query_fleet` MCP tool wraps a curated allowlist of UDAP/Sentinel views. Allowlist is configured in `framework/retrievers/fleet_views.yaml` and version-controlled.
- `text_to_sql` is constrained to the same allowlist (cannot reference base tables).

#### `kb_code` — structural index, not embeddings (spec §4.3)
- `code_pages(path PK, repo, body MD, git_sha, generated_at, summary)`
- `symbols(id PK, name, kind, path FK, line_start, line_end, json_signature)`
- `symbol_refs(src_symbol FK, dst_symbol FK, ref_kind)` — call graph if the language adapter produces one
- No vector embeddings on code per spec §4.3 ("structure-indexed, not vectorized").

#### `kb_wiki_meta` — metadata only; bodies in git (spec §4.4)
- `pages(path PK, repo, git_sha, frontmatter JSON, last_compiled_at, parser_version, schema_version)`
- `links(src_path FK, dst_path, rel)` — supports the §8.1 "graph-of-wikis" recall option later.
- A FastAPI cache-aside layer reads bodies from git on miss; OCI Object Storage holds rendered HTML cache.

#### `kb_fa_semantic` — property graph (spec §4.5)
- Property graph created via Oracle 23ai PG DDL. Nodes = FA objects; edges typed by relationship; JSON properties carry rule predicates.
- Vector embeddings on a `node_summaries` table allow "find starting node by NL" (spec §4.5: vector → graph hop).

#### `kb_shim` — shim index (spec §6.6)
- `sources(name PK, kind, summary, persona_visibility JSON, retrieval_tools JSON, root_path, last_health_check)`
- Loaded into the Context Builder prompt via `list_sources()` tool.

### Cross-cutting columns (all `content_items` analogues carry these)
| Column | Required for | Spec ref |
|---|---|---|
| `metadata.persona_visibility` (JSON array) | ACL placeholder | §10 |
| `metadata.owner` | ACL + observability | §10 |
| `metadata.classification` | ACL placeholder | §10 |
| `source_sha` | idempotency | §10 |
| `parser_version` | re-ingest decision | §10 |
| `schema_version` | re-ingest decision | §10 |

### Idempotency rule
ContentItem `id` = `sha256(source || ':' || source_id || ':' || schema_version)`. Re-running ingestion with no source change is a no-op (`upsert` finds the same id and short-circuits when `source_sha` matches).

### Migration / re-index policy
- Bumping `schema_version` (e.g., new fields in extraction schema) → reingest impacted corpora; old rows tagged superseded but kept for diff/eval comparison.
- Bumping embedding model → reindex all `chunks.embedding`; pinned model name + dim live in `kb_shim.sources`.

## Considered alternatives
- **Separate Autonomous DB instances per data type**: stronger isolation but multiplies operational cost. Rejected per DECISION-002.
- **Wiki bodies in DB instead of git**: better single-store atomicity but loses git's diff/PR/blame ergonomics. Rejected.
- **Embed code in vector store**: cheap to retrieve but, per spec §4.3, structure-indexed lookup outperforms vector for code navigation. Rejected.

## Consequences
- The §6.3 `Store` interface stays as designed; concrete implementations differ by schema, not by host.
- Cost telemetry must report per-schema bytes, queries, and embeddings cost so we can data-drive a future split (DECISION-002 revisit conditions).
- Backups are unified; restore granularity is per-schema (Oracle Flashback handles this).

## References
- [DECISION-001](../../../pmo/decisions/DECISION-001-oracle-tech-stack.md)
- [DECISION-002](../../../pmo/decisions/DECISION-002-converged-vs-polyglot.md)
- Spec §4 (data type catalog), §6.3 (Store contract), §10 (cross-cutting).
