---
id: ADR-023
title: kbf_ops persona and reviewSkillSession MCP tool for session quality review
status: accepted
created: 2026-05-12
owner: architect
tags: [ops, quality, kbf_ops, mcp, skill-builder]
related: [DECISION-007, ADR-021, DECISION-006]
supersedes: []
---

# ADR-023 — kbf_ops persona and `reviewSkillSession` MCP tool

## Status

Accepted — implementation in progress (DECISION-007).

---

## Context

The `authorSkill` state machine produces 4 committed artifacts per session. Manual review of
`synth-tpm-ec2fad6d` identified 5 server-side bugs and 4 enhancement gaps — all without any
user error. The review required cross-checking: session intent, conversation history, 4 artifact
CLOBs, the uploaded example file, eval gold sets, routing descriptors, and KB wiring. This is
comprehensive qualitative analysis that needs LLM reasoning (not just structural invariant checks)
to catch issues like:

- Schema missing 6 of 10 fields the user's artifact specified
- `use_when` too literal to route reliably at the 0.85 threshold
- Hallucinated KB reference (`pm.pm_market_research`) for an unrelated persona
- Two overlapping skills created when one was intended
- Artifact analysis not threaded into gold set seeding

Three implementation options were evaluated (see DECISION-007). **Option 2 — `kbf_ops` persona
+ LLM synthesis** was chosen because the team is in early stages and needs deep qualitative
analysis, not just structural invariant checks. Deterministic checks (Option 1) are recorded in
this ADR as a **deferred complementary layer** (ADR-024).

---

## Decision

Introduce a new `kbf_ops` **persona** whose data sources are the framework's own ADB tables
(`KBF_AUTHOR_SKILL_SESSIONS`, `KBF_SKILL_ARTIFACTS`) rather than Confluence/Jira/Git.

Introduce a new 5th external MCP tool **`reviewSkillSession`** that invokes the `kbf_ops`
review engine directly (bypassing the orchestrator's 4-tier routing — this tool is called
explicitly, not via intent matching).

### What `kbf_ops` is — and is not

`kbf_ops` is NOT a full persona builder with Confluence ingestion, vector embeddings, or
semantic search. The data it reads is **structured ADB rows** — applying the CLAUDE.md
principle: *"Don't LLM-parse data with no summary value — schema-defined data → relational
store, full stop."*

`kbf_ops` is a **namespace** for framework-internal operational skills. Its "retriever" is an
`AdbDirectRetriever` (new class) that executes targeted SQL and returns typed structs, not
cosine-ranked passages. The LLM is used only in the **critique/synthesis step**, not in data
retrieval.

---

## Architecture

### Execution flow

```
reviewSkillSession(synthId, depth)
  │
  ├─ KbfOpsSessionLoader          ← new: loads all data for synth_id from ADB
  │    ├─ AdbSessionStore.load()   ← session state, conversation history, intent
  │    ├─ AdbSkillStore.list()     ← 4 artifact CLOBs
  │    ├─ FilestoreArtifactStore   ← uploaded example files
  │    └─ KBF_ERROR_LOG query      ← errors during that session
  │
  ├─ KbfOpsReviewEngine           ← new: LLM critique with structured output
  │    ├─ builds critique context bundle (all data above)
  │    ├─ calls LLM with review prompt template
  │    └─ parses structured findings (dimension scores + bug list)
  │
  ├─ AdbErrorStore.record_user_bug() × N   ← files each finding to KBF_BUG_REPORTS
  │
  └─ returns QualityReport JSON
```

### New components

| Component | Location | What it does |
|---|---|---|
| `KbfOpsSessionLoader` | `framework/retrievers/kbf_ops/session_loader.py` | SQL queries for all synth_id data |
| `KbfOpsReviewEngine` | `framework/deploy/ops/review_engine.py` | LLM critique + structured output parser |
| `review_skill_session.yaml` | `framework/workflow_skills/kbf_ops/review_skill_session.yaml` | Workflow skill definition |
| `review_skill_session/v1.json` | `framework/parsers/schemas/kbf_ops/review_skill_session/v1.json` | Output JSON schema |
| `reviewSkillSession` handler | `framework/deploy/mcp_tools.py` | 5th external MCP tool |
| `KBF_AUDIT_RUNS` table | `framework/db/migrations/006_audit_runs.sql` | Dedup + history of audit runs |

### `reviewSkillSession` MCP tool schema

```json
{
  "name": "reviewSkillSession",
  "description": "Comprehensive LLM-powered quality review of a completed authorSkill session. Reads all committed artifacts, conversation history, and uploaded files. Files structured findings to KBF_BUG_REPORTS and returns a quality report.",
  "inputSchema": {
    "type": "object",
    "required": ["synthId"],
    "properties": {
      "synthId": {
        "type": "string",
        "description": "The synth_id of the session to review."
      },
      "depth": {
        "type": "string",
        "enum": ["structural", "semantic", "full"],
        "default": "full",
        "description": "structural=invariant checks only (fast, free). semantic=LLM critique of content quality. full=both."
      },
      "fileBugs": {
        "type": "boolean",
        "default": true,
        "description": "When true, confirmed findings are filed to KBF_BUG_REPORTS automatically."
      }
    }
  }
}
```

### Quality report output schema

```json
{
  "synthId": "synth-tpm-ec2fad6d",
  "reviewId": "rev-abc123",
  "persona": "tpm",
  "skillName": "weekly_26ai_exec_review_ppt",
  "status": "committed | promoted",
  "overallScore": 4.5,
  "recommendation": "do_not_promote | promote_with_fixes | promote",
  "dimensions": {
    "intentFidelity":       { "score": 6, "maxScore": 10, "findings": [] },
    "schemaCompleteness":   { "score": 3, "maxScore": 10, "findings": [] },
    "kbWiring":             { "score": 5, "maxScore": 10, "findings": [] },
    "routingDescriptors":   { "score": 4, "maxScore": 10, "findings": [] },
    "evalQuality":          { "score": 2, "maxScore": 10, "findings": [] },
    "artifactConsistency":  { "score": 6, "maxScore": 10, "findings": [] },
    "askKbRoutingSimulation": { "score": 4, "maxScore": 10, "findings": [] }
  },
  "bugsFiledCount": 5,
  "bugsFiledIds": ["BUG-009", "BUG-010", "BUG-011", "BUG-012", "BUG-013"]
}
```

### Critique dimensions

| Dimension | What the LLM checks |
|---|---|
| `intentFidelity` | Did the server correctly understand the user's stated goal? Do the artifacts reflect it? |
| `schemaCompleteness` | Are all fields the user described (in text or uploaded artifact) captured in the schema? |
| `kbWiring` | Do all `requires_extractions.kb` references exist? Do `provides_fields` match required fields? |
| `routingDescriptors` | Will `use_when` + `example_invocations` reliably route above the 0.85 threshold? Are there ≥3 variants? |
| `evalQuality` | Are gold set `expected_extraction` values populated? Are `expected_output_includes` scoped to this skill only? |
| `artifactConsistency` | Do all 4 artifacts tell the same story? Do field names align across schema, workflow YAML, persona builder? |
| `askKbRoutingSimulation` | Simulate 3 natural-language queries — do they reach this skill at Tier 1 or fall through? |

### `KBF_AUDIT_RUNS` table (new DDL — migration-006)

```sql
CREATE TABLE KB_SHIM.KBF_AUDIT_RUNS (
    review_id      VARCHAR2(64)  NOT NULL,
    synth_id       VARCHAR2(64)  NOT NULL,
    run_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP,
    depth          VARCHAR2(16)  DEFAULT 'full',
    overall_score  NUMBER(4,1),
    recommendation VARCHAR2(32),
    bugs_filed     NUMBER(4)     DEFAULT 0,
    triggered_by   VARCHAR2(128),  -- 'mcp_tool' | 'scheduler' | 'cli'
    report_json    CLOB,
    CONSTRAINT pk_audit_runs PRIMARY KEY (review_id)
);
CREATE INDEX idx_audit_synth ON KB_SHIM.KBF_AUDIT_RUNS (synth_id, run_at DESC);
```

---

## Deferred: Option 1 (deterministic audit layer) → ADR-024

Option 1 (Python check functions + background loop) is NOT abandoned. It is the **right
complementary layer** for cheap, always-on structural checks that run in milliseconds with zero
LLM cost. File ADR-024 when:
- The LLM review cost becomes significant at scale (>50 sessions/week)
- Known bug classes stabilise enough to encode as deterministic invariants
- A CI gate is wanted for every new skill commit (Option 1 can run in CI; Option 2 cannot)

The check functions from Option 1 can also be used as a **pre-filter** inside Option 2's
`KbfOpsReviewEngine`: run structural checks first, if all pass then invoke the LLM for
semantic analysis only.

---

## Consequences

### Positive
- Deep qualitative analysis of any session on demand — the same critique I did manually is
  now repeatable and consistent
- Findings automatically filed to `KBF_BUG_REPORTS` with structured metadata
- `reviewSkillSession` callable by any operator agent (Claude Code, monitoring scripts)
- `kbf_ops` namespace established for future ops skills (cost anomaly, routing effectiveness,
  KB freshness) — all follow the same `KbfOpsSessionLoader + ReviewEngine` pattern
- `depth=structural` provides a fast path that skips LLM calls for quick checks

### Negative / watch points
- LLM non-determinism: two review runs of the same session may produce different findings.
  Mitigate by: (a) structured output schema with explicit scoring rubric, (b) dedup on
  `(synth_id, check_name)` in `KBF_BUG_REPORTS` before filing
- Cost per review: one LLM call per session, ~2K tokens input + ~500 output. Acceptable at
  early stage; revisit when reviewing >50 sessions/week (→ ADR-024 deterministic layer)
- `AdbDirectRetriever` is a new pattern in the retriever layer. It must NOT be confused with
  the vector retriever. Document clearly in `framework/retrievers/README.md`.

---

## Amendment (2026-05-16) — LLM-review content-filter advisory finding

When the inference provider's content-safety filter rejects the LLM review prompt (e.g. OCI
GenAI returns HTTP 400 "Inappropriate content detected"), `_run_llm_review` now emits a
**distinct, provider-detail-free advisory finding** instead of the generic `llm_review_failed`
bug. Key properties:

| Property | Value |
|---|---|
| `check_name` | `llm_review_content_filtered` |
| `severity` | `minor` (lowest valid enum — advisory only) |
| Description | Provider-detail-free. Contains a KBF- correlation ID. Explicitly states "This is not a skill defect". |
| Provider internals | NOT persisted — no `opc-request-id`, no OCI endpoint, no raw error dict, no HTTP status code. |
| Structural checks | Continue to run and contribute to the report — content-filter does NOT abort the review. |

**Implementation**: `_is_content_filter_error` and `ContentFilterRejection` are imported from
`framework.skill_builder.review` (shared detector — no duplicate logic). The except handler
tests the content-filter condition FIRST before the generic fallback, which is unchanged.
A reviewer encountering `llm_review_content_filtered` must not block promotion on this finding
alone — it signals an environmental/provider block, not a quality issue with the skill.
