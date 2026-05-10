---
title: Phases (V2)
source: docs/raw/knowledge-builder-framework-spec.md (§7) + DECISION-001/002/003/004 + PDD V2
compiled_at: 2026-05-09T00:00:00Z
created: 2026-05-03
updated: 2026-05-09
owner: tpm
tags: [meta, roadmap, v2]
status: current
---

# Phases (V2)

| # | Name | Status | Scope summary | New in V2 |
|---|------|--------|---------------|-----------|
| 0 | Setup | ✅ done | Tech-stack baseline, §6 interfaces, persona-builder contract, eval harness skeleton, dev mode + scaffolds | (V1 deliverable; V2 adds skill_builder/, workflow_runtime/, renderers/, deliverers/ scaffolds) |
| 1 | Skeleton + incident KB | ⏳ planned | core/ + Confluence/Jira adapters + LLM parser + Vector store + Context Builder; match Aira on incident gold set | Unchanged from V1 |
| 2 | Fleet + code wiki + skill-builder Phase A | ⏳ planned | Fleet read-through, Som code wiki, MCP tools; **NEW**: skill_builder synthesizes extraction skills | **NEW**: Track D — skill_builder Phase A |
| 3 | PM/TPM persona builders + workflow runtime | ⏳ planned | First non-incident personas in production; **NEW**: workflow runtime + first 3 workflow skills | **NEW**: workflow_runtime, renderers, deliverers, shim_workflows, tiered routing (Tier 1/2/3), 3 production workflow skills |
| 4 | Permissions + FA semantic graph + skill suggestion + polish | ⏳ planned | persona_visibility enforced; FA graph; **NEW**: skill suggestion loop | **NEW**: skill_suggester nightly clustering + weekly digest |

## Phase 0 — Setup ✅

**Goal:** Tech-stack baseline + interface contract + eval harness skeleton + Phase 1 backlog. **No production code.** ✅ Closed.

## Phase 1 — Skeleton + incident KB

**Goal:** Match or beat Aira's incident KB on a 25-question persona gold set.

**Exit criteria (subset of spec §12):**
- New incident ingest → retrievable in <5 min
- `vector_search` top-5 with citations <500 ms p95
- ≥80% recall on the persona gold set
- Re-ingesting same Jira ticket = zero rows changed (idempotency)
- Eval CI runs on every PR; merge blocked on regression
- All ContentItems carry `persona_visibility` and `classification`
- Cost report: tokens/ingest, tokens/retrieve, daily totals

**Calendar:** 8 weeks (4 FTE).

## Phase 2 — Fleet + code wiki + skill-builder Phase A

**Goal:** Mixed-source queries work; persona teams can author **extraction skills** via skill-builder (no workflow skills yet).

**Exit criteria:**
- Context Builder answers cross-source queries with citations (e.g., "fleet state for tenants impacted by INC-X")
- Fleet adapter is read-through (no ingestion); `query_fleet` + `text_to_sql` MCP tools live
- Code wiki regenerates on commit; `find_symbol` + `read_code_page` tools live
- DECISION-005 filed (code-access write-path substrate)
- **NEW**: `kb-cli skill-builder` synthesizes extraction skills end-to-end from intent + samples; one persona team uses it without YAML editing

**Calendar:** 8 weeks (5 FTE).

**Tracks:**
- Track A — Ingest: Fleet, code wiki
- Track B — Retrieve: query_fleet, text_to_sql, find_symbol, read_code_page
- Track C — Eval/Ops: cross-source eval queries
- Track D — Skill Builder Phase A (NEW): `analyze_artifact`, `synthesize_schema`, `synthesize_builder`, `reuse_detector`

## Phase 3 — PM/TPM persona builders + workflow runtime

**Goal:** First non-incident personas in production; **first 3 workflow skills shipping**; tiered routing live.

**Exit criteria:**
- TPM and PM persona builders graduated to `status: production`
- Each has gold set passing thresholds (recall@5 ≥ 0.80, faithfulness ≥ 0.85)
- **NEW**: `workflow_runtime` executes `on_request` workflow skills with PPT/DOCX rendering and OCI Object Storage delivery
- **NEW**: 3 workflow skills in production:
  - `tpm.weekly_exec_review` (schedule + on_request, pptx)
  - `ops_eng.incident_summary` (on_request, structured response)
  - `pm.release_brief` (on_request, docx)
- **NEW**: Tiered routing live in persona context skills (Tier 1 workflow match, Tier 2 KB retrieval)
- **NEW**: Skill builder Phase B — synthesizes workflow skills from example artifacts
- Multi-source query latency p95 <2 s

**Calendar:** 12 weeks (6 FTE).

**Tracks:**
- Track A — Wiki: WikiMetadataStore + git-backed wiki bodies + Confluence ingestion (via skill builder)
- Track B — Workflow Runtime (NEW): executor, trigger_dispatcher, renderers, deliverers, shim_workflows
- Track C — Skill Builder Phase B (NEW): synthesize_workflow, analyze_artifact PPT/DOCX modes, skill builder UX polish
- Track D — Persona Builds (NEW): TPM/PM/ops_eng author 3 workflow skills via skill-builder; eval them; promote

## Phase 4 — Permissions + FA semantic graph + skill suggestion + polish

**Goal:** v2-ready ops posture; **skill suggestion loop active**.

**Exit criteria:**
- `persona_visibility` enforced at retrieval (filtered at SQL level)
- FA semantic graph integrates Dave's POC; `vector → graph_traverse` round-trip <1 s p95
- **NEW**: skill_suggester nightly clustering produces persona-team weekly digest
- **NEW**: At least 1 skill authored from a Tier-4 suggestion (closed-loop validation)
- **NEW**: 8+ workflow skills in production across personas
- Cost dashboards live; eval CI hardened

**Calendar:** 8 weeks (5 FTE).

**Tracks:**
- Track A — Permissions: persona_visibility SQL-level enforcement, consumer manifest, audit log retention
- Track B — Graph: FA semantic graph store, resource ontology bootstrap, graph traversal MCP tool
- Track C — Skill Suggestion Loop (NEW): skill_suggester nightly job, weekly digest, kb-cli skill-builder --resume
- Track D — Ops: cost dashboards, latency SLOs, eval CI hardening

## Resource asks

| Phase | FTE peak | Calendar | Persona-team commitment |
|---|---|---|---|
| 0 | 1 | done | 0 |
| 1 | 4 | 8 weeks | 0 |
| 2 | 5 | 8 weeks | 0 (skill-builder uses sample data first) |
| 3 | 6 | 12 weeks | TPM (~20%), PM (~20%), ops_eng (~10%) |
| 4 | 5 | 8 weeks | All personas (~5% each, async via digest) |

Total to Phase-4 exit: ~9 months with 5–6 FTE peak in Phase 3.
