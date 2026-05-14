---
title: authorSkill conversation flow — state-by-state map (post-ADR-027)
owner: architect
created: 2026-05-14
updated: 2026-05-14
related: [ADR-015, ADR-026, ADR-027, ADR-016, ADR-017]
supersedes: authorskill-flow-pre-adr-027.md
---

# authorSkill conversation flow — state-by-state map (post-ADR-027)

> This file describes the **ADR-027 16-state machine**. The pre-ADR-027 15-state
> machine is archived at `docs/wiki/authorskill-flow-pre-adr-027.md`. In-flight
> sessions at deploy time complete under the old machine (legacy handlers are
> retained in `conversation.py`); all new sessions use the machine below.

## Executive summary

7-12 LLM calls per session (up from 3-5 in ADR-026). The LLM is involved at:

1. **CAPTURE_INTENT** — parse free-text intent into a normalised goal object
2. **CONFIGURE_SOURCES** — propose sources from intent + persona adapters
3. **INSPECT_SOURCES** — one call per source to produce a capability inventory
4. **DESIGN_SKILL** — one integrated design call: schema + source_bindings + workflow_shape + reuse_plan
5. **REVIEW_DESIGN** — only on substantive user edits (LLM re-plan diff)
6. **PREVIEW_EXTRACTION** — one call per cached sample to show real extracted values
7. **EVAL** — extraction calls (one per sample) + one faithfulness judge call

Everything else — persona identification, trigger configuration, commit,
validation, ingest, and promotion — is deterministic.

---

## IDENTIFY_PERSONA

**Trigger.** `start()` called without persona, or session begins fresh.

**What runs.** `_prompt_identify_persona` reads available personas from
`framework/persona_builders/`. `_handle_identify_persona` validates the persona
and transitions to CAPTURE_INTENT.

**LLM involvement.** None.

**Output / next state.** Sets `_data.persona`. Transitions to CAPTURE_INTENT.

---

## CAPTURE_INTENT

**Trigger.** IDENTIFY_PERSONA completes; user submits free-text intent.

**What runs.** `_handle_capture_intent` calls the LLM with `_CAPTURE_INTENT_PROMPT`.
Input: persona + raw intent string. The LLM returns a normalised goal object:

```json
{
  "output_kind": "pptx",
  "audience": "exec",
  "cadence": "weekly",
  "scope_domains": ["26ai", "FA DB"],
  "success_criteria": ["one slide", "real Confluence data"],
  "ambiguities": ["which Confluence space? — inferred from URL in intent"]
}
```

Ambiguities are shown to the user; the user can clarify or proceed. The
normalised intent is stored on `_data.normalised_intent` and passed to all
downstream LLM prompts.

**LLM involvement.** One `synthesis` call (`_CAPTURE_INTENT_PROMPT`).

**External I/O.** None.

**Output / next state.** Sets `_data.normalised_intent` and
`_data.skill_name` (re-slugified from `scope_domains` + `output_kind`).
Transitions to CONFIGURE_SOURCES.

---

## CONFIGURE_SOURCES

**Trigger.** CAPTURE_INTENT completes; user confirms or edits the normalised intent.

**What runs.** `_handle_configure_sources` calls the LLM with
`_CONFIGURE_SOURCES_SUGGEST_PROMPT`. Input: normalised_intent + persona's declared
adapters (from persona YAML `knowledge_bases[].sources`). LLM proposes a list of
source descriptors. User sees the proposal and can:
- Confirm as-is ("done")
- Edit entries ("change space to FAAAS")
- Add entries (paste URLs, page IDs, Jira JQL)

**LLM involvement.** One `synthesis` call to propose sources. Subsequent
edits/additions are deterministic (same `_parse_source_descriptor` regex parser
as before).

**External I/O.** None at this state — no live fetches yet.

**Output / next state.** Populates `_data.sources`. Transitions to INSPECT_SOURCES.

---

## INSPECT_SOURCES

**Trigger.** CONFIGURE_SOURCES completes with at least one source.

**What runs.** For each confirmed source, calls `sampler.fetch_samples` (ADR-026
Fix 2) to fetch 2-3 live pages. Then runs one LLM call per source with
`_INSPECT_SOURCES_PROMPT` to produce a source capability inventory:

```json
{
  "source_id": "confluence:20030556732",
  "available_fields": [
    {"field": "scope", "confidence": "high", "evidence": "Scope section found with 3 items"}
  ],
  "missing_fields": [
    {"field": "budget", "reason": "No financial data on this page"}
  ],
  "suggested_fields": [
    {"field": "orm_status", "type": "string", "reason": "WBS/ORM section present on all 3 samples"}
  ],
  "summary": "26ai FA DB upgrade status page. Rich project management content."
}
```

Fetched samples are **cached on `_data.source_samples`** (dict keyed by
source_id). PREVIEW_EXTRACTION and EVAL **reuse this cache** — no refetch.

**Hard-fail policy.** If a source with a page_id/page_url returns no content
(adapter unavailable, auth failure, wrong page ID), the session fails at this
state with a clear error. No synthetic sample fallback.

**LLM involvement.** One `synthesis` call per source (`_INSPECT_SOURCES_PROMPT`).
Typically 1-2 calls (1-2 sources).

**External I/O.** Live Confluence/Jira/Git fetches via configured adapters.

**Output / next state.** Sets `_data.source_samples` (cached raw content) and
`_data.source_capability` (LLM inventory). Shows the inventory to the user.
Transitions to UPLOAD_ARTIFACT_EXAMPLE.

---

## UPLOAD_ARTIFACT_EXAMPLE

**Trigger.** INSPECT_SOURCES completes; user optionally uploads a reference artifact.

**What runs.** If the user provides an artifact path (filesystem or `artifact:` ID),
calls `analyze_artifact` (python-pptx/python-docx structural parse). Output is
a **layout hint** only — section order, column structure, heading hierarchy.
Field names are NOT derived from the artifact at this state; they come from
DESIGN_SKILL.

If the user skips ("no artifact"), `_data.artifact_layout` remains `None`.
Image-only PPTX hard-fails (ADR-026 Fix 1 still applies); no vision-LLM fallback
(deferred to ADR-028).

**LLM involvement.** None — structural parse only.

**External I/O.** Reads artifact file from filesystem or ArtifactStore.

**Output / next state.** Sets `_data.artifact_layout`. Transitions to DESIGN_SKILL.

---

## DESIGN_SKILL

**Trigger.** UPLOAD_ARTIFACT_EXAMPLE completes (with or without artifact).

**What runs.** One large LLM call with `_DESIGN_SKILL_PROMPT`. The model receives:
- Normalised intent (`_data.normalised_intent`)
- Source capability inventory (`_data.source_capability`)
- Artifact layout hint (`_data.artifact_layout`, may be None)
- Existing reusable KB cards for this persona (`ShimKb.cards_visible_to(persona)`)

Output JSON:
```json
{
  "schema": {
    "title": "weekly_exec_review_26ai",
    "properties": {
      "scope": {"type": "string", "description": "...", "maxLength": 500},
      "orm_status": {"type": "string", "description": "..."}
    },
    "required": ["scope", "orm_status", "risks_mitigations"]
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
    "gaps": ["scope", "orm_status"]
  },
  "unsupportable_fields": [],
  "open_questions": ["Should exec_asks be required?"]
}
```

The design is stored on `_data.design`. Fields and field_specs are derived
from `design["schema"]` and stored on `_data.fields` and `_data.field_specs`.
`_data.reuse_result` is populated from `design["reuse_plan"]`.

**LLM involvement.** One `synthesis` call (`_DESIGN_SKILL_PROMPT`). This is the
most expensive call in the session (~3-8 seconds, ~4k-8k tokens).

**External I/O.** Reads `ShimKb` (filesystem + ADB) for existing KB cards.

**Output / next state.** Sets `_data.design`, `_data.fields`, `_data.field_specs`,
`_data.reuse_result`. Shows the complete design to the user. Transitions to
REVIEW_DESIGN.

---

## REVIEW_DESIGN

**Trigger.** DESIGN_SKILL completes; user sees the full design.

**What runs.** The user can:

**Trivial edits** (handled deterministically, no LLM call):
- `describe <field> as <text>` — update field description in schema
- `set type of <field> to <type>` — change JSON Schema type
- `rename <field> to <new>` — rename field in schema + source_bindings
- `remove <field>` — remove from schema + source_bindings
- `set trigger to <cron>` — update workflow_shape.trigger

**Substantive edits** (trigger one LLM re-plan call):
- "also pull Jira tickets assigned this sprint" — new source + new fields
- "add a risk_score field from the risk register" — requires source inspection
- "change the layout to a 3-column format"

Substantive edits trigger `_REVIEW_DESIGN_REPLAN_PROMPT` which returns a
**diff** (only changed fields/bindings), not the full design. The diff is applied
to `_data.design` and the user re-reviews.

`ok` or `looks good` confirms and transitions to CONFIGURE_TRIGGERS.

**LLM involvement.** None for trivial edits. One `synthesis` call for substantive
edits (`_REVIEW_DESIGN_REPLAN_PROMPT`).

**External I/O.** None.

**Output / next state.** Mutates `_data.design`, `_data.fields`, `_data.field_specs`.
Transitions to CONFIGURE_TRIGGERS.

---

## CONFIGURE_TRIGGERS

**Trigger.** REVIEW_DESIGN confirmed.

**What runs.** Shows the trigger already proposed in `design["workflow_shape"]["trigger"]`.
User can confirm or override. Same `_parse_trigger_input` regex parser as before.

**LLM involvement.** None.

**External I/O.** None.

**Output / next state.** Confirms/updates `_data.trigger` and `_data.output_format`.
Transitions to PREVIEW_EXTRACTION.

---

## PREVIEW_EXTRACTION

**Trigger.** CONFIGURE_TRIGGERS completes.

**What runs.** Uses the **cached** `_data.source_samples` from INSPECT_SOURCES.
For each sample, calls `review_extractions(samples, schema, llm=self._llm)`
(ADR-026 Fix 3, `review.py`). Shows the user real extracted values from the
live source content.

**Hard-fail if no samples are cached.** INSPECT_SOURCES must have succeeded.
There is no synthetic sample fallback at this state.

**LLM involvement.** One `synthesis` call per sample (`_REVIEW_EXTRACT_PROMPT`
from `review.py`). Typically 2-3 calls.

**External I/O.** None — samples already in memory from INSPECT_SOURCES.

**Output / next state.** Shows extracted field values per sample. User sees
real content from the live source. `yes/commit` → CONFIRM. Any other input
loops in PREVIEW_EXTRACTION.

---

## CONFIRM

**Trigger.** User types `yes` at PREVIEW_EXTRACTION.

**What runs.** Calls `_synthesize_preview` to assemble artifacts (schema JSON,
workflow YAML, persona_builder_delta YAML, eval gold JSONL stubs). Then calls
`_handle_commit`.

**LLM involvement.** None.

**Output / next state.** Delegates to `_handle_commit` → COMMITTED.

---

## COMMITTED

**Trigger.** User confirmed commit at CONFIRM. `_handle_commit` calls `_write_artifacts`.

**What runs.** Same as pre-ADR-027 (unchanged):
1. Serialise all synthesized artifacts.
2. Write to filesystem.
3. Write typed artifacts to ADB via `skill_store.write_artifacts` (hard-fail).

**LLM involvement.** None.

**External I/O.** Filesystem + ADB writes.

**Output / next state.** Transitions to COMMITTED. Presents committed path list.
`yes` → VALIDATE; `stop` → DONE.

---

## VALIDATE

Unchanged from pre-ADR-027. See `authorskill-flow-pre-adr-027.md` for full detail.

Graph traversal: `required_fields ⊆ provides_fields` (ADR-017 link check).
No LLM.

---

## INGEST

Unchanged from pre-ADR-027. Real Confluence fetch + markdown conversion.
Hard-fail on zero pages. No LLM.

---

## EVAL

**Trigger.** INGEST completes successfully; user types `yes, run eval`.

**What runs.** `_run_eval` (ADR-027 replacement of the stub):

1. **Re-use cached samples.** Read `_data.source_samples` set at INSPECT_SOURCES.
   If no samples are cached (session resumed after INGEST from a pre-ADR-027 session
   or the cache was lost), re-fetch using `fetch_samples` for each configured source.

2. **Extraction gold generation.** For each cached sample, call `_llm_extract`
   (from `review.py`) with the committed schema (read from ADB via `skill_store`).
   Produce a `{field: value}` dict. Build a gold row:
   ```jsonl
   {"kind":"auto_generated","source_citation":"...","source_snippet":"<first 500 chars>","expected_extraction":{...},"schema_version":"v1","created_at":"..."}
   ```
   Write to `eval/gold_sets/{persona}-{skill_name}-extraction.jsonl`.

3. **Workflow gold generation.** Call the running MCP server's `/api/v1/ask`
   endpoint (bearer `dev-only-token-replace-me`) with the canonical question
   (e.g. "What is the status of the 26ai project for this week?"). Capture:
   - `tier_used`
   - `artifact_url` (if present)
   - response text
   Build a workflow gold row:
   ```jsonl
   {"kind":"auto_generated","question":"...","expected_skill":"{persona}.{skill_name}","expected_tier":1,"expected_fields":[...],"actual_tier_used":...,"actual_artifact_url":"...","created_at":"..."}
   ```
   Write to `eval/gold_sets/{persona}-{skill_name}-workflow.jsonl`.

4. **Score recall@k.** For each extraction gold row: count how many
   `expected_extraction` keys are present (non-empty) in the LLM output.
   `recall@k = matched_fields / total_expected_fields`. Average across samples.

5. **Score faithfulness.** For each extraction result, call the LLM with
   `_EVAL_JUDGE_PROMPT` (persona + schema field + extracted value + source snippet).
   LLM answers `{"faithful": true/false, "reason": "..."}` per field.
   `faithfulness = faithful_fields / total_fields`. Average across samples.

6. **Gate PROMOTE.** Read `exit_criteria` from the workflow YAML
   (`synthesis.exit_criteria.recall_threshold`, default 0.85;
   `synthesis.exit_criteria.faithfulness_threshold`, default 0.85).
   If `recall@k < threshold` OR `faithfulness < threshold`: set
   `eval_result.exit_criteria.passed = False`. Return a turn that blocks PROMOTE
   and surfaces the failing metrics with actionable guidance.
   If both thresholds are met: `passed = True`, offer PROMOTE.

7. **Surface to user.** Show auto-generated gold rows, metrics, and the disclaimer:
   "kind=auto_generated — these were created from the same LLM that did the
   extraction, so they measure consistency, not correctness. Human review
   encouraged before promoting to production fleet-wide."

**LLM involvement.**
- One `synthesis` call per sample for extraction (2-3 calls).
- One `synthesis` call as faithfulness judge (1-2 calls).

**External I/O.**
- Reads committed schema from ADB via `skill_store.read_artifact`.
- Calls `/api/v1/ask` on the local MCP server for workflow scoring.
- Writes gold set JSONL files to `eval/gold_sets/`.
- Writes updated eval_extraction + eval_workflow artifact content back to
  ADB via `skill_store.write_artifacts` (so the gold rows are durable).

**Output / next state.** Sets `_data.eval_result` with real metrics. If
`exit_criteria.passed = True`: transitions to PROMOTE. If `False`: stays at
EVAL, offers guidance for fixing low-scoring fields.

---

## PROMOTE

Unchanged from pre-ADR-027 (ADB writes: KBF_SKILL_SESSIONS.status,
KBF_PERSONA_BUILDERS upsert). Belt-and-suspenders guard refuses PROMOTE if
`ingest_result.status == "failed"` **or** `eval_result.exit_criteria.passed != True`.

---

## DONE

Terminal. Unchanged.

---

## _SessionData changes (ADR-027)

**New fields:**
- `normalised_intent: dict` — CAPTURE_INTENT output
- `source_samples: dict[str, list[dict]]` — cached from INSPECT_SOURCES (keyed by source_id)
- `source_capability: list[dict]` — LLM inventory from INSPECT_SOURCES
- `artifact_layout: dict | None` — structural parse from UPLOAD_ARTIFACT_EXAMPLE
- `design: dict | None` — full DESIGN_SKILL output

**Removed fields:**
- `llm_suggested_specs` — folded into `design["schema"]`
- `slide_mapping` — replaced by `artifact_layout`

## LLM call budget per session

| State | Calls |
|---|---|
| CAPTURE_INTENT | 1 |
| CONFIGURE_SOURCES | 1 |
| INSPECT_SOURCES | 1 per source (usually 1-2) |
| DESIGN_SKILL | 1 |
| REVIEW_DESIGN (substantive edits only) | 0-2 |
| PREVIEW_EXTRACTION | 1 per sample (usually 2-3) |
| EVAL (extraction) | 1 per sample (usually 2-3) |
| EVAL (faithfulness judge) | 1 |
| **Total** | **7-14** |
