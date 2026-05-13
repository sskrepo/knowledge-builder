---
title: ADR-025 — Vector Index INMEMORY NEIGHBOR GRAPH rebuild on first production deploy
status: accepted
created: 2026-05-13
owner: architect
deciders: user, tpm
supersedes: ~
tags: [arch, data, ops, prod-gate]
---

## Context

`framework/stores/sql/kb_incidents.sql` creates the HNSW vector index over the
`chunks.embedding` column:

```sql
CREATE VECTOR INDEX ix_chunks_embedding_hnsw
  ON chunks (embedding)
  DISTANCE COSINE
  WITH TARGET ACCURACY 95
  PARAMETERS (TYPE HNSW, NEIGHBORS 32, EFCONSTRUCTION 200);
```

Oracle 23ai supports two HNSW storage organisations:

| Organisation | Creation cost | Query latency | Memory requirement |
|---|---|---|---|
| **Disk-resident** (default, no clause) | Instant (empty table) | ~2-5× slower | None |
| **`ORGANIZATION INMEMORY NEIGHBOR GRAPH`** | 20-60 s even on empty table (ADB provisions in-memory pool) | Lowest — graph loaded into SGA | Requires `INMEMORY_SIZE` or ADB in-memory option |

During local dev (`KBF_ENV=laptop`) and schema migration the disk-resident form
is correct: it creates instantly, avoids triggering the OCI GenAI client init
that was causing the ~30-60 s hang in `kbf-start.sh --migrate`, and supports
all vector search operations identically.

For production, once the `chunks` table contains real embedded data the
in-memory graph delivers substantially lower ANN query latency (the full graph
is loaded into Oracle SGA). Skipping this rebuild means production vector search
will be noticeably slower than it could be.

## Decision

1. **Migrate step keeps disk-resident index** — `kb_incidents.sql` does NOT
   include `ORGANIZATION INMEMORY NEIGHBOR GRAPH`. This is permanent; do not
   revert.

2. **Production first-deploy runbook includes a mandatory index rebuild step**
   after initial data load (ingestion has run at least once and `chunks` is
   non-empty):

   ```sql
   -- Run as ADMIN (or KB_INCIDENTS owner) after first ingestion completes.
   -- ADB must have the In-Memory option enabled; verify first:
   --   SELECT value FROM v$option WHERE parameter = 'In-Memory Column Store';
   ALTER INDEX KB_INCIDENTS.ix_chunks_embedding_hnsw
     REBUILD ORGANIZATION INMEMORY NEIGHBOR GRAPH;
   ```

   This is a **production-gate requirement** — it must appear in the
   `pmo/dashboard.md` prod-rollout checklist and be signed off before the
   service is declared production-ready.

3. **If the ADB tier does not include the In-Memory option** (check with
   `SELECT value FROM v$option WHERE parameter = 'In-Memory Column Store'`),
   keep the disk-resident form indefinitely and document the latency trade-off
   in the OCI runbook.

## Consequences

- `kbf-start.sh --migrate` no longer hangs (~5 s total instead of 30-60 s).
- First-time prod rollout requires one extra SQL step post-ingestion; tracked
  as a mandatory gate in `pmo/dashboard.md`.
- If the rebuild step is skipped, vector search is functional but slower.
  Queries will still return correct results — only latency is affected.

## Prod-rollout checklist entry

Add to `pmo/dashboard.md` prod-rollout section:

```
- [ ] ADR-025: REBUILD ix_chunks_embedding_hnsw INMEMORY after first ingestion
      SQL: ALTER INDEX KB_INCIDENTS.ix_chunks_embedding_hnsw
             REBUILD ORGANIZATION INMEMORY NEIGHBOR GRAPH;
      Gate: chunks table non-empty + ADB In-Memory option confirmed
```

## References

- Oracle 23ai Vector Search Developer Guide — "Managing Vector Indexes"
- `framework/stores/sql/kb_incidents.sql` — DDL (disk-resident form, permanent)
- `framework/cli/kb_cli.py` — `cmd_migrate` (passes `llm=None`, no OCI init)
- `docs/wiki/engineering/oci-deployment-runbook.md` — § 5 (Database schema setup)
- Commit `2e2115e` — fix(migrate): remove OCI GenAI init + INMEMORY NEIGHBOR GRAPH
