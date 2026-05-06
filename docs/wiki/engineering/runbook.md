---
title: Knowledge Builder Framework — Operations Runbook
created: 2026-05-06
owner: dev-manager
tags: [engineering, runbook, ops]
status: current
---

# Operations Runbook

## Daily checks
- `/healthz` returns 200 + all retrievers in registry
- Last successful ingest within 10 min of expected schedule
- Daily cost report (kb_shim.cost_log) within budget alerts

## Common issues

### MCP tool returning empty results
1. Check `/healthz` — is the store registered? Are credentials valid?
2. Run `SELECT COUNT(*) FROM kb_incidents.chunks WHERE embedding IS NULL` — non-zero means embedding proc has lag.
3. Manually run `EXEC batch_insert_datasets_vectors_kbi;` to force-fill embeddings.

### Webhook deliveries are missing
1. Check `webhook_router` logs for HMAC verification failures.
2. Confirm webhook secret in OCI Vault matches what's configured at the source side.
3. Replay missing items via `change_detection.poll_since(<timestamp>)`.

### Eval CI failing
1. Look at `eval/runs/PR-N.md` for which questions regressed.
2. Was the parser schema changed? Confirm extraction is still complete.
3. Is the embedding model unchanged? `text-embedding-3-large` is pinned.
4. If a known regression is acceptable (e.g., schema change), update baseline in a separate PR with justification.

### Ingestion cost spike
1. Check `kb_shim.cost_log` for tokens-per-day per persona.
2. Identify the persona builder and time window.
3. If a backfill is in flight, expected — confirm completion and re-evaluate.
4. If steady-state spike, examine schema for over-extraction or rerun-loop bugs.

## Backup / restore
- Autonomous DB has Data Guard + automated backups (Oracle-managed).
- Wiki content lives in git — restore via `git clone` from the canonical repo.
- Eval baselines live in OCI Object Storage (per `framework/config/{env}.yaml::eval.baseline_storage`).

## Emergency rollback
- Persona builder produces bad data → set `status: draft` in YAML and reingest with previous schema version.
- Embedding model swap regression → revert `schema_version` in builder + reingest. Old rows retained for diff.

## On-call escalation
- L1: Dev Manager (engineering execution)
- L2: Architect (interface / design issues)
- L3: TPM (cross-team coordination)
- Aira-customer-impact: Ops Manager
