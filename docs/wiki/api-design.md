---
title: API Design — MCP Retrieval Tool Surface
source: spec §6.4 + ADR-003
compiled_at: 2026-05-05T00:00:00Z
created: 2026-05-05
owner: architect
tags: [api, mcp, framework]
status: current
related: [ADR-003, ADR-006, ADR-007, ADR-008]
---

# API Design — MCP Retrieval Tool Surface

The framework exposes its retrieval surface as **MCP tools** served by a FastAPI app on OCI Compute. Every consumer (Aira, internal portals, future agents) speaks MCP. The Orchestrator and persona context skills also call these tools internally over the same surface.

## Universal output rule

**Every tool returns at least one citation per result.** No citation = bug. The `Result` shape is shared:

```jsonc
{
  "content_id": "string",         // ContentItem.id
  "chunk_id": "string|null",      // optional; absent for whole-page hits
  "text": "string",               // the passage to use as evidence
  "score": 0.0,                   // retrieval score (vector dist, BM25, etc.)
  "citation_url": "string",       // ALWAYS present
  "metadata": {                   // dimension fields per ADR-008 (where applicable)
    "persona": "...",
    "kind": "...",
    "functional_area": ["..."],
    "resources": ["..."],
    "services": ["..."],
    "source_sha": "...",
    "schema_version": 1
  }
}
```

## Tool catalog (v1)

### `search_wiki(query, persona?, max_results?, filters?)`
Hybrid search over wiki KBs. Native Postgres-FTS-equivalent via Oracle Text + optional vector fallback.
**Input**:
```jsonc
{
  "query": "string",
  "persona": "string|null",
  "max_results": 10,
  "filters": {
    "functional_area": ["refresh"],
    "resources": ["pod"],
    "kind": "design",
    "time_window": ["2026-01-01","2026-05-01"]
  }
}
```
**Output**: `{ "results": [Result] }`

### `read_wiki_page(path)`
Fetch a full wiki page by canonical path. Returns body from git (cached).
**Input**: `{ "path": "kb-wiki/eng-mgr/refresh/pod-procedure.md" }`
**Output**: `{ "path", "body", "frontmatter", "citation_url" }`

### `vector_search(corpus, query, k?, filters?)`
Semantic recall over a named vector corpus. `corpus` matches a `knowledge_bases[].name` from a persona-builder config.
**Input**:
```jsonc
{ "corpus": "ops_incidents", "query": "...", "k": 10, "filters": { ... } }
```
**Output**: `{ "results": [Result] }`

### `query_fleet(view, filters, projection?)`
Typed read against an allowlisted UDAP/Sentinel view.
**Input**: `{ "view": "pod_health", "filters": { "tenant": "tenant-99" }, "projection": ["pod_id","status"] }`
**Output**: `{ "rows": [{...}], "row_count": N, "citation_url": "...", "view": "pod_health" }`

### `text_to_sql(nl_query, view_allowlist?)`
NL → SQL constrained to allowlisted views. Hard-rejects DDL/DML; rejects table refs outside the allowlist; row + runtime caps.
**Input**: `{ "nl_query": "how many PODs in EMEA on 25.01" }`
**Output**: `{ "generated_sql": "...", "rows": [...], "row_count": N, "citation_url": "..." }`

### `graph_traverse(start_entity, edge_types, depth?)`
Walk the property graph. `start_entity` is a URN.
**Input**: `{ "start_entity": "urn:faaas:resource:exadata", "edge_types": ["hosts","references"], "depth": 2 }`
**Output**:
```jsonc
{ "paths": [
    { "nodes": ["urn:faaas:resource:exadata", "urn:faaas:resource:poddb"],
      "edges": ["hosts"],
      "metadata": { "rel_count": 12 } },
    ...
  ]
}
```

### `read_code_page(path)`
Fetch a Som-style code wiki page (markdown + symbol summary).
**Input**: `{ "path": "kb-code/services/auth/overview.md" }`
**Output**: `{ "path", "body", "citation_url" }`

### `find_symbol(name, kind?, repo?)`
Code symbol lookup against `kb_code.symbols`.
**Input**: `{ "name": "verify_token", "kind": "function" }`
**Output**: `{ "results": [{ "path", "line", "kind", "signature", "citation_url" }] }`

### `get_incident_summary(incident_id)`
Direct lookup for a structured incident summary.
**Input**: `{ "incident_id": "INC-12345" }`
**Output**: a structured incident card (root_cause_summary, impact, resources_affected, …) per the incident schema, with citations.

### `list_sources(persona?)`
Returns the shim — used by the Orchestrator and persona skills (and discoverable by external consumers).
**Input**: `{ "persona": "ops_eng" }` (optional filter)
**Output**:
```jsonc
{
  "shim_faaas": { "personas": [...], "services": [...], "resources": [...], "functional_areas": [...] },
  "knowledge_bases": [
    { "name": "ops_incidents", "persona": "ops_eng", "kind": "vector",
      "kb_card": { "summary": "...", "use_when": "...", "input_shape": "...", "output_shape": "..." },
      "retrieval_tools": ["vector_search","get_incident_summary"] },
    ...
  ]
}
```

## Authentication

- **Bearer token** at the MCP endpoint, validated against an OCI Vault-stored token list.
- Per-consumer tokens identify the calling agent (Aira, portal, etc.).
- Token → `consumer_manifest.yaml` lookup → which `knowledge_bases_required` they can see → ACL filter on tool results (Phase 4 enforcement; v1 carries the metadata).

## Rate limits

- Per-token RPM cap (configurable in `consumer_manifest.yaml`)
- Per-tool token-cost cap per minute (e.g., `vector_search` is fast; `text_to_sql` is expensive)
- 429 with `Retry-After` on cap breach

## Versioning

- All tools v1 in v1. Future breaking changes ship `vector_search_v2` etc. (additive, deprecation timeline ≥ 2 phases).

## Errors

Standard MCP error envelope:
```jsonc
{ "error": { "code": "...", "message": "...", "details": { ... } } }
```
Common codes: `invalid_argument`, `not_found`, `permission_denied`, `rate_limited`, `upstream_unavailable`, `budget_exceeded`.

## Local-call vs MCP-call

The Orchestrator and persona skills can call retrievers in-process (saves serialization). The same code paths handle both — the tool dispatcher exports both `local_call(name, args)` and the MCP HTTP endpoint, sharing implementation.

## Health

`GET /healthz` — composite check: ADB connection, OpenAI ping, OCI Vault, every adapter's healthcheck.

## OpenAPI

`framework/deploy/openapi.yaml` is auto-generated from the FastAPI app — single source of truth for HTTP API consumers and external tooling.
