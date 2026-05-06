---
title: Phase 1 Developer Guide
created: 2026-05-06
owner: dev-manager
tags: [engineering, dev-guide, phase-1]
status: current
---

# Phase 1 Developer Guide

How to set up the framework locally and run the first end-to-end ingestion.

## Prerequisites
- Python 3.12+
- Oracle 23ai Autonomous Database (provisioned via OCI Console)
- OCI Vault with required secrets (run `framework/scripts/bootstrap-vault.sh`)
- OpenAI API key (Oracle-certified)

## Setup
```bash
# clone & install
cd /Users/sravansunkaranam/github/Knowledgebase
python -m venv .venv && source .venv/bin/activate
pip install -e framework/
pip install -r framework/requirements.txt

# environment
cp framework/.env.example framework/.env
# Edit framework/.env with your KBF_ENV, OCI auth method.

# fill in framework/config/dev.yaml with real OCIDs
$EDITOR framework/config/dev.yaml

# run vault setup
./framework/scripts/bootstrap-vault.sh dev

# verify
python framework/scripts/check-config.py --env dev
```

## First end-to-end run
```bash
# 1. Migrate the kb_incidents schema
python -m framework.cli.kb_cli migrate --schema kb_incidents --env dev

# 2. Validate the ops-eng persona builder
python -m framework.cli.kb_cli validate framework/persona_builders/ops-eng.yaml

# 3. Dry-run on a 5-issue Jira sample
python -m framework.cli.kb_cli ingest --dry-run --sample 5 framework/persona_builders/ops-eng.yaml

# 4. Promote to production
python -m framework.cli.kb_cli promote framework/persona_builders/ops-eng.yaml

# 5. Run eval harness
python -m framework.cli.kb_cli eval framework/persona_builders/ops-eng.yaml

# 6. Start MCP server
uvicorn framework.deploy.mcp_server:app --reload --port 8080
curl localhost:8080/healthz

# 7. Try a query
curl -X POST localhost:8080/answer \
  -H "Content-Type: application/json" \
  -d '{"query": "What incidents touched auth-service in the last 30 days?"}'
```

## Code layout (Phase 1 essentials)
- `framework/core/` — content model, IDs, URNs, LLM client, vault client
- `framework/adapters/` — Confluence/Jira native + MCP, Git, UDAP
- `framework/parsers/` — LLM parser (gpt-4o + JSON Schema), chunker
- `framework/stores/` — IncidentVectorStore + DDL
- `framework/retrievers/` — vector_search, get_incident_summary, list_sources
- `framework/orchestrator/` — shim_faaas, shim_kb, intent_classifier, context_builder, synthesizer
- `framework/persona_skills/` — ops_eng (Phase 1) + others (stubs for Phase 3+)
- `framework/ingestion/` — pipeline, change_detection, webhook_router
- `framework/eval/` — runner, metrics, reports
- `framework/deploy/` — FastAPI MCP server, ingestion worker
- `framework/cli/` — kb-cli
- `framework/persona_builders/` — YAML configs (8 personas; ops-eng is v1 priority)
- `framework/parsers/schemas/` — JSON-Schema extraction docs (per persona × KB)
- `framework/config/` — env yamls + adapter yamls + shim_faaas
- `eval/gold_sets/` — JSONL gold sets (incidents.jsonl is the Phase 1 exit gate)

## Daily dev workflow
1. Pull latest. Run `pytest framework/tests/`.
2. Make changes (always write tests).
3. Run `kb-cli validate framework/persona_builders/<persona>.yaml`.
4. If parser/store/retriever changed, run a small eval locally.
5. Commit + open PR. CI runs the eval gate.

## Code conventions
- Type hints required on all new code.
- `mypy --strict` clean on `framework/core/`.
- `ruff check + format` on the whole repo.
- Tests: pytest naming convention; one test file per module under `framework/tests/`.
- Docstrings on public classes; one-line on private methods.

## Ingestion model invariants (don't break)
- Every `ContentItem` has full metadata (per ADR-008 + spec §10): `persona_visibility`, `owner`, `classification`, `source_sha`, `parser_version`, `schema_version`. Failing this → `MissingMetadataError`.
- `id = sha256(source : source_id : schema_version)` for idempotency.
- Every retrieval `Result` has a non-empty `citation_url`.
- Cost telemetry fires on every LLM call.

## When things break
- "OCI vault unresolved" → run `bootstrap-vault.sh` again.
- "ORA-00955 name already used" during migrate → idempotent; safe to ignore.
- "vector dim mismatch" → embedding model changed; bump `schema_version` and reingest.
- "CI eval-gate regressed" → review `eval/runs/PR-N.md` diff vs baseline.

## Contact
- Architect: PR review on ADR drift
- Dev Manager: story sequencing, code review gatekeeper
- TPM: dashboard / dependency / phase-readiness
