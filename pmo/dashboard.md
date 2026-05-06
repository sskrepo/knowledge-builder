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

## 🌅 Morning briefing — 2026-05-05

**TL;DR.** Phase 0 documentation/config layer is **drafted in full**: 4 DECISIONs, 5 ADRs, 8 PM wiki pages, persona-builder + extraction-schema templates, an incident extraction schema (v1), a 5-question seed eval gold-set, the Phase-0 kickoff brief, the pending-decisions surface for all phases, an initial git commit, **and a consolidated PDD + executive brief** capturing all architectural decisions including knowledge_bases (rename of corpora), the 5-layer architecture, polyglot per persona, skills-default, functional-area + resources dimensions. **No production code was written** — Phase 0 is docs/configs only by design.

**📄 Two new documents to read** (start here):
- **[PDD — Knowledge Builder Framework](../docs/wiki/pdd/PDD-Knowledge-Builder-Framework.md)** — comprehensive product definition (all decisions consolidated, with mermaid diagrams)
- **[Executive Brief](../docs/wiki/exec-brief.md)** — 12-section summary for leadership; PPT-convertible

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
