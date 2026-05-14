---
title: ADR-027 — Design-first authorSkill — 16-state machine
status: accepted
created: 2026-05-14
owner: architect
deciders: user, tpm
tags: [adr, skill-builder, llm-in-authoring, eval]
related: [ADR-015, ADR-026, ADR-016, ADR-017]
supersedes: (state machine section of ADR-015)
---

# ADR-027 — Design-first authorSkill — 16-state machine

## Status

Accepted (2026-05-14).

## Context

A post-ADR-026 audit of the `authorSkill` conversation found that the existing
15-state machine produces "decoration, not design":

### The decoration problem

1. **ANALYZE_ARTIFACT runs one weak LLM call** that assigns `type` + `description`
   to fields a heuristic **already picked**. The LLM never sees the source pages.
2. **Source pages are fetched after the design is already done.** The ADR-026 Fix 4
   `_source_grounded_review` runs at `_advance_to_review_schema`, but by that point
   the user has already approved a field list. Sources are configured at
   CONFIGURE_SOURCES, which comes *after* REVIEW_SCHEMA — so the grounded review
   always runs against an empty source list (the most significant functional gap
   documented in `authorskill-flow.md` known gap #6).
3. **No DESIGN step.** The machine assembles schema, source bindings, workflow
   shape, and reuse plan as four separate downstream artifacts from four separate
   passes. No single LLM call ever sees the whole picture: "given this intent +
   these source capabilities + this artifact structure, produce an integrated
   design."
4. **EVAL is a stub.** `_run_eval` writes then immediately deletes tempfiles,
   sets `recall_at_k: null`, `faithfulness: null`, and unconditionally advances to
   PROMOTE regardless of quality.
5. **CHECK_REUSE is a pure deterministic check** that could be folded into the
   design call — it adds a state-machine step without adding user value.

The result: skills are authored without ever showing the user what the source
actually contains, what fields the source can and cannot support, or what the
extracted output will actually look like against the real data.

### The desired machine

A design-first machine where:
- Sources are inspected **before** the schema is designed.
- A single LLM design call integrates intent + source capability + artifact
  structure into one coherent output: schema + source_bindings + workflow_shape
  + reuse_plan.
- The user reviews the whole design at once, not field-by-field.
- An extraction preview runs against the live samples already cached at
  INSPECT_SOURCES — no refetch.
- EVAL actually executes: auto-generate gold rows from the live samples, run
  the workflow against the promoted KB, score recall@k and faithfulness, and
  gate PROMOTE on the exit thresholds.

## Decision

### New 16-state machine

The 15-state machine is replaced by a 16-state machine. The migration boundary
is per-session: sessions in flight at deploy time are carried to completion by
the old handlers (which are retained as `_handle_*_legacy` until no in-flight
sessions remain). New sessions use the new machine exclusively.

| # | State | LLM? | What it does |
|---|---|---|---|
| 1 | IDENTIFY_PERSONA | no | (unchanged) |
| 2 | CAPTURE_INTENT | LLM `synthesis` | One call parses intent into a normalised goal object; flags ambiguity |
| 3 | CONFIGURE_SOURCES | LLM `synthesis` | LLM proposes candidate sources from intent + available adapters; user confirms |
| 4 | INSPECT_SOURCES | LLM `synthesis` (one per source) | Live fetch 2-3 samples; LLM summarises what each source can provide |
| 5 | UPLOAD_ARTIFACT_EXAMPLE | no | Structural parse only — layout hint, not field names |
| 6 | DESIGN_SKILL | LLM `synthesis` (one big call) | Schema + source_bindings + workflow_shape + reuse_plan + open_questions in one JSON |
| 7 | REVIEW_DESIGN | LLM `synthesis` on substantive edits | User sees whole design; trivial edits patched deterministically; substantive edits trigger LLM re-plan diff |
| 8 | CONFIGURE_TRIGGERS | no | User confirms/adjusts trigger already proposed by DESIGN_SKILL |
| 9 | PREVIEW_EXTRACTION | LLM `synthesis` (one per sample) | Re-use samples from INSPECT_SOURCES; show real extracted values |
| 10 | CONFIRM | no | yes/no |
| 11 | COMMITTED | no | YAML serialize + filesystem + ADB write (hard-fail) |
| 12 | VALIDATE | no | Graph traversal: required_fields ⊆ provides_fields |
| 13 | INGEST | no | Adapter fetch + markdown conversion (hard-fail on zero pages) |
| 14 | EVAL | LLM `synthesis` + judge | Auto-generate gold rows; run workflow; score recall@k + faithfulness; gate PROMOTE |
| 15 | PROMOTE | no | ADB writes (KBF_SKILL_SESSIONS.status + KBF_PERSONA_BUILDERS upsert) |
| 16 | DONE | no | Terminal |

States COMMITTED, VALIDATE, INGEST, PROMOTE, DONE are unchanged from the
existing machine (their handlers are reused verbatim).

### CAPTURE_INTENT (state 2)

Replaces the raw-string passthrough in the old IDENTIFY_PERSONA → ANALYZE_ARTIFACT
transition.

**LLM call:** One `synthesis` call with prompt `_CAPTURE_INTENT_PROMPT`. Input:
persona + raw intent string. Output JSON:
```json
{
  "output_kind": "pptx",
  "audience": "exec",
  "cadence": "weekly",
  "scope_domains": ["26ai", "FA DB"],
  "success_criteria": ["one slide", "real Confluence data"],
  "ambiguities": ["which project? — inferred 26ai from intent"]
}
```

The normalised goal object is stored on `_data.normalised_intent` and passed to
every downstream LLM prompt. Ambiguities are surfaced to the user with a
"please confirm or correct" prompt; the user can proceed without resolving them.

### CONFIGURE_SOURCES (state 3)

The LLM now proposes candidate sources from the normalised intent and the
persona's declared adapters (from persona YAML `knowledge_bases[].sources`).

**LLM call:** One `synthesis` call with prompt `_CONFIGURE_SOURCES_SUGGEST_PROMPT`.
Input: normalised_intent + persona_adapters. Output: list of source descriptors.

The user sees the LLM's proposal, confirms, edits, or adds sources. The final
confirmed source list proceeds to INSPECT_SOURCES.

Replaces the old regex-only URL extractor with an LLM-assisted proposal that
can reason about which adapter is likely to contain the needed data.

### INSPECT_SOURCES (state 4)

New state. For each confirmed source, fetch 2-3 live samples (via existing
`sampler.fetch_samples`), run one LLM call per source to produce a "source
capability inventory": what content exists, what fields it could supply, what
is NOT present.

**LLM call:** One `synthesis` call per source with prompt `_INSPECT_SOURCES_PROMPT`.
Output per source:
```json
{
  "source_id": "20030556732",
  "available_fields": [{"field": "scope", "confidence": "high", "evidence": "..."}],
  "missing_fields": [{"field": "budget", "reason": "no financial data in page"}],
  "suggested_fields": [{"field": "orm_status", "type": "string", "reason": "WBS/ORM section present"}],
  "summary": "Page contains 26ai FA DB upgrade status..."
}
```

The fetched samples are cached on `_data.source_samples` (a dict keyed by
source_id). PREVIEW_EXTRACTION and EVAL reuse these samples — no refetch.

### UPLOAD_ARTIFACT_EXAMPLE (state 5)

Replaces ANALYZE_ARTIFACT. The artifact analysis is now structural only: extract
section/column layout as a LAYOUT HINT, not field names. The field names come from
DESIGN_SKILL (driven by source capability + intent), not from the artifact.

No LLM call at this state. python-pptx/python-docx structural parse produces
`_data.artifact_layout` dict (section order, column structure, heading hierarchy).
Vision-LLM for image-only artifacts is deferred to ADR-028.

### DESIGN_SKILL (state 6)

The integration step. One big LLM call that sees everything:
- normalised intent (from CAPTURE_INTENT)
- source capability inventory (from INSPECT_SOURCES)
- artifact layout hint (from UPLOAD_ARTIFACT_EXAMPLE, if provided)
- existing reusable KB cards visible to the persona (from ShimKb)

**LLM call:** One `synthesis` call with prompt `_DESIGN_SKILL_PROMPT`. Output JSON:
```json
{
  "schema": {
    "title": ...,
    "properties": { "field": { "type": ..., "description": ..., "maxLength": ... } },
    "required": [...]
  },
  "source_bindings": {
    "scope": ["confluence:20030556732"],
    "orm_status": ["confluence:20030556732"]
  },
  "workflow_shape": {
    "output_format": "pptx",
    "layout": "weekly_exec_review_v1",
    "trigger": {"on_request": true, "schedule": "0 16 * * 5"},
    "retriever": "search_wiki"
  },
  "reuse_plan": {
    "covered": {},
    "gaps": ["scope", "orm_status", "risks_mitigations"]
  },
  "unsupportable_fields": [],
  "open_questions": ["Should exec_asks be a required field?"]
}
```

This single call replaces:
- `_llm_analyze_artifact` (ANALYZE_ARTIFACT)
- `synthesize_field_descriptions` delta pass (REVIEW_SCHEMA)
- `_source_grounded_review` (REVIEW_SCHEMA Fix 4)
- `detect_reuse` (CHECK_REUSE)
- The four-separate-module PREVIEW assembly

### REVIEW_DESIGN (state 7)

Replaces REVIEW_FIELDS + REVIEW_SCHEMA (two states collapsed to one).

The user sees the complete design: schema with per-field rationale, source
bindings, workflow shape, reuse plan, and open questions.

**Trivial edits** (rename, remove field, change maxLength, change description):
handled deterministically by `_apply_design_patch` — no LLM call.

**Substantive edits** ("also pull jira tickets", "add a risk_score field from Jira"):
trigger one LLM re-plan call with prompt `_REVIEW_DESIGN_REPLAN_PROMPT` that
returns a diff (only changed fields/bindings), not the full design.

### CONFIGURE_TRIGGERS (state 8)

Unchanged from the old machine, but now the user is confirming a trigger that
DESIGN_SKILL already proposed in `workflow_shape.trigger`. The prompt shows the
proposal; the user can confirm or override.

### PREVIEW_EXTRACTION (state 9)

New state. Replaces the old PREVIEW state's artifact-path summary with a real
extraction preview.

Uses the cached `_data.source_samples` from INSPECT_SOURCES — no refetch.
Calls `review_extractions(samples, schema, llm=self._llm)` (ADR-026 Fix 3,
already implemented). Shows the user what values will actually be extracted
from the live source.

**Hard-fail if no samples are cached** (INSPECT_SOURCES must have succeeded
and cached at least one sample). No synthetic sample fallback.

### CONFIRM (state 10)

Renamed from PREVIEW but functionally identical. The user sees a summary of
committed artifact paths and confirms.

### EVAL (state 14) — Option A implementation

The stub is replaced with a real implementation.

**Algorithm:**
1. Re-use `_data.source_samples` (already fetched and cached at INSPECT_SOURCES).
2. For each sample, run the schema's extraction prompt against it using
   `_llm_extract` (from `review.py`) — produce `expected_extraction` values.
   Freeze the source snippet alongside so future runs replay against the same input.
3. Write extraction gold rows to
   `eval/gold_sets/{persona}-{skill_name}-extraction.jsonl` with
   `kind="auto_generated"`.
4. For workflow evaluation: submit the canonical "what is the status of {project}?"
   question to `/api/v1/ask` (MCP server at `http://localhost:8080`), capture the
   response, check `expected_skill` match + presence of `expected_fields` from
   the gold entries. Write workflow gold rows to
   `eval/gold_sets/{persona}-{skill_name}-workflow.jsonl` with `kind="auto_generated"`.
5. Compute metrics:
   - **recall@k**: fraction of expected_fields present in the extracted output.
   - **faithfulness**: LLM judge using prompt `_EVAL_JUDGE_PROMPT` — does the
     extracted value appear in the source snippet?
   - **latency**: wall-clock time for the `/api/v1/ask` round-trip.
   - **dollars**: token cost from the extraction + judge calls.
6. Gate PROMOTE on `exit_criteria` thresholds from the workflow YAML
   (default 0.85 recall / 0.85 faithfulness). Hard-fail when metrics fall below
   thresholds — no PROMOTE until criteria are met.
7. Surface the auto-generated rows + metrics with note:
   "kind=auto_generated — created from the same LLM that did the extraction,
   so they measure consistency, not correctness. Human review encouraged before
   promoting to production fleet-wide."

**LLM calls at EVAL:**
- One `synthesis` call per sample for extraction (reuses `_llm_extract`).
- One `synthesis` call as faithfulness judge using `_EVAL_JUDGE_PROMPT`.

The tempfile write-then-delete pattern (old lines ~1900-1926) is deleted.

### Session migration

Sessions persisted under the old 15-state machine continue to execute via the
old handlers until they reach DONE or are abandoned. The serialized `state`
field is compared against `STATES_V1` (the old list) to detect legacy sessions.
New sessions never see ANALYZE_ARTIFACT, REVIEW_FIELDS, REVIEW_SCHEMA, or
CHECK_REUSE in their STATES list.

### LLM call budget change

| Version | LLM calls per session |
|---|---|
| Pre-ADR-026 | ~2-3 |
| Post-ADR-026 | ~3-5 |
| Post-ADR-027 | ~7-12 |

The increase is intentional. `authorSkill` is a one-shot authoring flow (not a
real-time query path). The additional calls are in service of the core premise:
don't commit a skill the system has never "seen" against the real source data.

## Consequences

### Positive

- Source inspection happens **before** schema design — the LLM designs for what
  the source can actually provide, not for what the artifact headings suggest.
- One integrated design call sees intent + source capability + artifact layout
  simultaneously — no drift between schema and source bindings.
- PREVIEW_EXTRACTION shows real extracted values before commit — the ADR-026 Fix 3
  promise is finally wired into the flow.
- EVAL is real — skills that don't meet quality thresholds cannot be promoted.
- Gold rows are auto-generated from live sources — no more manual gold seeding to
  block the first promotion.
- `kind=auto_generated` honesty note prevents users from treating auto-generated
  gold as ground truth.

### Negative / Costs

- **Latency:** authorSkill now takes 2-5 minutes end-to-end (up from ~1 minute).
  This is the authoring latency for a one-shot workflow skill — not the query
  latency. Acceptable tradeoff for quality.
- **Cost:** ~$0.10-0.30 per authorSkill session at current OCI GenAI pricing
  (was ~$0.02). The per-session cost is amortized over all future queries.
- **Complexity:** the state machine now has 16 states vs 15. DESIGN_SKILL prompt
  is large (~200 lines with examples). Debugging failures requires understanding
  the full design JSON output.
- **INSPECT_SOURCES must succeed:** if the Confluence adapter is unavailable
  (network, auth, bastion), the session hard-fails at state 4. Previously the
  flow could proceed to COMMITTED without any live source access.

### Reversibility

- The new state machine is additive: old session artifacts (workflow YAML, schema
  JSON, gold JSONL) are format-compatible. Promoting a skill authored under ADR-027
  is identical from the runtime's perspective.
- Rolling back to the ADR-026 machine requires restoring `STATES`, the handler
  dispatch table, and the `_run_eval` stub — all isolated to `conversation.py`.
- EVAL Option A can be disabled by setting `exit_criteria.passed = True`
  unconditionally (feature flag in the workflow YAML).

## Spec §6 interface impact

No changes to the external API surface (`/api/v1/kb/authorSkill` endpoints,
session persistence schema, ConversationTurn dataclass). The state enum in the
OpenAPI spec gains new values:
- Added: `CAPTURE_INTENT`, `INSPECT_SOURCES`, `UPLOAD_ARTIFACT_EXAMPLE`,
  `DESIGN_SKILL`, `REVIEW_DESIGN`, `PREVIEW_EXTRACTION`
- Removed: `ANALYZE_ARTIFACT`, `REVIEW_FIELDS`, `REVIEW_SCHEMA`, `CHECK_REUSE`
- Unchanged: `IDENTIFY_PERSONA`, `CONFIGURE_SOURCES`, `CONFIGURE_TRIGGERS`,
  `CONFIRM`, `COMMITTED`, `VALIDATE`, `INGEST`, `EVAL`, `PROMOTE`, `DONE`

The `_SessionData` dataclass gains:
- `normalised_intent: dict` — output of CAPTURE_INTENT
- `source_samples: dict` — keyed by source_id, cached from INSPECT_SOURCES
- `artifact_layout: dict | None` — structural parse from UPLOAD_ARTIFACT_EXAMPLE
- `design: dict | None` — full DESIGN_SKILL output

Fields removed from `_SessionData`:
- `llm_suggested_specs` (folded into DESIGN_SKILL output)
- `slide_mapping` (replaced by `artifact_layout`)

## Alternatives considered

### Option A — Keep current heuristic+decorate machine (no change)
The existing machine works for simple skill authoring but produces skills with
schema-source mismatches that only appear at query time. The ADR-026 audit found
that 100% of skills authored in the current machine need manual schema corrections
before the extraction quality is acceptable. Rejected: the purpose of authorSkill
is to automate precisely this work.

### Option B — Source inspection at REVIEW_SCHEMA (smaller change)
Move `_source_grounded_review` to happen at CONFIGURE_SOURCES → REVIEW_SCHEMA
transition (sources are now available). This would fix the ordering problem
without adding new states. Rejected: the fundamental problem is that the *design*
(schema + source bindings + workflow) is split across 4-5 separate LLM calls
and deterministic passes, none of which sees the whole picture. A single design
call is architecturally cleaner and more reliable.

### Option C — Fully LLM-autonomous (no user review)
One LLM call authors the entire skill end-to-end; no intermediate user review
states. Rejected: spec principle §2 ("Deterministic extraction rules over autonomous
LLM extraction"). User review is the quality gate — the whole REVIEW_DESIGN state
exists to let the user catch incorrect source bindings before commit.

### Option D — EVAL Option B (separate human gold set)
Human curators fill in `expected_extraction` values after authoring. More accurate
but blocks promotion until human review is complete (days/weeks). Rejected for v1:
auto-generated gold unblocks the workflow while signalling the accuracy limitation
with the `kind=auto_generated` flag.

## References

- [ADR-015 — Skill-by-demonstration](ADR-015-skill-by-demonstration.md)
- [ADR-026 — Source-grounded schema review + layout-aware PPTX rendering](ADR-026-source-grounded-schema-review-and-layout-aware-pptx.md)
- [ADR-016 — Workflow skills](ADR-016-workflow-skills.md)
- [ADR-017 — Extraction-workflow linking](ADR-017-extraction-workflow-linking.md)
- [docs/wiki/authorskill-flow.md](../authorskill-flow.md) — updated to describe new flow
