---
title: ADR-004 — Persona-builder config schema
status: accepted (v2 — amended 2026-05-05)
created: 2026-05-04
amended: 2026-05-05
owner: architect
tags: [adr, persona, config, phase-0]
related: [ADR-003, ADR-006, ADR-007, ADR-008, persona-knowledge-builder, DECISION-004]
---

# ADR-004 — Persona-builder config schema

## Status
Accepted (v1: 2026-05-04). **Amended v2 on 2026-05-05** to:
1. Rename `stores:` → `knowledge_bases:` (matches user's vocabulary; aligns with framework name).
2. Document **polyglot-per-persona** as a top-level principle.
3. Add **KB cards** — each knowledge base self-describes its capabilities for the orchestrator.
4. Cross-references to ADR-006 (two-shim architecture), ADR-007 (persona context skill contract), ADR-008 (functional-area + resources dimensions).

## Context
Per DECISION-004, v1 ships PM, TPM, and Aira's incident KB as Knowledge Builders. Each persona's KB surface is configuration-driven: a YAML config (sources, knowledge_bases, retrieval tools, eval) plus one or more JSON-Schema documents for extraction. This ADR defines the contract so persona teams can ship configs without touching framework code.

## Foundational principle: polyglot per persona

A single persona's knowledge is rarely one shape. The right architecture lets a persona declare a **bundle of knowledge_bases**, each in the storage shape that fits its access pattern.

| Kind of knowledge | Storage shape | Example |
|---|---|---|
| Concepts, designs, decisions, runbooks, procedures | **wiki** (markdown + frontmatter, body in git) | "POD refresh design", "Escalation runbook" |
| Incidents, support tickets, observations | **vector** (embeddings + metadata in `kb_*`) | "INC-12345: PODDB stuck refresh" |
| Resource & service relationships | **graph** (Oracle Property Graph in `kb_fa_semantic`) | "POD contains PODDB" |
| Fleet inventory, ticket fields, time-series state | **sql_passthrough** (allowlisted UDAP/Sentinel views) | "instances on patch 24.05.1" |
| Code structure | **wiki + symbol_index** (markdown + AST table) | "where is auth implemented?" |

This is spec §2.1 ("polyglot, not unified") and §2.4 ("storage is consequence of retrieval pattern") applied **inside** a persona, not just across personas. **Markdown is the default for narrative content; it is the wrong shape for high-volume observational data, structured rows, or relationships.**

## Decision

### Persona-builder config: YAML schema
Location: `framework/persona_builders/{persona}.yaml`

```yaml
# REQUIRED FIELDS ----------------------------------------------------
persona: pm                       # short id; matches schema dir name
display_name: "PM Knowledge Builder"
schema_version: 1                 # bump on breaking schema changes; triggers re-ingest
status: draft | production        # draft = not run on schedule; production = scheduled

# Primary axis for content layout (per ADR-008)
primary_axis: feature_or_release  # one of: functional_area | service_id | feature_or_release | program

# Allowed values for tag fields (controlled vocabularies, sourced from shim_faaas — see ADR-006)
functional_areas: []              # empty if not relevant for this persona
resources_relevant: []            # which resource types this persona's content references
kinds_supported: [concept, procedure, decision, runbook, incident-history, postmortem, design]

# KNOWLEDGE BASES — REQUIRED. Each entry is one shape-coherent body of knowledge.
# Per the polyglot-per-persona principle, multiple kinds are normal and expected.
knowledge_bases:
  - name: pm_briefs
    kind: wiki                    # → kb_wiki_meta + git for bodies
    extraction_schema: parsers/schemas/pm/briefs/v1.json
    sources:
      - { kind: confluence, space: PRODUCT, include_labels: [prd, feature-brief],
          auth_secret_ref: vault://kb/confluence-readonly }
    retrieval_tools: [search_wiki, read_wiki_page]
    kb_card:                      # see "KB cards" below
      summary: "Product feature briefs and PRDs from PRODUCT space."
      use_when: "Question is about a feature definition, scope, or rationale."
      input_shape: "Natural-language question; optional release filter."
      output_shape: "Cited passages from one or more PRD pages."

  - name: pm_release_plans
    kind: wiki
    extraction_schema: parsers/schemas/pm/release-plans/v1.json
    sources:
      - { kind: confluence, space: PRODUCT, include_labels: [release-plan] }
      - { kind: jira, jql: 'project = PM AND issuetype = "Release"' }
    retrieval_tools: [search_wiki, read_wiki_page]
    kb_card:
      summary: "Per-release scope, target dates, gating risks."
      use_when: "Question references a release version (e.g. 25.01) or asks 'what's planned'."
      input_shape: "Question, optional release filter."
      output_shape: "Cited release-plan excerpts."

  - name: pm_market_research
    kind: vector                  # → kb_research vector schema
    extraction_schema: parsers/schemas/pm/research/v1.json
    sources:
      - { kind: confluence, space: PRODUCT, include_labels: [research] }
    retrieval_tools: [vector_search]
    kb_card:
      summary: "Competitive scans, market positioning notes."
      use_when: "Question is about competitors, pricing, or market gaps."
      input_shape: "Natural-language question."
      output_shape: "Top-K cited passages with similarity scores."

# Metadata defaults injected into every ContentItem this builder produces (spec §10).
metadata_defaults:
  persona_visibility: [pm, tpm, architect, dev_mgr, eng_mgr]
  owner: pm
  classification: internal

# Eval gate — REQUIRED. Builder cannot promote to production without passing.
eval:
  gold_set: eval/gold_sets/pm.jsonl
  exit_criteria:
    recall_at_5: 0.80
    faithfulness: 0.85
    p95_latency_ms: 800
    max_tokens_per_query: 2000

# Optional: rate limits / scheduling
ingestion:
  schedule: "0 */4 * * *"
  max_concurrent_workers: 4
  cost_alarm_tokens_per_day: 5000000
```

### KB cards (NEW in v2)

Every `knowledge_bases[].kb_card` is a structured self-description used by:
- The **shim_kb layer** (ADR-006) — the orchestrator and persona skills read these to decide which KB(s) to query.
- The **persona context skill** (ADR-007) — uses `use_when` for intent matching and `input_shape`/`output_shape` for tool dispatch.

Required fields:
| Field | Purpose |
|---|---|
| `summary` | One-sentence description of what this KB contains |
| `use_when` | Intent triggers — phrased so an LLM can match a query to the KB |
| `input_shape` | What the retrieval tool expects (free text? structured filter?) |
| `output_shape` | What it returns (passages? rows? graph paths?) — sets caller expectations |

Optional:
| Field | Purpose |
|---|---|
| `do_not_use_for` | Anti-patterns; reduces misroutes |
| `freshness` | How current the data is (e.g., "near-real-time", "weekly snapshot") |
| `expected_volume` | Rough size hint (e.g., "<100 items", "~10K items") — informs budget |

KB cards are auto-aggregated into `shim_kb` at config-load time. No code changes needed when a persona team adds, edits, or removes a KB.

### Allowed `kind:` values

| `kind` | Maps to | Schema (see ADR-002) |
|---|---|---|
| `vector` | `IncidentVectorStore`-style schema (per corpus) | `kb_<corpus>` |
| `wiki` | `WikiMetadataStore` + git for bodies | `kb_wiki_meta` |
| `graph` | `FaSemanticGraphStore` (often shared) | `kb_fa_semantic` |
| `sql_passthrough` | `FleetReadThroughStore` wrapping allowlisted views | (existing UDAP/Sentinel) |
| `code_index` | `CodeStructuralStore` (markdown + AST) | `kb_code` |

New kinds require a new `Store` Protocol implementation per ADR-003 + ADR-002. Persona teams cannot add a kind unilaterally.

### Extraction schema: JSON-Schema layout
Location: `framework/parsers/schemas/{persona}/{kb-or-kind}/v{N}.json`

A persona may have **multiple extraction schemas** — one per KB whose `kind` requires LLM extraction (`vector` and `wiki`; not `graph`, `sql_passthrough`, `code_index`).

Constraints:
- MUST be valid JSON Schema 2020-12.
- MUST declare `required` fields explicitly.
- MUST keep field count modest (≤ ~15) — large schemas dilute LLM extraction quality.
- SHOULD include `description` on every property; the LLM parser injects descriptions into the prompt.
- MAY include `enum` constraints for controlled vocabularies.

Versioning rule:
- Backward-compatible additions → no `schema_version` bump. No re-ingest required.
- Breaking field rename / removal → new schema file `v{N+1}.json`, builder bumps `schema_version`. Re-ingest impacted corpus; old rows tagged superseded but retained for diff.

### Lifecycle
| Phase | Action | CLI |
|---|---|---|
| Draft | Copy `_template.yaml` and `_template.json`. Edit. | (manual) |
| Validate | Lint config: schema valid; sources reference known adapters; knowledge_bases reference known kinds; gold set readable; KB cards complete. | `kb-cli validate persona_builders/pm.yaml` |
| Dry-run | Run LLM parser on N=5 sample items per KB; print extracted ContentItems for human review. No DB writes. | `kb-cli ingest --dry-run --sample 5 persona_builders/pm.yaml` |
| Eval | Run gold set against dry-run output; compute recall/faithfulness/latency/cost. Block promote on miss. | `kb-cli eval persona_builders/pm.yaml` |
| Promote | Flip `status: draft` → `status: production`. Scheduler picks it up. Shim_kb refreshed automatically. | `kb-cli promote persona_builders/pm.yaml` |
| Iterate | Bump `schema_version` on breaking change → re-ingest impacted KB. | `kb-cli reingest persona_builders/pm.yaml --kb pm_briefs --schema-version 2` |

### Cross-persona deduplication
When two builders ingest the same source page, the framework writes **two ContentItems** with different `id`s (because `id` includes `schema_version` per ADR-002). Both are indexed; routing by persona happens at the orchestrator/shim_kb layer (ADR-006). Storage cost is small relative to the simplicity win.

### Use-case-agent enrollment
Consumer agents (Aira, internal portals) declare which persona KBs they consume in `framework/consumer_manifest.yaml`:

```yaml
agent: aira
knowledge_bases_required:
  - ops_incidents
  - ops_runbooks
  - ops_dependencies
  - ops_fleet_state
mcp_tools:
  - vector_search
  - search_wiki
  - graph_traverse
  - query_fleet
```

The MCP server enforces `persona_visibility` against this manifest at retrieval time (Phase 4 hardens enforcement).

## Considered alternatives
- **One mega-config covering all personas** — easier to read but couples persona teams; rejected.
- **Code-defined builders (Python class per persona)** — flexible but defeats the "persona team can ship without touching framework" goal. Rejected.
- **Auto-generate schema from sample data** — interesting but violates spec §2.3 (deterministic extraction rules over autonomous LLM extraction). Rejected.
- **Single KB per persona** — simpler but forces wrong storage shapes; rejected per the polyglot principle above.

## Consequences
- Persona teams have a small, reviewable surface (one YAML + one or more JSON Schemas). Framework owns plumbing.
- `kb-cli` is the persona team's primary interface; Phase 0 ships its skeleton, Phase 3 fully implements `validate`/`dry-run`/`eval`/`promote`.
- ADR-003's `Store` and `Parser` Protocols stay unchanged; `knowledge_bases[].kind` maps to `Store.kind`.
- Adding a new persona at any phase = config-only (no framework change), as long as kinds are already supported.

## References
- [docs/wiki/persona-knowledge-builder.md](../persona-knowledge-builder.md)
- [DECISION-004](../../../pmo/decisions/DECISION-004-initial-persona-set.md)
- [PDD §5, §15](../pdd/PDD-Knowledge-Builder-Framework.md)
- [ADR-006 — Two-shim layered architecture](ADR-006-two-shim-architecture.md)
- [ADR-007 — Persona context skill contract](ADR-007-persona-context-skill.md)
- [ADR-008 — Functional-area & resource dimensions](ADR-008-functional-area-and-resources.md)
- Spec §2.1, §2.3, §6.2, §8.3
