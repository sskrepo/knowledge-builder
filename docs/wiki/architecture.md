---
title: Architecture
source: docs/raw/knowledge-builder-framework-spec.md (§3) + ADRs
compiled_at: 2026-05-05T00:00:00Z
created: 2026-05-05
owner: architect
tags: [architecture, framework]
status: current
related: [ADR-001, ADR-002, ADR-003, ADR-006, ADR-007, ADR-008, ADR-009, ADR-010, ADR-011, PDD]
---

# Architecture

Mirrors spec §3 with the design decisions from ADRs 001–011 baked in. The PDD is the executive narrative; this page is the engineering shape.

## Five-layer model

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ⑤  ORCHESTRATOR AGENT (LangGraph on OCI Compute)                       │
│     · loads shim_faaas at startup                                       │
│     · classifies query intent                                           │
│     · dispatches persona context skills (parallel where possible)        │
│     · merges + reranks ContextPackets                                   │
│     · synthesizes cited answer                                          │
└────────────────────────┬────────────────────────────────────────────────┘
                         │ shim_faaas (domain ontology)
┌────────────────────────▼────────────────────────────────────────────────┐
│  ④  shim_faaas — personas · services · resources · functional_areas    │
│     · static-ish; PR-reviewed in framework/config/shim_faaas.yaml       │
│     · mirror in kb_shim.faaas table                                     │
└────────────────────────┬────────────────────────────────────────────────┘
                         │ routes to skill(s)
┌────────────────────────▼────────────────────────────────────────────────┐
│  ③  PERSONA CONTEXT SKILLS  (8 skills + Aira agent)                    │
│     · PM, TPM, Architect, Eng Mgr, Developer, Ops Mgr, Ops Eng, SO     │
│     · Aira: AGENT (autonomous incident investigation)                   │
│     · skills follow ADR-007 contract                                    │
└────────────────────────┬────────────────────────────────────────────────┘
                         │ shim_kb (filtered to skill's persona)
┌────────────────────────▼────────────────────────────────────────────────┐
│  ②  shim_kb — KB cards (per ADR-004): when to query, with what shape  │
│     · auto-aggregated from persona_builders/*.yaml                      │
│     · mirror in kb_shim.kb_cards table                                  │
└────────────────────────┬────────────────────────────────────────────────┘
                         │ retrieval tool calls (MCP)
┌────────────────────────▼────────────────────────────────────────────────┐
│  ①  KNOWLEDGE BASES (polyglot per persona; physical: Oracle 23ai ADB)  │
│     ┌──────────┬───────────┬──────────┬──────────────┬──────────────┐  │
│     │  vector  │   wiki    │  graph   │  sql_pass    │  code_index   │  │
│     │  kb_*    │  kb_wiki  │  kb_fa_  │  (UDAP/      │  kb_code      │  │
│     │          │  _meta+   │  semantic│  Sentinel)   │               │  │
│     │          │  git body │          │              │               │  │
│     └──────────┴───────────┴──────────┴──────────────┴──────────────┘  │
└────────────────────────▲────────────────────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────────────────────┐
│  INGESTION PIPELINE                                                      │
│  Sources → Adapters (per ADR-011, dual-mode)                            │
│         → Parsers (LLM or rule, per persona schema)                     │
│         → Stores (Store Protocol per ADR-003)                           │
│         · idempotent (content-hash IDs)                                 │
│         · incremental (webhooks / OCI Streaming / git push)             │
│         · versioned (source_sha, parser_version, schema_version)        │
└─────────────────────────────────────────────────────────────────────────┘
                                ▲
┌───────────────────────────────┴─────────────────────────────────────────┐
│  RAW SOURCES                                                            │
│  Confluence · Jira · Git repos · UDAP/Sentinel · OCI Object Storage     │
└─────────────────────────────────────────────────────────────────────────┘
```

## Two flows

### Ingestion (write path)
```
source change → adapter.list/fetch → RawItem
              → parser.parse(RawItem, ParseContext) → ContentItem
              → store.upsert([ContentItem])
              → cost telemetry written to kb_shim.cost_log
```
- **Idempotent**: ContentItem `id = sha256(source : source_id : schema_version)`. Re-running with unchanged source = no-op.
- **Incremental**: webhooks normalize via OCI Streaming → ingestion workers (OCI Functions); fall back to scheduled polling for sources without webhooks (or in MCP mode).
- **Versioned**: `source_sha`, `parser_version`, `schema_version` on every chunk; mismatch → re-ingest needed.

### Retrieval (read path)
```
query (from consumer agent or human) → MCP server (FastAPI on OCI Compute)
                                     → Orchestrator (LangGraph)
                                       1. load shim_faaas (cached)
                                       2. classify intent → personas, FA, resources, kind
                                       3. dispatch persona context skills (parallel)
                                          each skill:
                                            - reads shim_kb_filtered
                                            - picks KBs to query
                                            - dispatches retrieval tools (vector_search,
                                              search_wiki, graph_traverse, query_fleet, ...)
                                            - returns ContextPacket
                                       4. merge + dedupe + rerank
                                       5. synthesize with citations
                                     → cited answer + per-persona attribution
```

## Module map (matches `framework/`)

```
framework/
├── core/                            # Protocols, ContentItem, Chunk, Edge, IDs, LLMClient
├── adapters/                        # one per source kind
│   ├── _base.py                     # Adapter Protocol
│   ├── confluence/{__init__,native,mcp,shared}.py    # ADR-011 dual-mode
│   ├── jira/{__init__,native,mcp,shared}.py          # ADR-011 dual-mode
│   ├── git_adapter.py
│   └── udap_adapter.py              # read-through; no ingest
├── parsers/
│   ├── llm_parser.py                # OpenAI-backed; schema-injected
│   ├── rule_parser.py               # deterministic field maps
│   ├── code_wiki_parser.py          # Som-style structural index
│   └── schemas/                     # versioned per persona/kind
│       ├── _template.json
│       ├── incidents/v1.json        # Aira (proven)
│       ├── pm/{briefs,release-plans,research}/v1.json
│       ├── tpm/{weekly-ops,ecars,dependencies}/v1.json
│       ├── architect/{designs,adrs,system-maps}/v1.json
│       ├── eng-mgr/{decisions,runbooks,known-issues}/v1.json
│       ├── developer/{openapi,decisions}/v1.json
│       ├── ops-mgr/{slas,escalation,incident-exec-summary,compliance}/v1.json
│       ├── ops-eng/{runbooks,postmortems}/v1.json
│       └── service-owner/{catalog,decisions}/v1.json
├── stores/                          # Store Protocol per ADR-003
│   ├── base.py
│   ├── incident_vector_store.py
│   ├── wiki_metadata_store.py
│   ├── code_structural_store.py
│   ├── fa_semantic_graph_store.py
│   ├── fleet_passthrough_store.py
│   └── shim_index_store.py
├── retrievers/                      # MCP tool implementations
│   ├── tools.py                     # MCP server registration
│   ├── vector_search.py
│   ├── search_wiki.py
│   ├── read_wiki_page.py
│   ├── query_fleet.py
│   ├── text_to_sql.py
│   ├── graph_traverse.py
│   ├── read_code_page.py
│   ├── find_symbol.py
│   ├── get_incident_summary.py
│   └── list_sources.py
├── orchestrator/
│   ├── context_builder.py           # LangGraph top-level
│   ├── shim_faaas.py
│   ├── shim_kb.py
│   ├── intent_classifier.py
│   ├── synthesizer.py
│   └── budget.py
├── persona_skills/                  # one file per persona, all use ADR-007 contract
│   ├── _base.py                     # BasePersonaSkill
│   ├── pm.py
│   ├── tpm.py
│   ├── architect.py
│   ├── eng_mgr.py
│   ├── developer.py
│   ├── ops_mgr.py
│   ├── ops_eng.py
│   └── service_owner.py
├── ingestion/
│   ├── pipeline.py                  # source → parser → store
│   ├── change_detection.py
│   ├── webhook_router.py
│   └── scheduler.py
├── persona_builders/                # YAML configs (Option 3 starter pack)
│   ├── _template.yaml
│   ├── pm.yaml
│   ├── tpm.yaml
│   ├── architect.yaml
│   ├── eng-mgr.yaml
│   ├── developer.yaml
│   ├── ops-mgr.yaml
│   ├── ops-eng.yaml
│   └── service-owner.yaml
├── config/                          # ADR-010
│   ├── _schema.json
│   ├── dev.yaml
│   ├── staging.yaml
│   ├── prod.yaml
│   ├── adapters/{confluence,jira,git,udap,openai}.yaml
│   └── shim_faaas.yaml
├── eval/
│   ├── runner.py
│   ├── metrics/{recall,latency,cost,faithfulness}.py
│   ├── reports/{render,diff}.py
│   └── gold_sets/                   # one JSONL per persona
├── deploy/
│   ├── mcp_server.py                # FastAPI app
│   ├── ingestion_worker.py          # OCI Functions entrypoint
│   └── ci/eval-gate.yml
├── scripts/
│   ├── bootstrap-vault.sh
│   └── check-config.py
├── cli/
│   └── kb-cli                       # validate / dry-run / eval / promote / reingest
└── tests/
```

## Cross-cutting concerns (per spec §10)

| Concern | Where it lives |
|---|---|
| Citations | `Result.citation_url` mandatory; enforced by `Store.upsert` + retriever output validation |
| Idempotency | `core/ids.py` content-hash; `Store.upsert` short-circuits on `source_sha` match |
| Versioning | `metadata.parser_version`, `schema_version`; mismatch surfaces in eval reports |
| Cost telemetry | `core/llm.py` wraps every LLM call; logs to `kb_shim.cost_log` |
| Eval | `framework/eval/`; CI on every parser/store/retriever change |
| ACL placeholder | `metadata.persona_visibility` + `classification` on every ContentItem; Phase 4 enforces |

## Trust boundaries

```
[Consumer agent (Aira, portal)] —MCP/HTTPS→ [MCP server]
                                          ↓ (in-process)
                                       [Orchestrator]
                                          ↓ (in-process)
                                       [Persona skills]
                                          ↓ (in-process tool calls or HTTPS to MCP-mode adapters)
                                       [Stores]
                                          ↓ (TLS)
                                       [Oracle 23ai ADB]
                                          + [git wiki repo]
                                          + [UDAP/Sentinel]
                                          + [OpenAI]
```
- All LLM calls go through `core/llm.py` (one chokepoint).
- All store reads/writes go through the `Store` Protocol (one chokepoint).
- All adapter I/O goes through the `Adapter` Protocol (one chokepoint).
- Vault refs resolved at startup + 60s cache; never logged.

## Phase 1 architecture subset

For Phase 1 (incident KB end-to-end), the active subset is:
- Adapters: Confluence + Jira (both modes)
- Parser: `llm_parser.py` with `parsers/schemas/incidents/v1.json`
- Store: `incident_vector_store.py`
- Retrievers: `vector_search`, `get_incident_summary`, `list_sources`
- Orchestrator: minimal (fixed routing — incidents → Aira/Ops Eng skill)
- Persona skill: `ops_eng.py` (covers Aira's incident-KB needs)
- Eval: `recall@k`, `latency`, `cost`, sampled Ragas faithfulness

Everything else (other personas' skills, code wiki, FA graph, ACL) lives behind feature flags / unfilled config — present in the codebase but inactive until later phases enable them.

## LLM JSON output sanitisation (BUG-queue-573e3)

`framework/skill_builder/review.py::_llm_extract` must sanitise bare control
characters from OCI LLM JSON responses before calling `json.loads()`. The OCI
`JSON_OBJECT` response mode does NOT guarantee that `\n`, `\r`, or `\t`
characters inside JSON string values are backslash-escaped — multi-line
Confluence table-cell content can produce raw newlines that break the JSON
parser with "Unterminated string".

The helper `_escape_bare_control_chars(s)` walks the string character by
character, escaping raw control chars only while inside a double-quoted JSON
string (respecting `\`-escape sequences). Structural whitespace between keys is
outside any string and is left untouched.

Parse order in `_llm_extract` (fail-loud, no silent return {}):
1. `json.loads(cleaned)` — fast path for well-formed output.
2. `json.loads(_escape_bare_control_chars(cleaned))` — OCI bare-newline fix.
3. Extract `{...}` slice from sanitised text, `json.loads()` that.
4. If all fail: check `tokens_out` vs `max_tokens` (see BUG-queue-44364 below).
   - If `tokens_out >= max_tokens`: raise `ValueError` naming structural truncation
     (BUG-queue-44364). Never return `{}`.
   - Otherwise: raise `ValueError` naming both possible causes (BUG-queue-573e3
     and BUG-queue-44364) without asserting which is definite. Never return `{}`.

## LLM output max-token truncation (BUG-queue-44364)

`_llm_extract` uses `_EXTRACT_MAX_TOKENS = 4096` (raised from 2048 in the fix).
This matches `WorkflowExecutor._llm_extract_fields` (`executor.py` ~line 495)
so the eval preview path and the production runtime path cannot drift.

Root cause of original defect: with the old 2048-token ceiling, a 32-field
schema caused the OCI model to emit exactly 2048 tokens — the hard ceiling —
truncating the JSON mid-string (observed: cut at field 19, key beginning `"m`).
All three parse-recovery attempts failed because 14 fields were structurally
absent, not merely containing unescaped control chars (that is BUG-queue-573e3).

Detection: after `llm.chat()` returns, `tokens_out` is captured from the result
dict (`llm_oci.py` returns `{"text": ..., "tokens_out": ..., ...}`). If all
parse attempts fail AND `tokens_out >= max_tokens`, the error message explicitly
names structural truncation and BUG-queue-44364, directing the operator to
increase `max_tokens` or reduce schema size rather than chasing control chars.

Residual risk: 4096 is higher than 2048 but is still a finite ceiling. A schema
with significantly more than 32 dense fields, or with very long field
descriptions, could still hit the ceiling. If that occurs, the truncation
detection will fire and name the issue — the operator should reduce schema size
or split extraction into batches.

See also: `framework/tests/unit/test_review.py` for unit coverage.

## Where to read deeper

- [PDD](pdd/PDD-Knowledge-Builder-Framework.md) — executive narrative
- [data-model.md](data-model.md) — ContentItem/Chunk/Edge with multi-axis fields
- [api-design.md](api-design.md) — MCP retrieval tool surface
- [adr/](adr/) — every load-bearing decision
