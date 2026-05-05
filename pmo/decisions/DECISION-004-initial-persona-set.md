---
id: DECISION-004
title: Initial persona-builder set for v1
status: decided
created: 2026-05-04
decided: 2026-05-04
owner: tpm
tags: [persona, scope, phase-0]
related: [persona-knowledge-builder]
---

# DECISION-004 — Initial persona-builder set for v1

## Context
The framework spec lists a full persona set: PM, TPM, Architect, Dev Manager, Dev, DevOps, Exec, Aira (spec §1). Each persona will eventually have its own Knowledge Builder agent (see [docs/wiki/persona-knowledge-builder.md](../../docs/wiki/persona-knowledge-builder.md)). Trying to ship all of them in v1 starves any single one of attention; we need a minimum set.

## Options considered
- **All 8 personas** — ambitious; high risk of shallow implementations everywhere.
- **PM + TPM + Aira's incident KB** — high-leverage; incident KB is already proven (spec §4.1); PM/TPM produce the most documentation surface; covers the open problem in §8.3.
- **Aira-only (incident KB)** — safe but doesn't exercise the per-persona pattern in v1.

## Decision
**PM + TPM + Aira's incident KB.**

Rationale:
- **Aira's incident KB** is a known-good slice. Lands the Phase 1 exit criterion (spec §12) with low risk.
- **PM and TPM** cover the largest documentation surface (Confluence) and force the framework to actually solve §8.3 (extraction schema for unstructured persona docs). If we don't tackle one PM/TPM-shaped persona in v1, we don't validate the persona-builder contract.
- Architect/Dev Manager/Dev/DevOps/Exec are deferred to v2. Their Knowledge Builders will follow the same contract — PM/TPM proving it out de-risks them.

## Implications
- Phase 1 ships: incident-KB ingestion + retrieval (Aira's existing path).
- Phase 3 ships: PM and TPM Knowledge Builder configs + the schema-template + `validate` / `dry-run` / `eval` CLI per persona-knowledge-builder.md.
- Phase 4+ adds the remaining personas one at a time, each with its own gold set.
- The persona-builder config contract (ADR-004) must be rich enough to support all 8 eventually, even if we only ship 3 in v1.

## Revisit conditions
- If a stakeholder agent (Architect, Dev Manager, etc.) needs a KB before Phase 4, file a new DECISION to advance their persona-builder.
