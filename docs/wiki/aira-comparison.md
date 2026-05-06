---
title: AIRA — Comparison & Analysis (Knowledge Extraction + Retrieval)
source: docs/raw/aira-vector-search-detailed-explained (1).html (DB-verified walkthrough of AIRA's current incident-KB)
compiled_at: 2026-05-06T00:00:00Z
created: 2026-05-06
owner: architect
tags: [comparison, aira, retrieval, ingestion, storage]
status: current
related: [PDD, ADR-002, ADR-003, ADR-004, ADR-007, ADR-008, persona-knowledge-builder]
---

# AIRA — Comparison & Analysis

## Scope of this document

A side-by-side architectural review of **AIRA's current incident knowledge base** (as of the May 2026 doc in `docs/raw/aira-vector-search-detailed-explained (1).html`) versus the **Knowledge Builder Framework** design captured in our PDD + ADRs. Two independent angles:

1. **Knowledge extraction & storage** — what AIRA stores, how it embeds, how it shapes the data model
2. **Retrieval** — how AIRA finds chunks, ranks them, assembles context, generates the final answer

This is **not** an AIRA critique; AIRA is the proven path the framework is being built to honor and extend. This document captures (a) what we should borrow, (b) what we'd do differently, (c) where AIRA's own roadmap converges with ours, and (d) the migration story.

---

## Part 1 — Knowledge Extraction & Storage

### 1.1 What AIRA does today

**Storage:**
- ~10 service-specific table pairs: `ADP_DATASETS` + `ADP_DATASETS_VECTOR`, `FAAASP2T_DATASETS` + `_VECTOR`, etc. — one pair per Jira project
- `CHUNK_TEXT` is **JSON-as-blob** containing the full KB record:
  ```json
  {
    "TicketId": "ADP-246527",
    "Component": "Source - adp-gi-domu-chain-patching",
    "Error Message": "NullPointerException: Pod DB state was null during resource validation",
    "Root Cause": "Pod db status was NULL; rotate pod db workflow set the pod db to NULL state.",
    "Resolution": "Updated pod db state to ACTIVE/None."
  }
  ```
- `CHUNK_EMBEDDING` is a `VECTOR` column

**Embedding generation — IN THE DATABASE:**
```sql
DBMS_VECTOR.UTL_TO_EMBEDDING(chunk_text, JSON('{
  "provider": "OCIGenAI",
  "credential_name": "OCI_VECTOR_CRED",
  "url": "https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com/20231130/actions/embedText",
  "model": "openai.text-embedding-3-large"
}'))
```

The DB itself calls OCI Gen AI (which proxies to OpenAI) to embed text. The application layer never touches the embedding API directly.

**Chunking:**
- `MAX_CHUNK_SIZE = 2000 characters` hard cap
- Sentence-boundary heuristic split: regex `(?<=\.)\s+` (split after period+spaces) tried first
- If a single sentence exceeds 2000 chars, hard-split into 2000-char pieces
- Each chunk gets `(DATASET_ID, CHUNK_INDEX)` as composite key — one ticket can produce N rows

**Insert flow:**
- `VectorDbHelper.addDataset(...)` does idempotent `MERGE` on `(DATASET_ID, CHUNK_INDEX)`
- Then calls `BATCH_INSERT_DATASETS_VECTORS(<dataset_table>, <vector_table>)` which copies missing rows and generates embeddings inline
- Skips rows already embedded (idempotent on `(DATASET_ID, CHUNK_INDEX)` match)

**Dataset table schema (selected fields):**
```
ID                         NUMBER          NOT NULL
DATASET_ID                 VARCHAR2(512)
CHUNK_INDEX                NUMBER
CHUNK_TEXT                 CLOB            NOT NULL
COMPONENT                  VARCHAR2(100)
TIME_CREATED               TIMESTAMP(6)    NOT NULL
IS_AGENT_CREATED           VARCHAR2(10)
IS_HUMAN_VALIDATED         VARCHAR2(10)
AGENT_COMMENT              VARCHAR2(2000)
JIRA_CREATION_DATE         TIMESTAMP(6)
STACK                      VARCHAR2(20/80)
ASSIGNEE                   VARCHAR2(512)
```

**Vector table schema (selected fields):**
```
DOCID                NUMBER          NOT NULL
DATASET_ID           VARCHAR2(512)   NOT NULL
CHUNK_INDEX          NUMBER          NOT NULL
BODY                 CLOB            NOT NULL
CHUNK_EMBEDDING      VECTOR
COMPONENT            VARCHAR2(100)
TIME_CREATED         TIMESTAMP(6)    NOT NULL
JIRA_CREATION_DATE   TIMESTAMP(6)
STACK                VARCHAR2(20/100)
```

**Critical gap acknowledged in AIRA's own doc:** there is no `PROPERTIES` column today. AIRA does not yet store structured failure context, tags, or compatibility fields in the DB — its retrieval today relies almost entirely on raw vector similarity plus light stack preference.

### 1.2 AIRA today vs our design — extraction & storage

| Concern | AIRA today | Our design | Verdict |
|---|---|---|---|
| Storage model | ~10 service-specific table pairs (`<SERVICE>_DATASETS` + `<SERVICE>_DATASETS_VECTOR`) | One converged `kb_incidents` schema with metadata filters | **Ours scales better** (no DDL per service); AIRA's is simpler at SQL level today |
| Embedding gen | **In-DB via `DBMS_VECTOR.UTL_TO_EMBEDDING`** | Client-side via `core/llm.py` calling OpenAI | **AIRA's is operationally cleaner — worth borrowing** (see §1.3 below) |
| Chunking | 2000-char with sentence-boundary heuristic | Token-based ~512 with overlap | Ours preserves semantic context better |
| Chunk content | JSON-as-text (loses field typing) | Typed metadata + body | Ours is queryable by field |
| Filters | **Soft** (stack 0.90× multiplier) | **Hard** WHERE clauses on indexed JSON paths | Ours is more precise; AIRA acknowledges this gap |
| `top_k` | Hard-coded 15 (param ignored) | Configurable via `Query.limit` | Trivial bug they have, not us |
| Query embedding | Includes `PROJECT:- stack:- ` prefix noise | Just the user query | They admit theirs is wrong |
| Scoring transform | `1/(1+dist)` | `dist` (raw cosine) — but should adopt same transform | **Borrow theirs** |
| Idempotency | `MERGE` on `(DATASET_ID, CHUNK_INDEX)` | `MERGE` on `id = sha256(source:source_id:schema_version)` | Both work; ours handles content-change re-ingest more cleanly |
| Multi-axis dimensions | **AIRA's proposed roadmap exactly matches our ADR-008**: failureContext, errorFamily, resourceType, operationArea, failureStage | Same as `kind`, `functional_area`, `resources`, `services` | **Independent validation of our design** |
| Properties / tags | Missing today; planned in their "future improvement" section | Built-in via `metadata_extra` JSON + multi-axis dimensions | They're adding what we already have |
| `PROPERTIES` column | Does not exist | `metadata` JSON column on every ContentItem | They're proposing to add what we ship by default |

### 1.3 Three things worth borrowing — extraction & storage

#### 1.3.1 In-DB embedding via `DBMS_VECTOR.UTL_TO_EMBEDDING` (HIGH value)

This is significant. AIRA doesn't run an OpenAI client in the application — the DB does it via OCIGenAI as a credential proxy. Same embedding model (`openai.text-embedding-3-large`), same vectors, but:

- ✅ **Simpler app code** — no batching, no rate-limit retry, no API-key plumbing on the application side
- ✅ **Lower failure surface** — fewer hops, fewer auth boundaries
- ✅ **Inline with insert** — `BATCH_INSERT_DATASETS_VECTORS` is a stored procedure that copies and embeds in one shot
- ✅ **Aligns with DECISION-001** ("full Oracle stack") — let Oracle 23ai do what it does best
- ✅ **Already proven at AIRA's production scale**

Trade-off: less flexibility if we ever swap embedding providers. But our `LLMClient` shim was for the LLM (parser/synthesizer), not embeddings — embeddings can be DB-managed without losing flexibility because both layers are independently pluggable.

**Recommendation: adopt this.** Draft **ADR-012** to formalize in-DB embedding for vector KBs, and update `framework/stores/incident_vector_store.py` to remove the app-side `_embed_chunks()` and add a `_call_batch_embed_proc()` instead. Update `framework/stores/sql/kb_incidents.sql` to add a `BATCH_INSERT_DATASETS_VECTORS`-equivalent procedure.

#### 1.3.2 Score transform `1 / (1 + VECTOR_DISTANCE)` (LOW value, easy)

Monotonic transform that maps cosine distance ∈ [0, 2] to a score ∈ (0, 1] — easier for thresholds, dashboards, and human reasoning. Currently our stub does raw distance; should swap. One-line change in `incident_vector_store.py`.

#### 1.3.3 Use AIRA's existing query/resolution pairs to seed our gold set (HIGH value, free)

AIRA must already track which retrievals worked vs didn't. Their existing gold set / production query log is the ideal **eval baseline** — we don't have to invent 25 questions; we can use 25 representative AIRA queries with known-good citations. This makes the Phase 1 exit gate ("match or beat AIRA") objectively measurable on the same eval set AIRA already runs.

**Action:** ask the AIRA team for ~50 query/expected-citation pairs from their evaluation harness; pick the 25 most representative; promote to `eval/gold_sets/incidents.jsonl`.

### 1.4 Three things to NOT borrow — extraction & storage

1. **Per-service tables as architecture** — locks you into N×DDL maintenance. Adding a new service shouldn't require ALTER permissions. Our converged schema with metadata filters wins.
2. **JSON-as-CHUNK_TEXT** — loses typed access to fields. Our schema-driven extraction (per ADR-004) splits fields into typed metadata correctly.
3. **Soft filters via score multipliers** as the *only* filter mechanism — when a user asks for "sev1 incidents in prod", they want NO preprod results, not preprod-with-0.90-multiplier. Hard filters are right; AIRA's own "Future Improvement" section is moving toward this anyway. (Soft filters do have a place — see §2.4 below.)

---

## Part 2 — Retrieval

### 2.1 AIRA's retrieval flow, separated cleanly

```
1. Java builds query string:  "FAAASP2T:- prod:- <user error text>"
2. DB function KB_VECTOR_SEARCH:
   ├── parses prefix → picks table (FAAASP2T_DATASETS_VECTOR)
   ├── embeds entire query string (including prefix) via DBMS_VECTOR
   ├── two SQL paths depending on stack:
   │   • prod/demo → SCORE × 1.00 if STACK matches, × 0.90 otherwise
   │   • other     → no boost; just raw score
   ├── ORDER BY SCORE DESC, AGE ASC          ← recency tiebreaker
   └── FETCH FIRST 15 ROWS ONLY
3. ContextBuilderNode (Java):
   ├── filter score >= 0.50
   ├── dedupe by DATASET_ID (collapses chunks)
   ├── sort SCORE DESC
   └── cap at 50,000 chars total
4. GenAI prompt → output forced into 3 sections:
   Root_Cause:
   Resolution:
   Similar ticket for reference:
5. Java parses output to typed fields (probableRootCause, probableResolution, similarTicketReference)
```

**Key code shape — the actual `KB_VECTOR_SEARCH` function (excerpted):**

```sql
CREATE OR REPLACE FUNCTION kb_vector_search (
    p_query IN VARCHAR2,
    top_k   IN NUMBER
) RETURN SYS_REFCURSOR IS
    -- ... parses p_query for project_name + stack_name from "PROJECT:- stack:- ..."
    -- ... CASE statement maps project_name to table_name (hardcoded list)

    query_vec := DBMS_VECTOR.UTL_TO_EMBEDDING(
        p_query,                              -- NB: includes the "PROJECT:- stack:- " prefix
        JSON('{
          "provider": "OCIGenAI",
          "credential_name": "OCI_VECTOR_CRED",
          "url": "...",
          "model": "openai.text-embedding-3-large"
        }')
    );

    v_age_expr := 'GREATEST(SYSDATE - COALESCE(JIRA_CREATION_DATE, TIME_CREATED), 0)';

    IF stack_name IN ('prod', 'demo') THEN
        v_sql_stmt :=
        'SELECT DATASET_ID AS DOCID,
                BODY,
                TRUNC(1 / (1 + VECTOR_DISTANCE(CHUNK_EMBEDDING, :b1)), 3) AS RAW_SCORE,
                TRUNC(
                    (1 / (1 + VECTOR_DISTANCE(CHUNK_EMBEDDING, :b1))) *
                    CASE
                        WHEN STACK IN (''prod'', ''demo'') THEN 1.00
                        ELSE 0.90
                    END
                , 3) AS SCORE
         FROM ' || table_name || '
         ORDER BY SCORE DESC,
                  NVL(' || v_age_expr || ', 3650) ASC
         FETCH FIRST :b3 ROWS ONLY';
        OPEN v_results FOR v_sql_stmt USING query_vec, query_vec, 15;  -- NB: 15 hardcoded
    ELSE
        -- non-prod path: no stack boost, but still uses age tiebreaker
        ...
    END IF;
END;
```

### 2.2 Five things strong at retrieval — worth borrowing

#### 2.2.1 Recency tiebreaker via age expression (HIGH value)

```sql
v_age_expr := 'GREATEST(SYSDATE - COALESCE(JIRA_CREATION_DATE, TIME_CREATED), 0)'
ORDER BY SCORE DESC, NVL(age, 3650) ASC
```

When two chunks score equally, the newer ticket wins. **Operationally meaningful** — last week's incident on POD-DB is more useful than last year's, even at identical similarity. We don't currently order by anything past score. Add this to `_vector_knn` SQL in `IncidentVectorStore`.

#### 2.2.2 Coarse-then-fine retrieval (MEDIUM value)

DB returns 15 candidates; app filters to score ≥ 0.50, dedupes, caps at 50K chars. This is a cheap-to-tune pattern — moving the threshold doesn't require re-running the SQL. We currently push `limit` straight into the SQL via `Query.limit`. Better: **fetch `2×limit` from DB and let the persona skill / orchestrator do final cut**. Lets us add reranker, dedup, char-cap without DB change.

#### 2.2.3 Hard character cap on context packet (MEDIUM value)

`max context size = 50,000 characters` on the way into the LLM. This bounds cost regardless of how many top-K passages got retrieved. We have token budget in ADR-007 but no explicit char cap. Add `max_context_chars` to `Budget`; persona skill enforces.

#### 2.2.4 Structured, parseable synthesis output (HIGH value)

```
Root_Cause:
Resolution:
Similar ticket for reference:
```

Java *parses* this into typed `probableRootCause`, `probableResolution`, `similarTicketReference` fields. **Three concrete benefits:**

- Downstream callers (Aira, portal, ticket UI) can use a typed contract, not blob text
- Eval can score each section independently (faithfulness on Root_Cause vs Resolution)
- Failure modes are localized (LLM forgot to write Resolution → easy to detect and re-prompt)

We currently spec the synthesizer as "synthesize with citations" — vague. **Worth amending ADR-007** to define a structured output schema per consumer use case (Aira's incident-investigation schema is one; PM/TPM consumers get different schemas).

#### 2.2.5 Score threshold ≥ 0.50 (LOW value but operationally informed)

This is the level below which AIRA's team found chunks tend to be irrelevant. We won't know our exact threshold until Phase 1 eval — but it tells us the **right artifact to produce**: a per-corpus threshold tuned against the gold set, not a constant baked into code.

### 2.3 Where AIRA's retrieval is materially weaker than ours

| Dimension | AIRA retrieval | Our design | Why ours wins |
|---|---|---|---|
| **Multi-source fanout** | Single vector table per query | Persona skill fans out across vector + wiki + graph + sql in parallel | Ops Eng question that needs incidents + runbook + fleet state requires AIRA to do 3 sequential trips; we do 1 round-trip |
| **Hard filters** | None — only stack as soft preference | `services`, `resources`, `functional_area`, `kind`, `time_window` as indexed JSON path predicates | "show patching incidents on POD" is precise, not approximate |
| **Graph traversal** | Doesn't exist | `graph_traverse(start_entity, edge_types, depth)` MCP tool | Blast-radius and dependency queries are answerable |
| **Routing** | Hardcoded `IF project_name IN ('FAAAS', ...)` in DB function — adding a service = ALTER FUNCTION | shim_faaas-driven orchestrator routing | New service = config edit |
| **Persona awareness** | None — same query, same table | Routes to PM/TPM/Ops Eng skill based on intent | A PM asking about an incident gets PM-relevant framing; AIRA only serves Ops |
| **Chunk granularity** | Returns DATASET_ID (collapses chunks) | Returns content_id + chunk_id | If chunk #7 of a 10-chunk ticket has the answer, we return chunk #7; AIRA returns the ticket and may dedupe out chunk #7 |
| **Query embedding cleanliness** | Embeds `"FAAASP2T:- prod:- "` prefix WITH the question — adds noise | Embeds query only; routing signals stay outside the embedding | Better recall on the actual question |
| **Citations invariant** | Implicit — DB returns DATASET_ID; Java rebuilds JIRA URL | Mandatory — every Result has `citation_url` | Citation contract verifiable in eval; non-citing retrievers caught by lint |

### 2.4 One subtle but important learning — hard filters vs soft preference

This is the most interesting product question in AIRA's doc.

**AIRA's choice:** stack is a **soft preference** (×0.90 multiplier, not exclusion). Reason — when there's no `prod` match, fall back to `preprod` knowledge rather than return empty. They prefer recall to precision.

**Our default:** hard filters on `services`, `resources`, etc. We prefer precision.

**Both are right in different cases.**

- A user asking *"how to refresh PODDB on prod"* probably WANTS preprod knowledge if no prod knowledge exists — recall is right.
- A user asking *"what's the SOC2 status of tenant-99"* wants hard filtering — precision is right.

**Recommendation: make filter strictness configurable per intent** in the persona skill, not globally:

```python
# In the ops_eng persona skill
filters = {
    "services":         {"values": ["auth"], "strictness": "hard"},
    "resources":        {"values": ["pod"],  "strictness": "soft", "soft_multiplier": 0.85},
    "functional_area":  {"values": ["refresh"], "strictness": "hard"},
    "stack":            {"values": ["prod"],  "strictness": "soft", "soft_multiplier": 0.90},
}
```

Maps to SQL like: hard filters → `WHERE`; soft filters → `CASE … MULTIPLY` in score expression. This is **directly inspired by AIRA's pattern** but generalized.

Worth promoting to **ADR-013 — Filter strictness contract**.

### 2.5 Concrete code changes to land (retrieval-side)

When the framework is being implemented in Phase 1, these are specific updates that follow from the AIRA review:

1. **`framework/stores/incident_vector_store.py`** `_vector_knn`:
   - Add age expression to `ORDER BY` (recency tiebreaker)
   - Use score transform `1/(1+VECTOR_DISTANCE)` (matches AIRA's monotonic score)
   - Fetch `min(2×limit, hard_cap)` then app-side cut
   - Honor `Query.limit` (no hardcoded 15)
   - Embed only the user query, not routing prefix

2. **`docs/wiki/adr/ADR-007-persona-context-skill.md`** — amend to add:
   - `max_context_chars` to `Budget`
   - Structured synthesis output schema (per consumer use case)
   - Hard vs soft filter strictness

3. **New `docs/wiki/adr/ADR-013-filter-strictness.md`** — formalize the hard/soft-with-multiplier pattern for retrieval filters.

4. **`framework/orchestrator/synthesizer.py`** (when written) — emit structured sections matching consumer's expected schema; persona skill or consumer manifest declares which schema.

5. **`docs/wiki/adr/ADR-005-eval-harness.md`** amend — score eval harness against AIRA's gold-set queries (when available); add **recency-weighted recall** as a metric (top-K with ≤30-day age preferred).

---

## Part 3 — Strategic insight: AIRA's roadmap = our current design

This is the headline takeaway worth flagging to leadership.

AIRA's own doc has two sections — **"What Is Missing Today"** and **"Recommended Future Improvement"** — that describe, almost word-for-word, the multi-axis dimensions in our [ADR-008](adr/ADR-008-functional-area-and-resources.md):

| AIRA's proposed addition | Our equivalent (already in ADRs) |
|---|---|
| `failureContext` | `kind` (incident_history / postmortem / known_issue) + `functional_area` |
| `errorFamily` (weight 0.30) | `kind` enum value |
| `failureType` (weight 0.25) | `kind` enum value |
| `resourceType` (weight 0.20) | `resources` (typed enum from `shim_faaas`) |
| `operationArea` (weight 0.15) | `functional_area` (REFRESH / PROVISIONING / PATCHING / DR) |
| `failureStage` (weight 0.10) | possible new enum or sub-axis of `kind` |
| Hard gates: same service, same component, same failure context | Hard filters on `services`, `kind`, `functional_area` |
| Tag-based scoring | Filter-driven retrieval + future hybrid scoring |
| Add `PROPERTIES JSON CHECK (PROPERTIES IS JSON)` column | We already have `metadata` JSON column on every ContentItem |

**AIRA's team independently arrived at our design.** This is strong validation that we built the right thing — and gives us a concrete migration story:

- AIRA continues running its current per-service tables until our converged path is proven on its gold set
- Phase 1 exit gate uses **AIRA's own benchmark queries** — apples-to-apples comparison
- After Phase 1 exit, AIRA's team can either (a) keep their current path with our framework as the ingestion engine, OR (b) migrate retrieval onto our converged schema in Phase 2+

---

## Part 4 — TL;DR

### Extraction & storage
- **Strongest AIRA pattern to borrow**: in-DB embedding via `DBMS_VECTOR.UTL_TO_EMBEDDING`. Eliminates a whole class of app-side embedding plumbing. Worth ADR-012.
- **Validation of our design**: AIRA's "Future Improvement" section reads like our ADR-008 + ADR-004 v2.
- **What to reject**: per-service tables as architecture; JSON-as-text in `CHUNK_TEXT`; soft-only filters.

### Retrieval
- **Strongest AIRA patterns to borrow**: recency tiebreaker, coarse-then-fine retrieval, char cap, structured synthesis output, score-threshold filtering. Each is a small ADR amendment + ~20 LOC.
- **Most subtle and valuable insight**: filter strictness should be **per-intent**, not global. Hard for "service=auth"; soft (with multiplier) for "stack=prod" so we don't lose preprod fallback.
- **Where AIRA materially can't do what we can**: multi-source fanout, graph traversal, persona-aware routing, hard typed filters, citations as a contract. These are our v1 differentiators.

### Strategic
- AIRA's team is already moving toward our design.
- Phase 1 exit gate should use AIRA's existing query/answer pairs as the gold set — apples-to-apples.
- Migration from AIRA's tables to ours is opt-in; framework can ingest into either physical layout. Recommended path: keep AIRA's tables operating; framework writes to converged schema in parallel; switch retrieval over after Phase 1 exit.

---

## Recommended next decisions

Five concrete items to formalize:

1. **ADR-012 — In-DB embedding for vector KBs** (formalize the AIRA-style pattern; update `IncidentVectorStore`)
2. **ADR-013 — Filter strictness contract** (hard / soft-multiplier per intent)
3. **ADR-005 amendment** — gold set bootstrap from AIRA's eval harness; add recency-weighted recall metric
4. **ADR-007 amendment** — `max_context_chars` budget field; structured synthesis output schema
5. **Action: contact AIRA team** — request 50 query/citation pairs from their eval harness for our gold set

Each is a small artifact; collectively they bake AIRA's operational lessons into our framework before Phase 1 implementation begins.

---

## References

- Source doc: `docs/raw/aira-vector-search-detailed-explained (1).html`
- Our framework spec: `docs/raw/knowledge-builder-framework-spec.md`
- PDD: [pdd/PDD-Knowledge-Builder-Framework.md](pdd/PDD-Knowledge-Builder-Framework.md)
- ADR-002 — Storage shape per data type: [adr/ADR-002-storage-shape.md](adr/ADR-002-storage-shape.md)
- ADR-003 — Core interfaces: [adr/ADR-003-core-interfaces.md](adr/ADR-003-core-interfaces.md)
- ADR-004 — Persona-builder config schema (v2): [adr/ADR-004-persona-builder-config.md](adr/ADR-004-persona-builder-config.md)
- ADR-007 — Persona context skill contract: [adr/ADR-007-persona-context-skill.md](adr/ADR-007-persona-context-skill.md)
- ADR-008 — Functional-area + resources dimensions: [adr/ADR-008-functional-area-and-resources.md](adr/ADR-008-functional-area-and-resources.md)
- IncidentVectorStore stub: [../../framework/stores/incident_vector_store.py](../../framework/stores/incident_vector_store.py)
- Incident KB DDL: [../../framework/stores/sql/kb_incidents.sql](../../framework/stores/sql/kb_incidents.sql)
