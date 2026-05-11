---
title: Current Status
source: derived from pmo/dashboard.md
compiled_at: 2026-05-10T00:00:00Z
created: 2026-05-04
owner: tpm
tags: [meta]
status: current
---

# Current Status

## Where we are
**Phase 1-3 code complete (filestore/stub mode).** 176 Python files, 22 framework modules. All code runs against filestore + stub LLM — no external provisioning needed for development or workshops.

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
| **3 — Workflow runtime + orchestrator** | conversation.py, WorkflowMCPTool, 4-tier routing, Tier 3 fanout, cost telemetry, validate_workflow_links, 3 workflow skills, WikiMetadataStore, Confluence ingestion | Code complete |
| **Eval tooling** | GoldSetFeeder interactive CLI (`kb-cli gold-feed`), 7-state machine, count_entries() utility, gold_sets/ directory, 63 tests | Code complete |

## What gates integration testing

External provisioning (ask once, when ready):
1. Oracle 23ai ADB (dev instance)
2. OCI Vault + secrets
3. OpenAI API key or OCI GenAI URL
4. Confluence API token + space keys
5. Jira API token + project keys
6. AIRA team's 50 query/citation pairs (for eval gold set)

## Recent decisions
- **DECISION-001–004** (2026-05-04) — all decided (Oracle stack, converged DB, OpenAI, PM/TPM/Aira personas)
- No open decisions

## Next milestones
- Schedule persona authoring workshops (workshop guide ready at `pmo/workshops/persona-authoring-workshop.md`)
- Provide provisioning when ready for integration testing
- AIRA team exports 50 query/citation pairs for gold set
