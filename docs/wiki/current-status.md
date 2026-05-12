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
**Phase 1-3 + V3 deployment layer + laptop mode code complete.** 160+ Python files, ~16K LOC, 574 tests passing. All code runs against filestore + stub LLM — no external provisioning needed. PDD V3 external API surface fully implemented. Laptop mode: bastion auto-reconnect for ADB (ADR-019), Codex CLI MCP transport for Confluence/Jira (ADR-020).

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

## What gates integration testing

| Item | Status |
|---|---|
| Oracle 23ai ADB | ✅ **CONNECTED** — `aira_genai_agent_db_Sravan` (Oracle AI DB 26ai, EU Frankfurt). Tunnel via `./framework/scripts/adb-connect.sh`. Config in `framework/config/laptop.yaml` (gitignored). |
| OCI Vault + secrets | ⏳ Pending — using env vars in laptop mode for now |
| OpenAI API key or OCI GenAI URL | ⏳ Pending |
| Confluence API token | ✅ Via `codex_proxy` mode (Codex OAuth) |
| Jira API token | ✅ Via `codex_proxy` mode (Codex OAuth) |
| AIRA team's 50 query/citation pairs | ⏳ Pending |

## Recent decisions
- **DECISION-001–004** (2026-05-04) — all decided (Oracle stack, converged DB, OpenAI, PM/TPM/Aira personas)
- **ADR-019** (2026-05-11) — Bastion auto-reconnect for Oracle ADB in laptop mode
- **ADR-020** (2026-05-11, amended 2026-05-11) — Codex CLI as MCP transport for laptop mode. Original: Option B (codex_cli, direct stdio subprocess). Amendment: discovered org MCP servers are HTTPS+OAuth, added codex_proxy mode (LLM-mediated via `codex mcp-server`). E2E smoke passed.
- No open decisions

## Next milestones
- Schedule persona authoring workshops (workshop guide ready at `pmo/workshops/persona-authoring-workshop.md`)
- Provide provisioning when ready for integration testing
- AIRA team exports 50 query/citation pairs for gold set
