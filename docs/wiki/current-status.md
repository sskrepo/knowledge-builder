---
title: Current Status
source: derived from pmo/dashboard.md
compiled_at: 2026-05-11T00:00:00Z
created: 2026-05-04
owner: tpm
tags: [meta]
status: current
---

# Current Status

## Where we are
**Phase 1-3 + V3 deployment layer + laptop mode code complete. ADR-028 S1-S6 live. ADR-029 Phases 1+2 complete. ADR-030 design accepted (prompt externalization + harness); implementation blueprint ready for dispatch.** 160+ Python files, ~18K+ LOC, 630+ tests passing.

ADR-030 design accepted (2026-05-16): all 12 authorSkill prompts to be externalized to hot-reloadable versioned YAML (`framework/config/prompts/`), with a `PromptRegistry` loader and `prompt_lab.py` harness CLI. `persona_prompts.yaml` confirmed live and wired; it migrates to `persona_overlays.yaml` in the same change. `_FAILURE_CLASSIFIER_PROMPT` carries a checksum lock; the gate test remains the re-validation gate for any classifier text change. Blueprint: 4 parallel P-streams (P1=registry, P2=YAML, P3=harness, P4=docs) + 4-step serial cutover + G1 gate.

ADR-028 serial stream complete through S5:
- S1-S4: synthesisable field type, must_show_human, CLARIFY state (17th state), persona prompt injection.
- S5 (ADR-029 Phase 1): reference artifact retained in `_SessionData`; image-only artifacts hard-rejected at upload; `ArtifactComparator` runs at EVAL producing gap report (structure/density/missing sections); `exit_criteria.passed` is now diagnostic-only (DECISION-010 superseded); PROMOTE gate is user acceptance only; reject path has labeled S6 seam (S6 not started).

Folded fixes also shipped in S5: shared `_parse_llm_json_response` helper (BUG-573e3 parity between review.py + executor.py); PROMOTE KB-resolvability gate (BUG-queue-e685d).

Next: S6 (ADR-029 Phase 2 — classifier validation gate for reject routing). Pre-existing 9 test failures in `test_skill_builder_conversation.py` (6 ShimKb-patch + 2 promote behavior + 1 EVAL behavior) await a dedicated ShimKb-patch task. Last filed: ADR-027, ADR-028, ADR-029, DECISION-010 (superseded), DECISION-011 (resolved).

### What's runnable today
```bash
# Interactive skill-builder (workshop interface)
python -m framework.cli.kb_cli skill-builder --persona tpm

# Run any of the 3 workflow skills
python -m framework.cli.kb_cli workflow-run ops_eng.incident_summary --inputs '{"incident_id": "INC-EXAMPLE-001"}'
python -m framework.cli.kb_cli workflow-run pm.release_brief --inputs '{"release_id": "25.01"}'
python -m framework.cli.kb_cli workflow-run tpm.weekly_exec_review --inputs '{"project": "all"}'

# List all skills, build code wiki, promote with link validation
python -m framework.cli.kb_cli skill-list
python -m framework.cli.kb_cli code-wiki-build
python -m framework.cli.kb_cli promote framework/persona_builders/ops-eng.yaml --validate-links
```

## What's built

| Phase | Scope | Status |
|-------|-------|--------|
| **0 — Setup** | ADRs 001-018, PDD V2, config plane, persona starters | Done |
| **1 — Skeleton + incident KB** | Adapters, parsers, stores, retrievers, orchestrator, eval, MCP server, CLI | Code complete (stub mode) |
| **2 — Fleet + code wiki + skill-builder** | Fleet adapter, code wiki, 4 MCP tools, skill-builder Phase A (module split per ADR-015), provides_fields backfill | Code complete |
| **3 — Workflow runtime + orchestrator** | conversation.py (15-state machine), WorkflowMCPTool, 4-tier routing, Tier 3 fanout, cost telemetry, validate_workflow_links, 3 workflow skills, WikiMetadataStore, Confluence ingestion | Code complete |
| **V3 — Deployment interaction layer** | PDD V3 design + implementation: 2 MCP tools (askKnowledgeBase, authorSkill), REST routes (ask + 5 authorSkill + 3 ops), bearer auth middleware, session persistence (filestore + ADB), camelCase serialization, cost store, OpenAPI 3.1 spec, OCI deployment runbook | Code complete (468 tests, 0 failures) |
| **Laptop mode** | Bastion auto-reconnect for ADB (ADR-019), Codex CLI MCP transport for Confluence/Jira (ADR-020 — codex_cli for local stdio + codex_proxy for org HTTPS+OAuth MCP servers), split-deployment: authorSkill local + consumption remote, shared ADB | Code complete (140 new tests; E2E smoke passed against real central_confluence) |
| **Eval tooling** | GoldSetFeeder interactive CLI (`kb-cli gold-feed`), 7-state machine, count_entries() utility, gold_sets/ directory, 63 tests | Code complete |

## Phase 2+3 gap closure (2026-05-12)

All 9 gaps from the architect's audit are resolved. Key retrieval paths that were silently broken are now functional:

| Gap | File(s) | Fix |
|-----|---------|-----|
| GAP-I1 | `ingestion/confluence_wiki_ingest.py` | Ingestor now calls `WikiMetadataStore.upsert_page()` after every new/updated page |
| GAP-R1 | `retrievers/search_wiki.py` | Full implementation replacing `NotImplementedError` |
| GAP-R2 | `retrievers/read_wiki_page.py` | Full implementation replacing `NotImplementedError` |
| GAP-M1 | `deploy/mcp_server.py` | PmSkill + TpmSkill wired into skills dict |
| GAP-M2 | `deploy/mcp_server.py` | ShimWorkflows passed to ContextBuilder |
| GAP-M3 | `deploy/mcp_server.py` | cost_store passed to ContextBuilder |
| GAP-M4 | `deploy/mcp_server.py` | Workflow tool registry stores callables not dicts |
| GAP-D1 | `persona_skills/_base.py` | All tool dispatch branches implemented |
| GAP-H1 | `deploy/routes/ask.py`, `deploy/mcp_tools.py` | persona/serviceId/functionalArea hints forwarded |
| GAP-C1 | `stores/incident_vector_store.py` | Real HTTPS citation URLs when base URLs configured |

## Bug fix — OCI instance_principal on laptop (2026-05-12)

`mcp_server.py` lifespan now calls `_load_env_llm_overrides(repo_root, kbf_env)` unconditionally (replaces the `if kbf_env == "laptop": _load_laptop_llm_overrides()` guard). `ingestion_worker.py` also applies the same env-LLM overrides before constructing `LLMClient()`. `adapters/llm.yaml`'s `auth: instance_principal` default is only used when the env config YAML has no `[llm]` section. Regression-tested in `test_llm_factory.py` (4 new tests).

## What gates integration testing

| Item | Status |
|---|---|
| Oracle 23ai ADB | ✅ **CONNECTED + AUTO-RECONNECT** — `aira_genai_agent_db_Sravan` (Oracle AI DB 26ai, EU Frankfurt). Tunnel via `./framework/scripts/adb-connect.sh`. Config in `framework/config/laptop.yaml` (gitignored). mcp_server.py auto-creates pool on `KBF_ENV=laptop`; RetryWrapper calls adb-connect.sh on any connectivity error and retries transparently. |
| OCI Vault + secrets | ⏳ Pending — using env vars in laptop mode for now |
| OCI GenAI inference | ✅ **LIVE** — Oracle GPT-5 (eu-frankfurt-1). `LLMClient(auth=config_file, config_profile=adpcpprod)` confirmed working. Config: `framework/config/adapters/llm.yaml` (endpoint + model OCID) + `laptop.yaml [llm]` override. SecurityTokenSigner auto-selected from `~/.oci/config [adpcpprod]`. |
| Confluence API token | ✅ Via `codex_proxy` mode (Codex OAuth) |
| Jira API token | ✅ Via `codex_proxy` mode (Codex OAuth) |
| AIRA team's 50 query/citation pairs | ⏳ Pending |

## Recent decisions
- **DECISION-001–004** (2026-05-04) — all decided (Oracle stack, converged DB, OpenAI, PM/TPM/Aira personas)
- **ADR-019** (2026-05-11) — Bastion auto-reconnect for Oracle ADB in laptop mode
- **ADR-020** (2026-05-11, amended 2026-05-11) — Codex CLI as MCP transport for laptop mode. Original: Option B (codex_cli, direct stdio subprocess). Amendment: discovered org MCP servers are HTTPS+OAuth, added codex_proxy mode (LLM-mediated via `codex mcp-server`). E2E smoke passed.
- **DECISION-009** (2026-05-12) — Dedicated bug DB connection: `bug_db` config section, `KBF_BUGS` Oracle user, same ADB for now. Future DB separation is config-only.
- **ADR-024** (2026-05-12) — Bug DB connection design: `_init_bug_pool` inheritance contract, `setup-bug-user` CLI, migration-007 GRANTs, non-fatal startup policy for bug_pool, `export-bugs` CLI update.
- **ADR-027** (2026-05-14) — Design-first authorSkill: 16-state machine, source inspection before schema design, integrated DESIGN_SKILL call, real EVAL with auto-generated gold sets.
- **DECISION-010** (2026-05-14) — EVAL gold sets: Option A chosen — auto-generate from live samples, kind=auto_generated, gate on recall@k + faithfulness thresholds.
- No open decisions

## Next milestones
- Schedule persona authoring workshops (workshop guide ready at `pmo/workshops/persona-authoring-workshop.md`)
- Provide provisioning when ready for integration testing
- AIRA team exports 50 query/citation pairs for gold set
