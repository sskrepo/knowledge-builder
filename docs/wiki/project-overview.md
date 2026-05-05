---
title: Project Overview
source: docs/raw/knowledge-builder-framework-spec.md (§1, §2, §3)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [overview, framework]
status: current
---

# Knowledge Builder Framework — Project Overview

## What we are building
A **central knowledge layer for LLM consumption** so that every persona/agent on a Fusion Applications cloud platform (20K+ customer instances) does not re-ingest context from scratch. The framework ingests Confluence, Jira, code repos, and fleet/UDAP data; stores each data type in the backend that fits its access pattern (vector / SQL / graph / git-backed wiki); and exposes a uniform MCP retrieval surface to downstream agents.

This is **plumbing**, not content definition. Each persona team (PM, TPM, Architect, Dev Mgr, DevOps, Exec, Aira) plugs into the framework via a **Knowledge Builder agent** — a small YAML config + JSON extraction schema declaring (a) what to extract and (b) which raw sources to pull from. The framework runs the ingestion, stores the output, and lets downstream "use-case agents" (Aira, internal portals, coding assistants) query the resulting knowledge.

See [persona-knowledge-builder.md](persona-knowledge-builder.md) for the per-persona agent contract.

## Why now
- AI adoption is happening across all personas — without a shared knowledge layer, every agent solves the same context problem badly.
- Knowledge today is fragmented: Confluence (TPM wikis, ECARs, design docs), Jira (incidents, roadmaps), code (APIs, OpenAPI specs), UDAP/Sentinel (fleet metadata, ops events).
- A single unified store was rejected; **polyglot by design** (spec §2.1) is the operating principle.

## Who it serves
**Persona teams** (knowledge producers — write Knowledge Builder configs):
- PM — product definitions, feature briefs, release plans
- TPM — weekly ops summaries, ECARs, dependency graphs
- Architect — design docs, ADRs, system maps
- Dev Manager — engineering conventions, story lifecycle
- Dev — code knowledge, OpenAPI specs
- DevOps — runbooks, operational playbooks
- Exec — strategy decks, OKRs, board updates

**Use-case agents** (knowledge consumers — query via MCP):
- **Aira** — remote agent for incident response and code workflows (today's primary consumer; spec §4.1 already proven)
- Internal portals — search, Q&A, onboarding assistants
- Coding assistants (Codex-style)
- Future agents per persona

See [personas.md](personas.md) for detail.

## Value loop
```
Persona team writes a Knowledge Builder config (YAML + JSON-Schema)
        │
        ▼
Framework ingests sources → parsers → stores (Oracle 23ai Autonomous DB + git wiki)
        │
        ▼
MCP retrieval tools expose stores uniformly (spec §6.4)
        │
        ▼
Context Builder orchestrates: query → tool selection → retrieval → cited synthesis
        │
        ▼
Use-case agent (Aira, portals) gets grounded, cited answers — fast, cheap, repeatable
        │
        ▼
Eval harness (spec §10) blocks regressions; cost telemetry guards spend
```

## Architecture (4 layers, top → bottom)
```
Personas / Agents (PMs, TPMs, Devs, DevOps, Execs, Aira)
                ↓ queries
Context Builder Agent (LangGraph on OCI; intent → tools → cited synthesis)
                ↓ MCP tool calls
Retrieval Tools (search_wiki, vector_search, query_fleet, graph_traverse, ...)
                ↓
Stores (logical-polyglot inside Oracle 23ai Autonomous DB + git for wiki bodies)
                ↑ writes
Ingestion Pipelines (adapters + LLM/rule parsers; idempotent, content-hashed)
                ↑
Raw sources: Confluence · Jira · Code repos · UDAP/Sentinel
```
See [architecture.md](architecture.md) (TODO — Architect to author from spec §3 + ADRs).

## Tech stack (decided in Phase 0)
Full Oracle stack with two carve-outs:
- **OpenAI** for LLM and embeddings (Oracle-certified)
- **LangGraph on OCI** for orchestration

See [DECISION-001](../../pmo/decisions/DECISION-001-oracle-tech-stack.md) and [ADR-001](adr/ADR-001-tech-stack-baseline.md) for the full layer-by-layer mapping.

## v1 scope (Phase 1 exit gate per spec §12)
- Operational incident KB (spec §4.1) end-to-end: Confluence + Jira → LLM parser → Vector store → MCP tools → Context Builder
- ≥80% recall on a 25-question gold set per persona
- p95 retrieval latency <500ms with citations
- Eval CI green; cost telemetry on every ingest/retrieve

v2 layers in: PM/TPM persona Knowledge Builders, FA semantic graph, code wiki, fleet read-through, ACL enforcement.

## Out of scope (v1)
- User-facing UI (framework is plumbing; UIs are downstream)
- Per-persona LLM prompt templates (each consumer brings its own)
- Real-time wiki collaboration (git PR workflow is enough)
- Custom embedding model (OpenAI text-embedding-3-large is pinned)
- Cross-region replication (single-region v1)

## Open problems (research, not implementation — spec §8)
- §8.1 — LLM wiki storage and retrieval for remote agents (git+cached MCP vs TOC-on-demand vs BM25 vs graph-of-wikis vs hybrid). Phase 3 must pick.
- §8.2 — Code accessibility for remote agents (VM-spinup vs central pre-built code wiki vs hybrid). Phase 2 / Phase 3 boundary.
- §8.3 — TPM/PM extraction schema. **The framework's answer is ADR-004**: persona teams own their schemas; framework provides the contract + tooling.

## Key references
- Source spec: [docs/raw/knowledge-builder-framework-spec.md](../raw/knowledge-builder-framework-spec.md)
- Per-persona builder concept: [persona-knowledge-builder.md](persona-knowledge-builder.md)
- ADRs: [adr/](adr/)
- Phase plan: [../../pmo/phases.md](../../pmo/phases.md)
- Phase 0 kickoff: [../../pmo/phase-briefs/PHASE-0-kickoff.md](../../pmo/phase-briefs/PHASE-0-kickoff.md)
