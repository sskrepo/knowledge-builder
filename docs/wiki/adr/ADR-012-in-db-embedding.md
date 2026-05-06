---
title: ADR-012 — In-DB embedding via DBMS_VECTOR
status: accepted
created: 2026-05-06
owner: architect
tags: [adr, embedding, oracle, phase-1]
related: [ADR-001, ADR-002, ADR-003, aira-comparison]
---

# ADR-012 — In-DB embedding via DBMS_VECTOR

## Status
Accepted (2026-05-06). Adopts the AIRA-proven pattern (see [aira-comparison.md §1.3.1](../aira-comparison.md)).

## Context
Our original design (ADR-001/003) called for client-side embedding: the application reads a `Chunk`, calls OpenAI's `text-embedding-3-large` over HTTPS, gets a 3072-dim vector back, and binds it into the SQL `INSERT`. This requires the application to manage:
- OpenAI API key plumbing on every embedding-producing host
- Batching (32 chunks/request to amortize)
- Rate-limit handling and exponential backoff
- Network failure handling between app and OpenAI
- Dimension validation
- Token-cost accounting per call

AIRA's existing production system uses a different pattern: `DBMS_VECTOR.UTL_TO_EMBEDDING` inside the database, with `OCIGenAI` as a credential-managed proxy to the same OpenAI model. The DB layer handles all of the above.

## Decision
**Adopt in-DB embedding for vector KBs.** Specifically:

1. Embedding calls are made by Oracle 23ai via `DBMS_VECTOR.UTL_TO_EMBEDDING` using the `OCIGenAI` provider, pointing at OpenAI `text-embedding-3-large` (same model as DECISION-003).
2. Application code writes only `chunk_text`; a stored procedure `BATCH_INSERT_DATASETS_VECTORS_KBI` (per-schema variant) populates the `CHUNK_EMBEDDING` column on insert.
3. The application's `LLMClient.embed()` is retained but used only for **query-time** embedding (when a retrieval tool needs to embed a single user query). Bulk ingestion never calls OpenAI from the app.
4. Query-time embedding can ALSO be moved into the DB via a function call, but for v1 we keep it app-side because:
   - Single embedding per query, no batching needed
   - Application already has the LLMClient
   - Allows hybrid retrieval (BM25 + vector) where the BM25 part is app-side too

### Schema additions
Append to `framework/stores/sql/kb_incidents.sql`:

```sql
-- OCI Vector credential (one-time setup; admin runs)
-- See https://docs.oracle.com/en/database/oracle/oracle-database/23/vecse/...
BEGIN
  DBMS_CLOUD.CREATE_CREDENTIAL(
    credential_name => 'OCI_VECTOR_CRED',
    user_ocid       => '<resource-principal>',
    tenancy_ocid    => '<tenancy>',
    private_key     => '<from OCI Vault>',
    fingerprint     => '<fingerprint>'
  );
END;
/

-- Per-schema embedding procedure
CREATE OR REPLACE PROCEDURE batch_insert_datasets_vectors_kbi AS
  CURSOR c_rows IS
    SELECT ci.id            AS content_id,
           c.id             AS chunk_id,
           c.text           AS chunk_text,
           c.heading_path   AS heading_path
    FROM   chunks c
    JOIN   content_items ci ON c.content_id = ci.id
    WHERE  c.embedding IS NULL;
BEGIN
  FOR r IN c_rows LOOP
    UPDATE chunks
    SET    embedding = DBMS_VECTOR.UTL_TO_EMBEDDING(
                          r.chunk_text,
                          JSON('{
                            "provider": "OCIGenAI",
                            "credential_name": "OCI_VECTOR_CRED",
                            "url": "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com/20231130/actions/embedText",
                            "model": "openai.text-embedding-3-large"
                          }')
                       )
    WHERE  id = r.chunk_id;
  END LOOP;
  COMMIT;
END;
/
```

The application calls this procedure after every batch upsert.

### Insert flow (revised)
```python
# framework/stores/incident_vector_store.py
def upsert(self, items):
    for item in items:
        if self._unchanged(item): continue
        self._validate(item)
        chunks = self._build_chunks(item)         # text only; no embedding
        self._upsert_content_item(item)
        self._upsert_chunks_text_only(chunks)     # embedding column NULL
    # After batch upsert, fire embedding procedure (idempotent — only embeds NULL rows)
    self._exec_proc("batch_insert_datasets_vectors_kbi")
```

### Retrieval flow (query-time)
```python
def _vector_knn(self, q):
    # App-side: embed the user query via LLMClient
    query_vec = self.llm.embed(model="text-embedding-3-large",
                               input=[q.payload["query"]])[0]
    # Bind to SQL
    cursor.execute("SELECT ... VECTOR_DISTANCE(embedding, :qv, COSINE) ...", qv=query_vec)
```

Alternative considered: use `DBMS_VECTOR.UTL_TO_EMBEDDING` for query embedding too (eliminating the app-side OpenAI client entirely). Deferred to ADR-014 if/when Phase 4 wants pure-DB retrieval.

## Why this is right

### Pros (per AIRA's production experience)
- **Simpler app**: no OpenAI HTTP client in ingestion workers
- **Lower failure surface**: fewer hops, fewer auth boundaries
- **Built-in batching**: `BATCH_INSERT_DATASETS_VECTORS` is one DB call per upsert batch
- **Idempotent by design**: `WHERE embedding IS NULL` makes re-running the proc safe
- **Aligns with DECISION-001**: full Oracle stack — let 23ai do what it does best
- **No secrets in app env**: OCI credential lives in DB; OpenAI key never touches app

### Cons (acknowledged, mitigated)
- **Less flexibility for embedding-model swap**: if we switch from OpenAI to Cohere or Voyage, both `LLMClient` (query-time) and the stored procedure (bulk) need updating. Mitigated: the stored proc is a thin wrapper; swap is a string change.
- **DB load**: embedding generation runs in the DB. Mitigated: Oracle 23ai is sized for this (Aira already runs it at production volume); embedding work is parallelizable.
- **OCI Vector credential setup**: one-time admin step. Mitigated: documented in `framework/scripts/bootstrap-vault.sh`.

## Considered alternatives
- **App-side embedding only** (our original ADR-003 design): rejected; AIRA's production proves in-DB is operationally superior.
- **Hybrid**: app-side for ingestion, in-DB for queries — rejected; backwards from where the cost is. Bulk ingest is where batching matters.
- **Sidecar embedding service**: rejected; adds a service that doesn't justify itself when 23ai already does this.

## Consequences
- `framework/stores/incident_vector_store.py`: `_embed_chunks()` removed; replaced with `_call_embed_proc()`
- `framework/stores/sql/kb_incidents.sql`: gains the `batch_insert_datasets_vectors_kbi` procedure + OCI Vector credential setup
- `framework/scripts/bootstrap-vault.sh`: adds OCI Vector credential setup as a one-time step
- `framework/core/llm.py`: keeps `embed()` for query-time use only
- All other vector KBs (PM research, Eng Mgr known_issues, Ops postmortems) follow the same pattern via per-schema variant procs
- Cost telemetry: must instrument both DB-side embedding cost and app-side query-embedding cost separately
- Eval CI: must verify the embedding model name matches across DB proc and `LLMClient` config (consistency check)

## Migration path for AIRA
This decision aligns the framework's embedding pattern with AIRA's existing one. Migration is therefore straightforward:
1. AIRA's existing tables continue working unchanged
2. Framework writes to its converged `kb_incidents` schema using the same embedding model
3. Vectors are dimensionally identical (3072) — could even share an index pool if the AIRA team chose to
4. After Phase 1 exit gate, AIRA can either keep its tables or migrate retrieval to the framework

## References
- [aira-comparison.md §1.3.1](../aira-comparison.md)
- [DECISION-001](../../../pmo/decisions/DECISION-001-oracle-tech-stack.md)
- [DECISION-003](../../../pmo/decisions/DECISION-003-llm-provider.md)
- [ADR-001 §LLM plane](ADR-001-tech-stack-baseline.md)
- [ADR-002 §kb_incidents](ADR-002-storage-shape.md)
- AIRA source: `docs/raw/aira-vector-search-detailed-explained (1).html`
