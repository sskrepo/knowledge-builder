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

---

## Running on a laptop (no OCI Vault yet)

For local dev before/while OCI Vault is provisioned, the framework supports a **local secrets file** backend.

### One-time setup

```bash
# 1. Create ~/.kbf/secrets.yaml from the template
./framework/scripts/setup-local-dev.sh

# 2. Add to your shell profile (~/.zshrc / ~/.bashrc)
export KBF_ENV=dev
export KBF_SECRETS_BACKEND=local                 # use local file (not OCI Vault)
export KBF_SECRETS_FILE=$HOME/.kbf/secrets.yaml

# 3. Pick LLM provider for laptop:
#    Easiest: direct OpenAI (just need an OpenAI key)
export KBF_LLM_PROVIDER=openai_direct

#    Alternative: OCI GenAI (requires OCI auth via config_file)
# export KBF_LLM_PROVIDER=oci_genai
# export OCI_AUTH_METHOD=config_file
# export OCI_CONFIG_PROFILE=DEFAULT

# 4. Edit secrets (mode 0600, never committed)
$EDITOR ~/.kbf/secrets.yaml
```

### Required secrets for laptop run

Minimum to ingest from Jira and answer one query:

| Slug | What it is | When you need it |
|---|---|---|
| `adb-admin-dev` | Autonomous DB admin password | Once your ADB is provisioned |
| `kb-incidents-rw-dev` | Schema RW password | Same |
| `openai-api-key` | OpenAI API key | If `KBF_LLM_PROVIDER=openai_direct` |
| `jira-readonly` | Jira personal access token | Always |
| `confluence-readonly` | Confluence PAT | Optional (only if pulling design-doc supplements) |

The remaining slugs in the template can be empty for Phase 1 laptop work.

### Verify

```bash
python3 framework/scripts/check-config.py --env dev
# → "all green" or lists what's missing
```

### Notes

- `~/.kbf/secrets.yaml` is created with `chmod 600` and lives outside the repo. The repo's `.gitignore` also blocks `.secrets.local.yaml`, `.kbf/`, and `secrets/` to prevent accidental commits.
- The same `vault://kb/<slug>` references in `framework/config/dev.yaml` and the adapter configs work unchanged — only the resolution backend differs.
- When you migrate to OCI Vault later: set `KBF_SECRETS_BACKEND=vault` and run `framework/scripts/bootstrap-vault.sh` to populate the same slug names. No code or config changes.

### Going from laptop dev → real Vault (one-line switch)

```bash
unset KBF_SECRETS_BACKEND       # back to default (vault)
./framework/scripts/bootstrap-vault.sh dev    # walks you through populating each slug
```

The framework rereads creds via `VaultClient` on next process start. Same code, same configs.

