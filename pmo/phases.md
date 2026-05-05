---
title: Phases
source: docs/raw/knowledge-builder-framework-spec.md (§7) + DECISION-001/002/003/004
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-03
owner: tpm
tags: [meta, roadmap]
status: current
---

# Phases

| # | Name | Status | Scope summary | Stories |
|---|------|--------|---------------|---------|
| 0 | Setup | 🟡 active | Tech-stack baseline (Oracle 23ai + OpenAI + LangGraph), §6 interfaces, persona-builder contract, eval harness skeleton, no production code | (Phase 0 is documentation/config — no stories yet; backlog for Phase 1 starts post-Gate-1) |
| 1 | Skeleton + incident KB | ⏳ planned | `core/` interfaces + Confluence/Jira adapters + LLM parser w/ incident schema + Vector store + `vector_search` & `get_incident_summary` MCP tools + minimal Context Builder | TBD by PM after Gate 1 |
| 2 | Fleet + code wiki | ⏳ planned | Read-through fleet adapter + `query_fleet` & `text_to_sql` + Som-style code wiki on commit + `read_code_page` & `find_symbol` | TBD |
| 3 | PM/TPM wiki + Context Builder maturity | ⏳ planned | Git-backed wiki store + frontmatter + Confluence→wiki ingestion (per-persona schema plug-in) + shim index + intent classification + parallel tool calls. Resolves spec §8.1 + §8.3 | TBD |
| 4 | Permissions, FA semantic graph, polish | ⏳ planned | `persona_visibility` enforced at retrieval + FA graph store (Dave's POC) + Jira roadmap approach + cost dashboards + eval CI hardening | TBD |

## Phase 0 — Setup

**Goal:** Tech-stack baseline + interface contract + eval harness skeleton + Phase 1 backlog. **No production code.**

**Exit criteria:**
- ✅ Tech stack decided — DECISION-001/002/003 + ADR-001
- ✅ Storage shape decided — ADR-002
- ✅ Core interfaces specified — ADR-003
- ✅ Persona-builder contract decided — ADR-004 + DECISION-004
- ✅ Eval harness defined — ADR-005
- ⏳ Gate 1 approved by user (see dashboard)
- ⏳ External provisioning (Oracle ADB, OpenAI key, OCI Vault) delivered — see [pending-decisions/PHASE-0.md](pending-decisions/PHASE-0.md)
- ⏳ Phase 1 backlog drafted

**Deliverables (Phase 0):**
- ADRs in `docs/wiki/adr/` (✅ 5 drafted)
- DECISIONs in `pmo/decisions/` (✅ 4 filed)
- PM wiki: project-overview, personas, 6 module pages (✅)
- Persona-builder + extraction-schema templates (✅)
- Incident extraction schema v1 (✅)
- Eval gold-set seed (✅)
- Phase 0 Kickoff Brief (✅)
- Pending-decisions surface for all phases (✅)
- Phase 1 backlog (⏳ awaiting Gate 1)

## Phase 1 — Skeleton + incident KB (preview)

**Goal:** Match or beat Aira's current incident KB on a 25-question persona gold set.

**Exit criteria (subset of spec §12 acceptance):**
- New incident ingest → retrievable in <5 min
- `vector_search` top-5 with citations <500ms p95
- ≥80% recall on the persona gold set
- Re-ingesting same Jira ticket = zero rows changed (idempotent)
- Eval CI runs on every PR; merge blocked on regression
- All ContentItems carry `persona_visibility` and `classification`
- Cost report: tokens/ingest, tokens/retrieve, daily totals

**Deliverables:**
- `framework/core/interfaces.py`, `framework/core/content.py` (per ADR-003)
- Confluence + Jira adapters (read-only)
- LLM parser using `incidents/v1.json`
- IncidentVectorStore + edges (per ADR-002)
- MCP tools: `vector_search`, `get_incident_summary`
- Minimal Context Builder (fixed routing for incident queries)
- Eval harness Python implementation + CI wiring (per ADR-005)

## Phase 2 — Fleet + code wiki (preview)

**Goal:** Mixed-source queries work (e.g., "show fleet state for tenants impacted by incident X").

**Exit criteria:**
- Context Builder answers mixed queries with citations
- Fleet adapter is read-through (no ingestion)
- Code wiki regenerates on commit; `find_symbol` returns cited results

## Phase 3 — PM/TPM wiki + Context Builder maturity (preview)

**Goal:** Multi-source queries return cited, faithful answers within budget. Resolves spec §8.1 (LLM-wiki storage) and §8.3 (per-persona extraction schemas).

**Exit criteria:**
- PM and TPM Knowledge Builder configs ship in `framework/persona_builders/`
- Each persona's gold set lives at `eval/gold_sets/{persona}.jsonl`; recall@5 ≥ 80%
- `kb-cli validate / dry-run / eval / promote` fully implemented
- Wiki content survives a re-ingest with zero changed rows when source unchanged
- Cross-source query latency p95 <2s

## Phase 4 — Permissions, FA semantic graph, polish (preview)

**Goal:** v2-ready ops posture.

**Exit criteria:**
- `persona_visibility` enforced at the retrieval layer (not just placeholder metadata)
- FA semantic graph store integrates Dave's POC; `vector → graph_traverse` round-trip <1s p95
- Jira roadmap aggregation decided (DECISION-009 or similar)
- Ops: cost dashboards, retrieval latency SLOs, eval CI hardened
