---
title: ADR-003 — Core interfaces (ContentItem, Parser, Store, Retriever, Context Builder)
status: accepted
created: 2026-05-04
owner: architect
tags: [adr, interfaces, phase-0]
related: [ADR-001, ADR-002]
---

# ADR-003 — Core interfaces

## Status
Accepted (2026-05-04). Codifies spec §6.1–§6.6 with Oracle/OpenAI specifics from ADR-001/ADR-002.

## Context
Spec §6 specifies content model, parser contract, store contract, MCP retrieval surface, Context Builder, and shim index. This ADR locks the interface contract so Phase 1 implementation is unambiguous.

## Decision

### Module map (spec §5, finalized)
```
framework/
├── core/
│   ├── interfaces.py          # ABCs / Protocols below
│   ├── content.py             # ContentItem, Chunk, Edge dataclasses
│   ├── llm.py                 # LLMClient shim (OpenAI now; swappable)
│   └── events.py              # ingestion events, change detection
├── adapters/                  # one per source: confluence, jira, code, fleet, git
├── parsers/
│   ├── llm_parser.py          # OpenAI-backed; prompt + schema injection
│   ├── rule_parser.py         # deterministic field-mapping
│   ├── code_wiki_parser.py    # Som-style code structure builder
│   └── schemas/               # versioned extraction schemas (per data type / per persona)
├── stores/                    # IncidentVectorStore, FleetReadThroughStore,
│   │                          # CodeStructuralStore, WikiMetadataStore,
│   │                          # FaSemanticGraphStore, ShimIndex
├── retrievers/                # MCP tool implementations
├── orchestrator/              # Context Builder (LangGraph) + synthesizer
├── ingestion/                 # pipeline, change_detection, scheduler
├── eval/                      # gold sets, runners, reports
├── persona_builders/          # one YAML per persona (PM, TPM, Aira)
└── deploy/                    # MCP server entrypoint, OCI deploy descriptors
```

### `core/content.py` — content model (spec §6.1)
```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass(frozen=True)
class Edge:
    src: str          # entity URN, e.g. urn:fa:incident:INC-123
    dst: str
    rel: str          # owns | depends_on | references | resolves | impacts
    metadata: dict = field(default_factory=dict)

@dataclass
class Chunk:
    id: str           # f"{content_id}#chunk_{ord}"
    text: str
    ord: int
    heading_path: list[str]
    embedding: Optional[list[float]] = None  # 3072 dims (text-embedding-3-large)
    metadata: dict = field(default_factory=dict)  # inherits + chunk-specific (page_no, span)

@dataclass
class ContentItem:
    id: str           # sha256(source || ':' || source_id || ':' || schema_version)
    source: str       # "confluence" | "jira" | "code" | "fleet" | "git_wiki" | ...
    source_id: str
    path: str         # human-readable canonical path
    title: str
    body: str         # raw or summarized depending on parser kind
    metadata: dict    # MUST include: owner, persona_visibility (list[str]), classification,
                      # last_reviewed, links, source_sha, parser_version, schema_version,
                      # timestamps
    chunks: list[Chunk] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
```
**Invariants** (enforced by `core/content.py:validate()`):
- `metadata` MUST contain `persona_visibility`, `owner`, `classification`, `source_sha`, `parser_version`, `schema_version`. Reject with `MissingMetadataError` otherwise (spec §10 mandate).
- `id` MUST be deterministic from `(source, source_id, schema_version)` for idempotency.

### `core/interfaces.py` — Protocols (spec §6.2 / §6.3)
```python
from typing import Protocol, runtime_checkable
from .content import ContentItem

@dataclass
class RawItem:
    kind: str         # "confluence_page" | "jira_issue" | "git_file" | ...
    source: str
    source_id: str
    payload: dict     # raw API response or parsed file content
    metadata: dict    # raw source metadata (created_at, author, etc.)

@dataclass
class ParseContext:
    schema_id: str    # e.g. "incidents/v1" or "pm/v0"
    parser_version: str
    persona: str | None
    extra: dict = field(default_factory=dict)

@runtime_checkable
class Parser(Protocol):
    name: str
    input_kinds: set[str]
    def parse(self, raw: RawItem, ctx: ParseContext) -> ContentItem: ...

@dataclass
class Query:
    kind: str         # store-specific tag e.g. "vector_knn", "graph_traverse"
    payload: dict     # store-specific query body
    persona: str | None = None
    limit: int = 10

@dataclass
class Result:
    content_id: str
    chunk_id: str | None
    text: str
    score: float
    citation_url: str
    metadata: dict

@runtime_checkable
class Store(Protocol):
    kind: str         # "vector" | "sql" | "graph" | "wiki" | "code" | "shim"
    schema_name: str  # Autonomous DB schema, per ADR-002
    def upsert(self, items: list[ContentItem]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def query(self, q: Query) -> list[Result]: ...

@runtime_checkable
class Retriever(Protocol):
    name: str         # e.g. "vector_search"
    def __call__(self, **kwargs) -> list[Result]: ...
```

### `parsers/schemas/*.json` — extraction schemas
Each schema is a JSON-Schema document with required fields the parser MUST emit. Persona teams own their own schemas under their persona-builder config (see ADR-004); framework ships starter schemas only for **incidents/v1** (spec §4.1) and a `_template.json` for persona authors.

`parsers/schemas/incidents/v1.json` (sketch):
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["root_cause_summary", "impact", "resources_affected"],
  "properties": {
    "root_cause_summary": {"type": "string", "maxLength": 500},
    "impact": {"type": "object", "required": ["blast_radius", "duration_minutes"]},
    "resources_affected": {"type": "array", "items": {"type": "string"}},
    "ora_codes": {"type": "array", "items": {"type": "string"}},
    "tenant_ids": {"type": "array", "items": {"type": "string"}},
    "service_owner": {"type": "string"},
    "resolution_summary": {"type": "string"}
  }
}
```

### `retrievers/` — MCP tool surface (spec §6.4)
Standard v1 tools, with concrete tool names and signatures:

| Tool | Inputs | Output |
|---|---|---|
| `search_wiki(query, persona?, max_results?)` | string + optional filters | `[{path, title, snippet, score, citation_url}]` |
| `read_wiki_page(path)` | string | `{path, body, frontmatter, citation_url}` |
| `vector_search(corpus, query, filters?, k?)` | corpus name + query | `[{content_id, chunk_id, text, score, citation_url}]` |
| `query_fleet(view, filters, projection)` | allowlisted view + filter | rows + `citation_url` (link to source) |
| `text_to_sql(nl_query, view_allowlist?)` | NL string | `{generated_sql, rows, citation_url}` |
| `graph_traverse(start_entity, edge_types, depth)` | entity URN | `[{path: [URN...], metadata}]` |
| `read_code_page(path)` | repo path | `{path, body, citation_url}` |
| `find_symbol(name, kind?)` | symbol | `[{path, line, kind, citation_url}]` |
| `get_incident_summary(incident_id)` | id | structured summary + `citation_url` |
| `list_sources()` | — | shim index entries |

**Universal output rule**: every tool returns `citation_url`. No citation = bug (spec §10).

### `orchestrator/context_builder.py` — Context Builder (spec §6.5)
LangGraph state machine:
```
ingest_query → load_shim_index (cached) → classify_intent → select_tools → call_tools (parallel)
            → dedupe_and_rerank → assemble_context_packet → synthesize → return ContextPacket
```
- **Intent classification**: small LLM call returning `{tool_names: [...], confidence}`. Fallback to a default toolset if confidence < threshold.
- **Budgets**: per-query budget on `(tokens, latency, tool_calls)`. Enforced before each step.
- **ContextPacket**: `{passages: [...], citations: [...], used_tools: [...], cost: {...}}`. Synthesizer's prompt is templated to require inline citations.

### `orchestrator/shim_index.py` — shim index (spec §6.6)
- Loads `kb_shim.sources` once per process; refreshes on TTL or webhook.
- Renders into a compact YAML chunk that fits the synthesizer's system prompt.

## Considered alternatives
- **Pydantic v2** instead of dataclasses for the content model — chose dataclasses + a `validate()` helper to keep core lightweight; Pydantic models live one layer up at the API/parser boundary.
- **Inheritance-based Store hierarchy** — chose Protocol so the converged-DB schemas can be plugged in without a base class explosion.
- **Dict-of-dicts for ContextPacket** — chose explicit dataclasses for type safety; serialization is via `dataclasses.asdict`.

## Consequences
- All ingestion code paths flow through `Parser.parse() → Store.upsert()`; bypass = lint failure (spec §2.5).
- Persona teams plug in *only* via `parsers/schemas/{persona}/{version}.json` and a YAML in `persona_builders/`. They never touch `core/`.
- Adding a new data type = new schema in `kb_*` + a new `Store` impl + register in `kb_shim.sources`. The Context Builder picks it up automatically via `list_sources()`.

## Compliance with spec
- §6.1 content model: implemented as above with explicit invariants.
- §6.2 Parser: Protocol; LLM and rule variants both conform.
- §6.3 Store: Protocol with five concrete impls per ADR-002.
- §6.4 retrieval surface: 10 tools enumerated; uniform `citation_url` rule.
- §6.5 Context Builder: LangGraph implementation per ADR-001.
- §6.6 shim index: `kb_shim.sources` table.
- §10 cross-cutting: invariants enforce metadata; `Store.upsert()` MUST be idempotent on `source_sha` match.
