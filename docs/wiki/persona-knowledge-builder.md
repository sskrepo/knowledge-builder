---
title: Persona Knowledge Builder Agent
source: user request 2026-05-04 (extends spec §4 + §7)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [framework, persona, ingestion]
status: draft
---

# Persona Knowledge Builder Agent

> **Status:** draft seed page. Captures the user's intent at kickoff. PM and Architect to formalize during Phase 0.

## Why this exists

The framework spec (`docs/raw/knowledge-builder-framework-spec.md`) describes the **infrastructure** (ingestion → stores → retrievers → orchestrator). It explicitly leaves *what* each domain extracts to the data-type teams (spec §2 principle 7, §8.3).

The **Persona Knowledge Builder** is the userspace concept that fills that gap: one agent per persona (PM, TPM, Architect, Dev Manager, DevOps, Exec, …), each declaring (a) what to extract, (b) which raw sources to pull from. Each persona-builder produces a knowledge base that downstream **use-case agents** (Aira, internal portals, Codex-style assistants) can query through the framework's uniform MCP retrieval surface.

```
┌─ persona builders (USERSPACE — config-driven) ──────────────┐
│  PM Knowledge Builder                                       │
│    extracts: {feature briefs, release plans, PRDs}          │
│    sources:  Confluence space "PRODUCT", Jira filter "PM-*" │
│  TPM Knowledge Builder                                      │
│    extracts: {weekly ops summaries, ECARs, dependencies}    │
│    sources:  Confluence space "TPM", Jira filter "OPS-*"    │
│  Architect Knowledge Builder, Dev Manager Knowledge Builder │
│  …                                                          │
└────────────────┬────────────────────────────────────────────┘
                 │ writes via parser contract (spec §6.2)
                 ▼
┌─ FRAMEWORK (this repo) ─────────────────────────────────────┐
│  ingestion · parsers · stores · retrievers · orchestrator   │
└────────────────┬────────────────────────────────────────────┘
                 │ MCP tools (spec §6.4)
                 ▼
┌─ use-case agents (DOWNSTREAM CONSUMERS) ────────────────────┐
│  Aira · internal portals · coding assistants · …            │
└─────────────────────────────────────────────────────────────┘
```

## What a persona-builder config looks like (proposed)

A persona-builder is a YAML config + (optionally) a domain-specific schema. The framework loads it, runs the ingestion pipeline, writes to the configured store(s), and registers the resulting corpus in the shim index.

```yaml
# persona_builders/pm.yaml
persona: pm
display_name: "PM Knowledge Builder"

# What to extract — fixed schema fed to the LLM parser (spec §2 principle 3,
# §6.2: "deterministic extraction rules over autonomous LLM extraction").
# Keep this short and concrete; let the parser do summarization, not field selection.
extraction_schema: parsers/schemas/pm.json
# parsers/schemas/pm.json defines fields like:
#   - feature_name, target_release, owner, persona_impacted,
#     dependencies, acceptance_criteria, links_to_design_docs

# Where to pull from — sources the framework already supports via adapters.
sources:
  - kind: confluence
    space: PRODUCT
    space_url: https://confluence.internal/spaces/PRODUCT
    include_labels: [pm, prd, feature-brief]
    exclude_labels: [archived]
  - kind: jira
    jql: 'project = PM AND issuetype in ("Feature","Epic")'
    filter_url: https://jira.internal/issues/?filter=12345
  - kind: git
    repo: git@github.com:org/product-design-docs.git
    paths: ["features/**/*.md"]

# Where this persona's KB lives. Storage choice follows retrieval pattern (spec §2 principle 4).
stores:
  - name: pm_wiki                 # git-backed LLM wiki for browsable summaries
    kind: wiki
    root_path: kb-wiki/pm/
  - name: pm_vectors              # embeddings for semantic recall
    kind: vector
    corpus: pm

# Retrieval surface this persona exposes to use-case agents.
retrieval_tools:
  - search_wiki
  - read_wiki_page
  - vector_search

# v1 metadata carried on every ContentItem (spec §2 principle 8, §10).
metadata_defaults:
  persona_visibility: [pm, tpm, architect, dev_mgr]
  owner: pm
  classification: internal

# Eval gold-set lives next to the config — every persona-builder must ship one.
eval:
  gold_set: eval/gold_sets/pm.jsonl
  exit_recall_at_k: 0.8
```

## Responsibilities — framework vs persona team

| Concern | Framework owns | Persona team owns |
|---|---|---|
| Adapters (Confluence/Jira/git/SQL) | ✅ | — |
| Parser engine (LLM + rule) | ✅ | — |
| Stores (vector/SQL/graph/wiki) | ✅ | — |
| Retrieval tools (MCP surface) | ✅ | — |
| Context Builder | ✅ | — |
| Eval harness (recall/faithfulness/latency/cost runners) | ✅ | — |
| **What to extract (schema)** | provides template + lint + "test your schema" CLI | **owns the schema and its versioning** |
| **Which sources to pull from** | validates source kinds it supports | **owns the source list (config)** |
| **Persona-specific gold set** | provides runner + thresholds | **writes the questions** |
| **Visibility/ACL** | enforces at retrieval (Phase 4) | declares `persona_visibility` per corpus |

This split aligns with spec §7 principle 7: *"The framework provides infrastructure, not content definition."*

## Lifecycle of a persona-builder config

1. **Draft** — persona team copies `parsers/schemas/_template.json` and `persona_builders/_template.yaml`. Edits both.
2. **Validate** — `kb-cli validate persona_builders/pm.yaml` checks: schema is valid JSON-schema, sources reference adapters that exist, stores reference store kinds that exist, gold-set file is readable.
3. **Dry-run** — `kb-cli ingest --dry-run --sample 5 persona_builders/pm.yaml` runs the LLM parser on 5 sample items and prints the extracted ContentItems for human review.
4. **Eval** — `kb-cli eval persona_builders/pm.yaml` runs the gold set against the dry-run output. Block promotion if recall@k < threshold.
5. **Promote v0 → v1** — once eval passes, the config flips to `status: production`. Ingestion runs on the framework's incremental schedule (webhooks/CDC).
6. **Iterate** — schema changes are versioned (`schema_version` on every chunk, spec §10). Reingest only impacted corpora.

## Shared knowledge model

Every persona-builder writes ContentItem/Chunk/Edge per spec §6.1. Cross-persona overlap is fine — two builders extracting the same Confluence page produce two ContentItems with different schemas/metadata, both indexed. The Context Builder uses the shim index (spec §6.6) to know which corpus to query for which intent.

This is also how a use-case agent (Aira, internal portal) gets a *unified* view: it doesn't talk to per-persona corpora directly; it asks the Context Builder, which routes by intent + persona visibility.

## Open questions for the user (file as DECISIONs)

1. **Initial persona set** — which Knowledge Builders ship in v1? Spec personas are PM, TPM, Architect, Dev Manager, Dev, DevOps, Exec, Aira. Suggest starting with *PM + TPM* (highest-leverage, most documentation surface) and *Aira's incident KB* (already proven).
2. **Config home** — `persona_builders/` in this repo, or a sibling repo per persona team? Spec is silent. Default: in this repo at `framework/persona_builders/`, one YAML per persona, with the persona team owning their PRs.
3. **Cross-persona deduplication** — when two builders ingest the same source page, do we (a) store both as separate ContentItems with different schemas, (b) merge under one ContentItem with multiple schema views, or (c) elect one canonical owner? Default proposed: (a) — simplest, lets Context Builder route by persona intent.
4. **Use-case-agent enrollment** — how does a downstream agent (Aira, Codex-style) declare which personas' KBs it needs? Default proposed: a `consumer_manifest.yaml` similar in shape to a persona-builder config but read-only.

## Relationship to spec §8.3 (open problem)

Spec §8.3 says TPM/PM extraction schema is unsolved and explicitly out of scope for the framework. This page **is** the framework's answer: *we don't define the schema; we define the contract a persona team uses to plug their schema in.* Phase 0 must produce the schema template + validate/dry-run/eval CLI; Phase 3 wires it into ingestion.

## Next steps

- PM: when ingesting the spec, link from `project-overview.md` and `personas.md` to this page.
- Architect: this page describes a contract — formalize as ADR-002 (or similar) and align with `core/interfaces.py` from spec §6.
- TPM: file DECISION-002 ("initial persona set for v1") once PM has drafted personas.md.
