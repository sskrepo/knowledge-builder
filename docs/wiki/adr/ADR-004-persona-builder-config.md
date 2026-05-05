---
title: ADR-004 — Persona-builder config schema
status: accepted
created: 2026-05-04
owner: architect
tags: [adr, persona, config, phase-0]
related: [ADR-003, persona-knowledge-builder, DECISION-004]
---

# ADR-004 — Persona-builder config schema

## Status
Accepted (2026-05-04). Formalizes the contract proposed in [docs/wiki/persona-knowledge-builder.md](../persona-knowledge-builder.md).

## Context
Per DECISION-004, v1 ships PM, TPM, and Aira's incident KB as Knowledge Builders. Each persona's KB is configuration-driven: a YAML config (sources, stores, retrieval tools, eval) plus a JSON-Schema document for extraction. This ADR defines the contract framework so persona teams can ship configs without touching framework code.

## Decision

### Persona-builder config: YAML schema
Location: `framework/persona_builders/{persona}.yaml`

```yaml
# REQUIRED FIELDS
persona: pm                    # short id; matches schema dir name
display_name: "PM Knowledge Builder"
schema_version: 1              # bump on breaking schema changes; triggers re-ingest
status: draft | production     # draft = not run on schedule; production = scheduled

# What to extract — REQUIRED. References a JSON-Schema doc the LLM parser uses.
extraction_schema: parsers/schemas/pm/v1.json

# Where to pull from — REQUIRED. At least one source, kinds restricted to registered adapters.
sources:
  - kind: confluence            # adapter must exist in framework/adapters/
    space: PRODUCT
    space_url: https://confluence.internal/spaces/PRODUCT
    include_labels: [pm, prd, feature-brief]
    exclude_labels: [archived]
    auth_secret_ref: vault://kb/confluence-readonly      # OCI Vault path
  - kind: jira
    jql: 'project = PM AND issuetype in ("Feature","Epic")'
    filter_url: https://jira.internal/issues/?filter=12345
    auth_secret_ref: vault://kb/jira-readonly
  - kind: git
    repo: git@github.com:org/product-design-docs.git
    paths: ["features/**/*.md"]
    auth_secret_ref: vault://kb/git-readonly

# Where to write — REQUIRED. Each entry maps to a Store impl per ADR-002.
stores:
  - name: pm_wiki_meta
    kind: wiki                  # WikiMetadataStore (kb_wiki_meta) + git for bodies
    root_path: kb-wiki/pm/
  - name: pm_vectors
    kind: vector                # IncidentVectorStore-style schema, corpus="pm"
    corpus: pm

# Retrieval surface this persona contributes to. Tools must exist in framework/retrievers/.
retrieval_tools:
  - search_wiki
  - read_wiki_page
  - vector_search

# Metadata defaults injected into every ContentItem this builder produces. REQUIRED.
metadata_defaults:
  persona_visibility: [pm, tpm, architect, dev_mgr]   # who can read
  owner: pm                                           # who maintains
  classification: internal                            # public | internal | restricted

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
  schedule: "0 */4 * * *"       # cron; default = on-change webhooks only
  max_concurrent_workers: 4
  cost_alarm_tokens_per_day: 5000000
```

### Extraction schema: JSON-Schema layout
Location: `framework/parsers/schemas/{persona}/v{N}.json`

Constraints:
- MUST be a valid JSON Schema 2020-12 document.
- MUST declare `required` fields explicitly.
- MUST keep field count modest (<= ~15) — large schemas dilute LLM extraction quality.
- SHOULD include `description` on every property; the LLM parser injects descriptions into the prompt.
- MAY include `enum` constraints for controlled vocabularies.

Versioning rule:
- Backward-compatible additions → bump `schema_version` patch (config-side `schema_version` does not change). No re-ingest required.
- Breaking field rename / removal → new schema file `v{N+1}.json`, config bumps `schema_version`. Re-ingest impacted corpus; old rows tagged superseded but retained for diff.

### Lifecycle (per persona-knowledge-builder.md, formalized)
| Phase | Action | CLI |
|---|---|---|
| Draft | Copy `_template.yaml` and `_template.json`. Edit. | (manual) |
| Validate | Lint config: schema valid, sources reference known adapters, stores reference known kinds, gold set readable. | `kb-cli validate persona_builders/pm.yaml` |
| Dry-run | Run LLM parser on N=5 sample items; print extracted ContentItems for human review. No DB writes. | `kb-cli ingest --dry-run --sample 5 persona_builders/pm.yaml` |
| Eval | Run gold set against dry-run output; compute recall/faithfulness/latency/cost. Block promote on miss. | `kb-cli eval persona_builders/pm.yaml` |
| Promote | Flip `status: draft` → `status: production`. Scheduler picks it up. | `kb-cli promote persona_builders/pm.yaml` |
| Iterate | Bump `schema_version` on breaking change → re-ingest impacted corpus. | `kb-cli reingest persona_builders/pm.yaml --schema-version 2` |

### Cross-persona dedup (per persona-knowledge-builder.md Q3)
When two builders ingest the same source page, the framework writes **two ContentItems** with different `id`s (because `id` includes `schema_version` per ADR-002). Both are indexed; the Context Builder routes by persona/intent. This is the simplest behavior and keeps each persona's schema sovereign.

### Use-case-agent enrollment (per persona-knowledge-builder.md Q4)
Use-case agents (Aira, internal portals) declare which persona corpora they consume in `framework/consumer_manifest.yaml`:

```yaml
agent: aira
corpora_required:
  - incident_kb
  - pm_vectors
  - fa_semantic
mcp_tools:
  - vector_search
  - graph_traverse
  - get_incident_summary
```

The MCP server enforces `persona_visibility` against this manifest at retrieval time (Phase 4 hardens it).

## Considered alternatives
- **One mega-config covering all personas** — easier to read but couples persona teams; rejected.
- **Code-defined builders (Python class per persona)** — flexible but defeats the "persona team can ship without touching framework" goal. Rejected.
- **Auto-generate schema from sample data** — interesting but violates spec §2.3 (deterministic extraction rules over autonomous LLM extraction). Rejected.

## Consequences
- Persona teams have a small, reviewable surface (one YAML + one JSON Schema). Framework owns plumbing.
- The `kb-cli` command becomes the persona team's primary interface; Phase 0 ships its skeleton, Phase 3 fully implements `validate`/`dry-run`/`eval`/`promote`.
- ADR-003's `Store` and `Parser` Protocols stay unchanged.

## References
- [docs/wiki/persona-knowledge-builder.md](../persona-knowledge-builder.md)
- [DECISION-004](../../../pmo/decisions/DECISION-004-initial-persona-set.md)
- Spec §2.3 (deterministic extraction), §6.2 (Parser contract), §8.3 (open problem this ADR resolves).
