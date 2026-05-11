---
title: Current Status
source: derived from pmo/dashboard.md
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: tpm
tags: [meta]
status: current
---

# Current Status

## Where we are
**Phase 2 Track A+B — Fleet adapter + Code Wiki — implementation complete (filestore mode).** All 4 new MCP retrieval tools are implemented and registered. Fleet adapter reads from fixture files, code wiki builder produces a structural AST index, and both sets of tools carry citations on every result.

Prior: Phase 0 full design pass produced ADRs 006–011, architect wiki pages, full configuration plane, Adapter Protocol + dual-mode adapter stubs, Option-3 persona starter pack. Phase 1 produced the full code (adapters, parsers, IncidentVectorStore, orchestrator, eval harness, MCP server, kb-cli). PDD published as .docx; exec brief as .pptx. ADR-014 (OCI GenAI LLM), ADR-015–018 (skill-by-demonstration, workflow skills, extraction workflow linking, skill suggestion loop) + V2 PDD + phases V2 authored. Skill builder, workflow runtime, renderers, deliverers all implemented for Phase 2 V2.

Two things gate Phase 1:
1. **Your Gate-1 review** of ADRs + PM ingest (see dashboard)
2. **External provisioning** — 3 blocking items in [pending-decisions/PHASE-0.md](../../pmo/pending-decisions/PHASE-0.md): Oracle 23ai Autonomous DB, OpenAI API key, OCI Vault

## Active stories
(none yet — Phase 1 backlog drafts after Gate 1)

## Awaiting user decision
- 🔴 **Gate 1 — Phase 0**: ADRs 001–005 + PM ingest. Reply `GATE-1-PHASE-0: approved` (or per-artifact: `ADR-001: approved`).
- 🚨 **External provisioning** (3 blocking items): see [pending-decisions/PHASE-0.md](../../pmo/pending-decisions/PHASE-0.md).

## Recent decisions
- **DECISION-001** (2026-05-04) — Oracle commitment level: full Oracle stack with OpenAI + LangGraph carve-outs.
- **DECISION-002** (2026-05-04) — Converged Autonomous DB; logical-polyglot (each data type owns its access pattern + schema), physical-converged (one DB instance).
- **DECISION-003** (2026-05-04) — OpenAI for LLM and embeddings (Oracle-certified). `gpt-4o` + `text-embedding-3-large` (3072 dims).
- **DECISION-004** (2026-05-04) — v1 personas: PM + TPM + Aira's incident KB.

## Next milestones
- You: Gate-1 review (ADRs + PM ingest) and external provisioning kickoff.
- After Gate 1: PM drafts Phase 1 backlog (stories) and Architect breaks ground on `framework/core/`.
- After external provisioning: Phase 1 begins; first eval pass against real Confluence/Jira data.

## Open problems flagged (research, not implementation — spec §8)
- §8.1 — LLM wiki storage for remote agents (default: git + cached MCP + TOC-on-demand). DECISION-006 at Phase 3 kickoff.
- §8.2 — Code accessibility for remote agents (default: hybrid VM-spinup for write paths + central code wiki for reads). DECISION-005 at Phase 2 kickoff.
- §8.3 — Per-persona extraction schemas. **Resolved by ADR-004**: framework provides the contract; persona teams own the schemas.

## Lint notes (TPM)
- `init-project.sh` left `api/`, `server/`, `web/` stubs that don't apply. To be cleaned up at Phase 1 entry when `framework/` proper is created.
- Phase 0 deliverables authored by TPM acting on behalf of PM and Architect (the symlinked subagents weren't loaded as dispatchable types in this session). Future sessions started with `claude` from this dir will dispatch normally.
