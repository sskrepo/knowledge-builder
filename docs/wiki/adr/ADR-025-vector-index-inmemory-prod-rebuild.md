---
title: ADR-025 — Vector index INMEMORY NEIGHBOR GRAPH is mandatory for HNSW; production In-Memory pool verification
status: accepted
created: 2026-05-13
amended: 2026-05-13 (correcting earlier disk-resident premise)
owner: architect
deciders: user, tpm
supersedes: ~
tags: [arch, data, ops, prod-gate]
---

## Context

`framework/stores/sql/kb_incidents.sql` creates the HNSW vector index over the
`chunks.embedding` column.

**Correction of earlier draft (2026-05-13):** The first version of this ADR
claimed Oracle 23ai supported a "disk-resident" HNSW form created by omitting
the `ORGANIZATION` clause. That was wrong — Oracle 23ai raises **ORA-51914
"Missing ORGANIZATION clause when creating a vector index"** on any vector
index DDL that omits it. For HNSW the only supported organisation is
`ORGANIZATION INMEMORY NEIGHBOR GRAPH`; for IVF it is
`ORGANIZATION NEIGHBOR PARTITIONS`. There is no third option.

The earlier ~30-60 s "migration hang" was misdiagnosed. Two things were
happening together; only the first was actually slow:

| Cause | Cost | Status |
|---|---|---|
| `LLMClient()` constructed in `cmd_migrate` → OCI GenAI client init + circuit breaker | 30-60 s | **Fixed** (commit `2e2115e`): `cmd_migrate` now passes `llm=None`. Migration only runs DDL. |
| HNSW INMEMORY index DDL on **empty** `chunks` table | ~3-8 s | Normal; cannot be avoided without changing index type. |

In other words, the slow part was OCI GenAI client startup, not the index DDL.
Removing `ORGANIZATION INMEMORY NEIGHBOR GRAPH` was unnecessary and produced
invalid SQL — fixed in commit (this change).

## Decision

1. **`kb_incidents.sql` always specifies `ORGANIZATION INMEMORY NEIGHBOR GRAPH`** —
   it is non-optional for HNSW. The migration creates the index in this form
   on every environment (laptop, staging, production). The DDL is cheap on
   empty data.

2. **Production gate: verify ADB In-Memory option is provisioned BEFORE first
   deploy.** Run:

   ```sql
   SELECT value FROM v$option WHERE parameter = 'In-Memory Column Store';
   -- Expected: 'TRUE'
   ```

   If `FALSE` or missing, the migration will succeed but query performance will
   degrade once data volume grows (the graph cannot be loaded into SGA). The
   prod-rollout checklist must include this verification.

3. **After first ingestion completes (chunks table non-empty), rebuild to
   refresh the in-memory graph with the loaded vectors:**

   ```sql
   -- Run as ADMIN (or KB_INCIDENTS owner) after first ingestion completes.
   ALTER INDEX KB_INCIDENTS.ix_chunks_embedding_hnsw
     REBUILD ORGANIZATION INMEMORY NEIGHBOR GRAPH;
   ```

   This is a **production-gate requirement** in `pmo/dashboard.md`.

4. **If the ADB tier does not include the In-Memory option**, the only
   alternative is switching the index type to IVF
   (`ORGANIZATION NEIGHBOR PARTITIONS`). That is a separate ADR with
   different recall/latency trade-offs and is not pursued here unless we hit
   a tenancy that lacks In-Memory.

## Consequences

- `kbf-start.sh --migrate` total runtime: a few seconds for DDL plus the
  one-time ADB user/table creation on first migrate (~30 s, not avoidable).
  The previously-reported 30-60 s hang is gone because `LLMClient()` is no
  longer constructed during migration.
- Production vector search relies on the In-Memory pool. Skipping the
  verification step or running on a tenancy without In-Memory will silently
  degrade query latency under load.
- Removing the `ORGANIZATION` clause is **never safe** — it produces invalid
  DDL (ORA-51914).

## Prod-rollout checklist entry

Add to `pmo/dashboard.md` prod-rollout section:

```
- [ ] ADR-025 (gate 1): verify ADB In-Memory option is enabled
      SQL: SELECT value FROM v$option WHERE parameter = 'In-Memory Column Store';
      Expected: 'TRUE'. If FALSE, escalate before migration runs.

- [ ] ADR-025 (gate 2): REBUILD ix_chunks_embedding_hnsw after first ingestion
      SQL: ALTER INDEX KB_INCIDENTS.ix_chunks_embedding_hnsw
             REBUILD ORGANIZATION INMEMORY NEIGHBOR GRAPH;
      Gate: chunks table non-empty + gate 1 passed
```

## References

- Oracle 23ai Vector Search Developer Guide — "Managing Vector Indexes"
- ORA-51914: https://docs.oracle.com/error-help/db/ora-51914/
- `framework/stores/sql/kb_incidents.sql` — DDL (HNSW + INMEMORY NEIGHBOR GRAPH)
- `framework/cli/kb_cli.py` — `cmd_migrate` (passes `llm=None`, no OCI init)
- `docs/wiki/engineering/oci-deployment-runbook.md` — § 5 (Database schema setup)
- Commits:
  - `2e2115e` — fix(migrate): remove OCI GenAI init (correct fix; kept)
  - (this commit) — fix(migrate): restore ORGANIZATION INMEMORY NEIGHBOR GRAPH
    (revert of the incorrect omission introduced in `2e2115e`)
