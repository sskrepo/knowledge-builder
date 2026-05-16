# Knowledgebase — Wiki Index

## Meta
- [current-status](current-status.md)
- [log](log.md)

## Framework — start here
- 📄 **[PDD V3 — Knowledge Builder Framework (Deployment Interaction Layer)](pdd/PDD-Knowledge-Builder-Framework-v3.md)** — current; REST API surface, MCP tool catalog, two interaction models (Consumption + Knowledge Builder), OCI deployment topology
- 📋 **[OpenAPI 3.1 Spec](../../framework/deploy/openapi.yaml)** — authoritative REST contract; all field names camelCase; source of truth for client SDK generation. Covers: `POST /api/v1/ask`, `POST/GET/DELETE /api/v1/kb/authorSkill[/{synthId}]`, `/healthz`, `/api/v1/metrics/cost`, `/api/v1/version`
- 📄 **[PDD V2 — Knowledge Builder Framework (Internal Architecture)](pdd/PDD-Knowledge-Builder-Framework-v2.md)** — internal architecture reference; two-flow model, three-shim arch, four-tier routing, workflow skills, skill-by-demonstration. Read alongside V3.
- 📄 [PDD V1 — Knowledge Builder Framework](pdd/PDD-Knowledge-Builder-Framework.md) — superseded by V2 (also as .docx in same folder)
- 📊 **[Executive Brief](exec-brief.md)** — 14-section deck for leadership (also as .pptx in same folder)
- [project-overview](project-overview.md) — vision, personas, value loop, v1 scope
- [personas](personas.md) — knowledge producers (PM/TPM/...) and use-case agents (Aira/portals)
- [persona-knowledge-builder](persona-knowledge-builder.md) — per-persona builder agent contract
- [architecture](architecture.md) — system shape, 5-layer model, module map
- [data-model](data-model.md) — ContentItem / Chunk / Edge + multi-axis fields
- [api-design](api-design.md) — MCP retrieval tool surface
- 🔍 **[AIRA — Comparison & Analysis](aira-comparison.md)** — extraction + retrieval comparison vs the framework; what to borrow, what to avoid, the migration story
- 🛠️ **[Agentic Code Access — Read & Write Story](code-access-story.md)** — how agents (Aira, future coding assistants) read & modify code; spec §8.2 hybrid path; DECISION-005 framing
- 📋 **Persona onboarding workbooks** in [`onboarding/`](onboarding/) — for sharing with persona team leads in parallel with engineering:
  - [PM & TPM](onboarding/pm-tpm.md) — extraction schemas, sources, gold sets
  - [Ops Engineer / AIRA](onboarding/ops-eng.md) — Phase-1-exit-gate persona; includes AIRA migration story

## Modules (one per data type, spec §4)
- [module-incidents](module-incidents.md) — operational incidents (Phase 1, proven path)
- [module-fleet](module-fleet.md) — UDAP/Sentinel read-through (Phase 2)
- [module-code](module-code.md) — Som-style structural code wiki (Phase 2)
- [module-pm-tpm-wiki](module-pm-tpm-wiki.md) — Confluence-driven PM/TPM wikis (Phase 3)
- [module-fa-graph](module-fa-graph.md) — FA semantic property graph (Phase 4)
- [module-jira-roadmap](module-jira-roadmap.md) — open / v2 candidate

## Architecture Decision Records (Architect)
- [ADR-001 — Tech-stack baseline](adr/ADR-001-tech-stack-baseline.md)
- [ADR-002 — Storage shape per data type](adr/ADR-002-storage-shape.md)
- [ADR-003 — Core interfaces (§6)](adr/ADR-003-core-interfaces.md)
- [ADR-004 — Persona-builder config schema (v2 amended)](adr/ADR-004-persona-builder-config.md)
- [ADR-005 — Eval harness (amended for AIRA gold-set + recency)](adr/ADR-005-eval-harness.md)
- [ADR-006 — Two-shim layered architecture](adr/ADR-006-two-shim-architecture.md)
- [ADR-007 — Persona context skill contract (amended for char cap + structured synthesis)](adr/ADR-007-persona-context-skill.md)
- [ADR-008 — Functional-area + resources dimensions](adr/ADR-008-functional-area-and-resources.md)
- [ADR-009 — Resource ontology](adr/ADR-009-resource-ontology.md)
- [ADR-010 — Configuration plane](adr/ADR-010-configuration-plane.md)
- [ADR-011 — Dual-mode source adapters (REST + MCP)](adr/ADR-011-dual-mode-source-adapters.md)
- [ADR-012 — In-DB embedding via DBMS_VECTOR](adr/ADR-012-in-db-embedding.md)
- [ADR-013 — Filter strictness contract (hard / soft-with-multiplier)](adr/ADR-013-filter-strictness.md)
- [ADR-014 — LLM access via OCI Generative AI Inference (AIRA pattern)](adr/ADR-014-llm-via-oci-genai.md)
- [ADR-015 — Skill-by-demonstration (skill_builder module)](adr/ADR-015-skill-by-demonstration.md)
- [ADR-016 — Workflow skills (renderers + deliverers)](adr/ADR-016-workflow-skills.md)
- [ADR-017 — Extraction workflow linking](adr/ADR-017-extraction-workflow-linking.md)
- [ADR-018 — Skill suggestion loop](adr/ADR-018-skill-suggestion-loop.md)
- [ADR-019 — Bastion auto-reconnect for Oracle ADB in laptop mode](adr/ADR-019-bastion-auto-reconnect.md)
- [ADR-020 — Codex CLI as MCP transport for laptop mode](adr/ADR-020-codex-cli-mcp-transport.md)
- [ADR-021 — Artifact upload for remote authorSkill sessions](adr/ADR-021-artifact-upload-oci.md)
- [ADR-023 — KBF-ops persona + reviewSkillSession quality review](adr/ADR-023-kbf-ops-persona-session-quality-review.md)
- [ADR-024 — Dedicated bug DB connection](adr/ADR-024-bug-db-connection.md)
- [ADR-025 — Vector index INMEMORY rebuild on first production deploy ⚠️ prod gate](adr/ADR-025-vector-index-inmemory-prod-rebuild.md)
- [ADR-026 — Source-grounded schema review + layout-aware PPTX rendering](adr/ADR-026-source-grounded-schema-review-and-layout-aware-pptx.md)
- [ADR-027 — Design-first authorSkill — 16-state machine](adr/ADR-027-design-first-authorskill.md)
- [ADR-028 — authorSkill: prompt investment, human-loop enforcement, and conversational clarification](adr/ADR-028-authorskill-prompt-investment-human-loop-conversation.md) — **ACCEPTED 2026-05-15 — Item1=A (persona playbook), Item2=A (must_show_human), Item3=A (CLARIFY state), Item4=A (synthesisable)**
- [ADR-029 — Outcome-based EVAL: demonstration-artifact acceptance loop](adr/ADR-029-outcome-based-eval-acceptance-loop.md) — **ACCEPTED 2026-05-15 — Option A + text-only comparator + image-only hard-reject; supersedes DECISION-010 terminal gate**
- [ADR-028 + ADR-029 — Implementation Blueprint](adr/ADR-028-029-impl-plan.md) — **ACTIVE — file-partitioned, dependency-ordered work breakdown for 3-stream parallel dev team**
- [ADR-030 — authorSkill: Externalize LLM prompts to hot-reloadable versioned YAML + prompt-test harness](adr/ADR-030-prompt-externalization-and-harness.md) — **ACCEPTED 2026-05-16 — YAML store layout, PromptRegistry loader contract, gate-lock enforcement, prompt_lab harness design, persona_prompts.yaml folded in**
- [ADR-030 — Implementation Blueprint](adr/ADR-030-impl-plan.md) — **ACTIVE — file-partitioned, serial-aware work breakdown: 4 parallel P-streams + 4-step serial cutover + gate task**
- [ADR-031 — No Arbitrary Content Caps](adr/ADR-031-no-arbitrary-content-caps.md) — **ACCEPTED 2026-05-16 — synthesized schemas carry no arbitrary maxLength; source text sized to model context; all LLM-JSON parses detect truncation; last hard-coded prompt migrated to PromptRegistry**
- [ADR-032 — Ask-time / Runtime Source Ingestion](adr/ADR-032-ask-time-source-ingestion.md) — **ACCEPTED 2026-05-16 — DECISION-012 resolved: Option C (ephemeral request-scoped ingestion); P3 shipped commit 8c947dc; P1+P2 blueprint ready for dispatch**
- [ADR-032 — Implementation Blueprint](adr/ADR-032-impl-plan.md) — **ACTIVE — file-partitioned, serial-aware work breakdown: 3-stream parallel Phase 1 + 2-stream Phase 2 + sequential Phase 3/4**

## Skill builder
- **[authorSkill flow — state-by-state LLM usage map](authorskill-flow.md)** — NEW 16-state machine (post-ADR-027): design-first, source inspection before schema design, real EVAL with auto-generated gold sets
- **[authorSkill prompts — full prompt dump](authorskill-prompts.md)** — living reference: every LLM prompt in the authorSkill flow with format kwargs and persona-awareness audit (ADR-028 Item 1)
- [authorSkill flow — pre-ADR-027 (archived)](authorskill-flow-pre-adr-027.md) — 15-state machine; preserved for reference and in-flight session support

## Engineering
- 🚀 **[laptop-quickstart](engineering/laptop-quickstart.md)** — V2; run framework end-to-end on laptop with no provisioning (no ADB, no Vault, no OpenAI required)
- 🎯 **[workshop-ops-guide](engineering/workshop-ops-guide.md)** — how to run the application + stand up the NL gold-set feeder for persona workshops
- [dev-guide](engineering/dev-guide.md) — Phase 1 setup + first end-to-end run
- [runbook](engineering/runbook.md) — operations playbook
- 🖥️ **[oci-deployment-runbook](engineering/oci-deployment-runbook.md)** — complete step-by-step guide: empty OCI tenancy → live framework on a Compute VM (VCN, ADB, Vault, GenAI, Nginx, systemd, MCP client config)

## Persona starter packs (in `framework/persona_builders/`)
All 8 producer personas have starter configs + extraction schemas + gold sets — labeled `status: draft`. Persona teams refine and promote to `status: production` per ADR-004.

| Persona | Config | Gold set |
|---|---|---|
| PM | [pm.yaml](../../framework/persona_builders/pm.yaml) | [pm.jsonl](../../eval/gold_sets/pm.jsonl) |
| TPM | [tpm.yaml](../../framework/persona_builders/tpm.yaml) | [tpm.jsonl](../../eval/gold_sets/tpm.jsonl) |
| Architect | [architect.yaml](../../framework/persona_builders/architect.yaml) | [architect.jsonl](../../eval/gold_sets/architect.jsonl) |
| Eng Manager | [eng-mgr.yaml](../../framework/persona_builders/eng-mgr.yaml) | [eng-mgr.jsonl](../../eval/gold_sets/eng-mgr.jsonl) |
| Developer | [developer.yaml](../../framework/persona_builders/developer.yaml) | [developer.jsonl](../../eval/gold_sets/developer.jsonl) |
| Ops Manager | [ops-mgr.yaml](../../framework/persona_builders/ops-mgr.yaml) | [ops-mgr.jsonl](../../eval/gold_sets/ops-mgr.jsonl) |
| Ops Engineer | [ops-eng.yaml](../../framework/persona_builders/ops-eng.yaml) | [ops-eng.jsonl](../../eval/gold_sets/ops-eng.jsonl) |
| Service Owner | [service-owner.yaml](../../framework/persona_builders/service-owner.yaml) | [service-owner.jsonl](../../eval/gold_sets/service-owner.jsonl) |

## Configuration plane (in `framework/config/`)
- [_schema.json](../../framework/config/_schema.json) — JSON-Schema validating env files
- [dev.yaml](../../framework/config/dev.yaml) / [staging.yaml](../../framework/config/staging.yaml) / [prod.yaml](../../framework/config/prod.yaml) — env configs
- adapters: [confluence.yaml](../../framework/config/adapters/confluence.yaml) (dual-mode) · [jira.yaml](../../framework/config/adapters/jira.yaml) (dual-mode) · [git](../../framework/config/adapters/git.yaml) · [udap](../../framework/config/adapters/udap.yaml) · [openai](../../framework/config/adapters/openai.yaml)
- [shim_faaas.yaml](../../framework/config/shim_faaas.yaml) — FAaaS domain ontology
- [bootstrap-vault.sh](../../framework/scripts/bootstrap-vault.sh) — Vault setup walker
- [check-config.py](../../framework/scripts/check-config.py) — pre-deploy validation

## Compiled by Dev Manager
- `engineering/` — conventions, eval harness wiring, cost telemetry (Phase 0 → Phase 1)

## Raw sources (immutable, in `docs/raw/`)
- `knowledge-builder-framework-spec.md` — the framework spec (§1–§14)
- `Meeting Notes - LLM WIki vs KB Approaches.pdf` — design discussion notes
