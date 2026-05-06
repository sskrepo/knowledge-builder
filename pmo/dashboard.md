---
title: Project Dashboard
source: derived from pmo/stories, pmo/decisions, pmo/handoffs
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-03
owner: tpm
tags: [meta, live]
status: current
---

# Knowledgebase — Dashboard

**Current phase:** Phase 0 — Setup
**Updated:** 2026-05-04 by tpm (autonomous Phase 0 run)

## 🌅 Morning briefing — 2026-05-06 (Phase 1 implementation complete)

**Phase 1 code is done.** ~4150 LOC across 84 Python files. Two final commits landed:
- ADRs 012/013 + ADR-005/007 amendments (per AIRA comparison)
- Phase 1 implementation pass: adapters, parsers, vector store, retrievers, orchestrator, persona skills, ingestion, eval harness, FastAPI MCP server, kb-cli, CI gate, unit tests, dev-guide, runbook, 22 backlog stories

**Honest status:** code is structurally complete and import-clean. Integration verification requires real provisioning. External-touchpoint markers in every file flag what needs ADB / OpenAI / live Jira / AIRA gold-set queries to run.

**What you can do today (no provisioning needed):**
1. Read `docs/wiki/engineering/dev-guide.md` for first-run instructions
2. Read `docs/wiki/onboarding/pm-tpm.md` (and `onboarding/ops-eng.md`) and share with persona team leads in parallel
3. Review the 22 stories in `pmo/stories/STORY-001..022.md`
4. Run `pytest framework/tests/unit/` (no external deps; verifies chunker, ids, urns, recall metrics, ADR-013 filter strictness)

**What unlocks Phase 1 exit gate (80% recall on 25-question gold set):**
1. Provision Oracle 23ai ADB (dev tier) + populate Vault + run `kb-cli migrate --schema kb_incidents --env dev`
2. OpenAI API key in Vault (Oracle-certified)
3. Confluence + Jira tokens in Vault
4. AIRA team shares ~50 query/citation pairs from their eval harness → bootstrap `eval/gold_sets/incidents.jsonl` to 25 entries
5. Run `kb-cli ingest` against a Jira sample → verify end-to-end works
6. Run `kb-cli eval` → if recall ≥ 80% and faithfulness ≥ 0.85, Phase 1 exits

---

## 🌅 Earlier briefing — 2026-05-05

**TL;DR.** Phase 0 documentation/config layer is **drafted in full**: 4 DECISIONs, 5 ADRs, 8 PM wiki pages, persona-builder + extraction-schema templates, an incident extraction schema (v1), a 5-question seed eval gold-set, the Phase-0 kickoff brief, the pending-decisions surface for all phases, an initial git commit, **and a consolidated PDD + executive brief** capturing all architectural decisions including knowledge_bases (rename of corpora), the 5-layer architecture, polyglot per persona, skills-default, functional-area + resources dimensions. **No production code was written** — Phase 0 is docs/configs only by design.

**📄 Two stakeholder documents** (also as office files):
- **[PDD — Knowledge Builder Framework](../docs/wiki/pdd/PDD-Knowledge-Builder-Framework.md)** ([.docx](../docs/wiki/pdd/PDD-Knowledge-Builder-Framework.docx))
- **[Executive Brief](../docs/wiki/exec-brief.md)** ([.pptx](../docs/wiki/exec-brief.pptx))

**🆕 Architect kickoff complete** (overnight 2026-05-05 → 2026-05-06):
- ADRs 006–011 published in [docs/wiki/adr/](../docs/wiki/adr/)
- ADR-004 amended (corpora → knowledge_bases, polyglot principle, KB cards)
- [architecture.md](../docs/wiki/architecture.md), [data-model.md](../docs/wiki/data-model.md), [api-design.md](../docs/wiki/api-design.md) authored
- Configuration plane built in [framework/config/](../framework/config/) — env yamls + adapter yamls (dual-mode Confluence/Jira) + shim_faaas + scripts
- Adapter stubs in [framework/adapters/](../framework/adapters/) — Confluence native + MCP, Jira native + MCP, Git, UDAP
- 8 persona context skill stubs in [framework/persona_skills/](../framework/persona_skills/)
- **Full Option-3 starter pack**: 8 persona builder configs + 22 extraction schemas + 8 gold sets across all producer personas (PM, TPM, Architect, Eng Mgr, Developer, Ops Mgr, Ops Eng, Service Owner). All marked `status: draft` for persona teams to refine.

**You are gating two things to unblock Phase 1:**
1. **Gate 1 review** — read the ADRs + PM ingest. Reply `GATE-1-PHASE-0: approved` (or per-artifact: `ADR-001: approved`, etc.). See "🔴 Approval gates" below.
2. **🚨 External provisioning** — 3 blocking items in [pending-decisions/PHASE-0.md](pending-decisions/PHASE-0.md) that only you can do (Oracle 23ai ADB, OpenAI key, OCI Vault). The Phase-0 kickoff brief has step-by-step instructions: [phase-briefs/PHASE-0-kickoff.md](phase-briefs/PHASE-0-kickoff.md).

**Where to read:**
- 30-second view: [docs/wiki/current-status.md](../docs/wiki/current-status.md)
- Full picture: [docs/wiki/project-overview.md](../docs/wiki/project-overview.md)
- Tech stack: [docs/wiki/adr/ADR-001-tech-stack-baseline.md](../docs/wiki/adr/ADR-001-tech-stack-baseline.md)
- Storage shape (the §2.1 polyglot reconciliation): [docs/wiki/adr/ADR-002-storage-shape.md](../docs/wiki/adr/ADR-002-storage-shape.md)
- Persona-builder contract: [docs/wiki/adr/ADR-004-persona-builder-config.md](../docs/wiki/adr/ADR-004-persona-builder-config.md)

**Notable decisions you should sanity-check when awake:**
- DECISION-002 — I read your "go full Oracle stack with Autonomous DB as converged DB" as *physical-converged, logical-polyglot* (each data type has its own schema and access pattern, sharing one DB). If you wanted strict §2.1 (separate DB instances per data type), say so and I'll revisit.
- DECISION-004 — v1 personas locked to PM + TPM + Aira. Architect/Dev Mgr/etc. are deferred to Phase 4+. Adjust if you want broader v1 scope.

## 📋 Current Phase Kickoff
[PHASE-0-kickoff.md](phase-briefs/PHASE-0-kickoff.md) — `status: awaiting-external-setup`

## 🔴 Approval gates — Phase 0

### Gate 1 — ADRs + PM ingest (awaiting your approval)
- [ADR-001 — Tech-stack baseline](../docs/wiki/adr/ADR-001-tech-stack-baseline.md)
- [ADR-002 — Storage shape](../docs/wiki/adr/ADR-002-storage-shape.md)
- [ADR-003 — Core interfaces](../docs/wiki/adr/ADR-003-core-interfaces.md)
- [ADR-004 — Persona-builder config](../docs/wiki/adr/ADR-004-persona-builder-config.md)
- [ADR-005 — Eval harness](../docs/wiki/adr/ADR-005-eval-harness.md)
- [project-overview](../docs/wiki/project-overview.md)
- [personas](../docs/wiki/personas.md)
- 6 module pages: [incidents](../docs/wiki/module-incidents.md) · [fleet](../docs/wiki/module-fleet.md) · [code](../docs/wiki/module-code.md) · [pm-tpm-wiki](../docs/wiki/module-pm-tpm-wiki.md) · [fa-graph](../docs/wiki/module-fa-graph.md) · [jira-roadmap](../docs/wiki/module-jira-roadmap.md)
- Reply: `GATE-1-PHASE-0: approved` (or per-artifact: `ADR-NNN: approved` / `WIKI-{name}: approved`)

### Gate 2 — Interface spec (blocked on Gate 1)
- This framework has no UI; "Gate 2" here means the MCP retrieval-tool surface in ADR-003 §retrievers + the §6.4 OpenAPI-equivalent interface contract. Surfaced once Gate 1 passes.

## 🔴 Decisions awaiting your review

| # | Title | Why | Options |
|---|-------|-----|---------|
| (none open) — DECISIONs 001–004 are decided. | | | |

## 🟡 In-flight work

| Story | Module | Status | Owner | Blocked by |
|-------|--------|--------|-------|-----------|
| (Phase 1 backlog will be drafted by PM after Gate 1) | — | ⏳ pending Gate 1 | pm | Gate 1 |

## 📋 In-flight handoffs

(none)

## ✅ Done this phase
- DECISIONs 001–004 filed (decided)
- ADRs 001–005 drafted
- PM wiki ingest (project-overview, personas, 6 module pages)
- Persona-builder YAML template + extraction-schema JSON template
- Incident extraction schema v1
- 5-question incident eval gold-set seeded
- Phase 0 Kickoff Brief filed
- Pending-decisions surface filed for all phases (PHASE-0 active; 1–4 preview/placeholder)
- Project bootstrapped from dev-agent-team v0.1.5

## 🚧 Blocked
- (none — all blockers are user-side external provisioning, tracked in pending-decisions)

## ⚠️ Risks / contradictions (from lint)
- **Repo layout note**: `init-project.sh` created `api/`, `server/`, `web/` stubs that don't apply to this framework. Architect to remove or repurpose during Phase 1 when `framework/` proper is created. Tracked, not blocking.
- **Subagent dispatch quirk**: Phase 0 deliverables were authored by TPM (acting on behalf of PM and Architect) because the symlinked persona subagents weren't loaded as dispatchable types in this session. Future sessions started with `claude` from the project dir will dispatch normally. Tracked.

---

## How to read this

- 🌅 **Morning briefing** → start here when you wake up
- 📋 **Phase Kickoff** → external dependencies you need to handle in parallel
- 🔴 **Gates** → user approval steps that unblock the next phase
- 🔴 **Decisions** → things only you can answer. Reply: "DECISION-NNN: option X"
- 🟡 **In-flight** → who's doing what right now
- 📋 **Handoffs** → cross-agent transitions in progress
- 🚧 **Blocked** → stories waiting on something
- ⚠️ **Risks** → TPM lint findings (stale wiki, code/spec drift, etc.)
