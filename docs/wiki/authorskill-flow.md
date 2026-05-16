---
title: authorSkill conversation flow — state-by-state map (post-ADR-029 S6)
owner: architect
created: 2026-05-14
updated: 2026-05-15
related: [ADR-015, ADR-026, ADR-027, ADR-028, ADR-029, ADR-016, ADR-017]
supersedes: authorskill-flow-pre-adr-027.md
---

# authorSkill conversation flow — state-by-state map (post-ADR-028/ADR-029 S6)

> This file describes the **ADR-028/ADR-029 17-state machine** (ADR-028 Stream A S1-S4 +
> ADR-029 Phase 1 S5 + ADR-029 Phase 2 S6).
> The pre-ADR-027 15-state machine is archived at `docs/wiki/authorskill-flow-pre-adr-027.md`.
> The ADR-027 16-state machine is superseded by this document (CLARIFY added as 17th state).
> In-flight sessions at deploy time complete under the old machine (legacy handlers are
> retained in `conversation.py`); all new sessions use the machine below.

## ADR-029 changes (Phase 2 — S6)

- **EVAL reject path is now fully active.** The `# TODO-S6` seam is replaced by
  `_classify_and_route` (new method). When the user types anything other than "accept",
  "ship as draft", "stop", or "force promote" at EVAL, the failure-class classifier runs.
- **Failure-class classifier** (`_FAILURE_CLASSIFIER_PROMPT`): validated against the
  known gold case (commit eb31230, gate: 3/3 runs → MISSING_FIELDS). Called with ALL SIX
  mandatory inputs: `normalised_intent`, `schema_properties`, `capability_inventory`,
  `gap_report`, `missing_sections`, `thin_sections`. Parsed via shared
  `_parse_llm_json_response` helper (parity with S5 / review.py).
- **Constrained routing map (code, not LLM-decided):**

  | failure_class | target state |
  |---|---|
  | MISSING_FIELDS | REVIEW_DESIGN |
  | THIN_FIELDS | REVIEW_DESIGN |
  | WRONG_LAYOUT | REVIEW_DESIGN |
  | SOURCE_COVERAGE | CONFIGURE_SOURCES |
  | WRONG_SOURCE | INSPECT_SOURCES |
  | UNSUPPORTABLE | DONE (draft) |

- **Six guardrails (ADR-029 §C.3):**
  1. `confidence == "low"` → route to REVIEW_DESIGN by default; NEVER auto-route to
     CONFIGURE_SOURCES or INSPECT_SOURCES on low confidence. Unknown/garbled class also
     treated as low-confidence.
  2. `UNSUPPORTABLE` → DONE as draft immediately. No loop.
  3. Consecutive-same-class: if `last_eval_failure_class == current failure_class` →
     emit pathological-loop message, DONE as draft.
  4. `eval_iteration_count >= 3` → DONE as draft.
  5. `eval_cumulative_cost_usd > 2.00` → DONE as draft.
  6. ALWAYS surface `evidence` + `why_not_alternative` to the user with
     `must_show_human=True` BEFORE applying the route. The routing turn must be confirmed
     by the user — no silent auto-advance even on confident diagnosis.
- **Routing-confirmation interstitial state** (`EVAL_ROUTE_PENDING`): after the classifier
  runs, the state machine moves to this internal transient state. `_handle_eval_route_confirm`
  handles the user's confirmation. Valid responses: `"confirm route to <STATE>"` (transitions),
  `"accept"` (→ PROMOTE), `"ship as draft"` (→ DONE), `"stop"` (→ DONE).
- **New `_SessionData` fields (backward-compat defaults for pre-S6 sessions):**
  - `eval_iteration_count: int = 0` — incremented each time the reject path runs the classifier
  - `eval_cumulative_cost_usd: float = 0.0` — accumulates classifier LLM call cost
  - `last_eval_failure_class: str | None = None` — set after each classification for
    consecutive-same-class detection
  - `_eval_pending_route: str = "REVIEW_DESIGN"` — transient; target state pending user confirm
  All three durable fields are in `to_dict()` / `from_dict()`.
- **No stub-mode for classifier**: if `self._llm is None`, returns a `must_show_human=True`
  EVAL error turn (actionable operator message). No silent skip.
- **Auto-gold is diagnostic-only**: the `exit_criteria.passed` path remains diagnostic-only.
  The only PROMOTE gate is user's explicit `"accept"` (unchanged from S5).

## ADR-029 changes (Phase 1 — S5)

- **UPLOAD_ARTIFACT_EXAMPLE**: Image-only artifacts are now **hard-rejected** with verbatim
  `IMAGE_ONLY_MESSAGE` and `must_show_human=True`. State does NOT advance. No vision-LLM
  fallback (ADR-029 decision). Unsupported file types are also rejected. Artifact bytes
  and type are **retained** in `_SessionData` (`artifact_reference_id`,
  `artifact_reference_type`) through to EVAL for comparator use.
- **EVAL**: `exit_criteria.passed` (recall@k + faithfulness thresholds) is now
  **diagnostic-only** — it no longer gates PROMOTE (DECISION-010 superseded by ADR-029).
  The primary EVAL signal is now the **ArtifactComparator gap report** (`structure_score`,
  `density_score`, `missing_sections`, `thin_sections`). Gap report surfaced in EVAL turn
  with `must_show_human=True`. PROMOTE gate is now **user acceptance only**.
- **EVAL terminal gate — user acceptance**: options are `["accept", "ship as draft",
  "review design", "configure sources", "stop here"]`. "accept" or "promote" → transitions
  to PROMOTE (stamps `user_accepted=True`). "ship as draft" → DONE without promoting.
  "stop" → DONE. Reject / "review design" / "configure sources" → **S6 classifier routing
  (active as of S6 — gate passed commit eb31230).**
- **PROMOTE KB-resolvability gate** (Folded Fix 2, BUG-queue-e685d): before marking
  production, `persona_builder_delta` must exist in ADB (hard-fail if absent). After
  `upsert_persona_builder_kb`, a fresh `ShimKb` load must find the card (hard-fail only
  when the store has cards — test env with 0 cards gets a warning and proceeds).
- **Shared `_parse_llm_json_response` helper** (Folded Fix 1, BUG-573e3 parity):
  `review._llm_extract` and `executor._llm_extract_fields` both use one canonical
  JSON-parse function with full BUG-573e3 (bare control chars) + BUG-44364 (truncation)
  fix sequence. Neither call site silently returns `{}` on parse failure.

## ADR-028 changes (Stream A S1–S4)

- **S1**: `synthesisable` added as fourth confidence level in INSPECT_SOURCES + DESIGN_SKILL
  prompts. Fields aggregated from multiple source items must now use this level; the field
  description must include an explicit "Derive this value by…" aggregation instruction.
- **S2**: `ConversationTurn` gains `must_show_human: bool` and `awaiting_user: bool`.
  `must_show_human=True` is set on REVIEW_DESIGN, PREVIEW_EXTRACTION, and CLARIFY turns.
  MCP tool description updated with CRITICAL instruction for smart clients.
- **S3**: CLARIFY added as the 17th state (between CAPTURE_INTENT and CONFIGURE_SOURCES,
  and optionally between DESIGN_SKILL and REVIEW_DESIGN). See CLARIFY section below.
- **S4**: Persona prompt fragments (`key_fields`, `extraction_style`, `few_shot_example`)
  injected from `framework/config/persona_prompts.yaml` into CAPTURE_INTENT and DESIGN_SKILL
  prompts. Unknown personas degrade loudly (warning logged, empty defaults used).

## Executive summary

**17-state machine** (ADR-028 added CLARIFY as 17th state between CAPTURE_INTENT and
CONFIGURE_SOURCES). 7-12 LLM calls per session (up from 3-5 in ADR-026). The LLM is
involved at:

1. **CAPTURE_INTENT** — parse free-text intent into a normalised goal object (S4: persona fragments injected)
2. **CONFIGURE_SOURCES** — propose sources from intent + persona adapters
3. **INSPECT_SOURCES** — one call per source to produce a capability inventory (S1: synthesisable level added)
4. **DESIGN_SKILL** — one integrated design call: schema + source_bindings + workflow_shape + reuse_plan (S1+S4)
5. **REVIEW_DESIGN** — only on substantive user edits (LLM re-plan diff)
6. **PREVIEW_EXTRACTION** — one call per cached sample to show real extracted values
7. **EVAL** — extraction calls (one per sample) + one faithfulness judge call

Everything else — persona identification, CLARIFY (deterministic question/answer),
trigger configuration, commit, validation, ingest, and promotion — is deterministic.

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

**What runs.** `_advance_to_capture_intent` calls the LLM with `_CAPTURE_INTENT_PROMPT`.
Input: persona + raw intent string + persona key_fields hint (S4). The LLM returns:

```json
{
  "output_kind": "pptx",
  "audience": "exec",
  "cadence": "weekly",
  "scope_domains": ["26ai", "FA DB"],
  "success_criteria": ["one slide", "real Confluence data"],
  "blocking_ambiguities": ["Which Confluence space — FAAAS or FA-LEGACY?"],
  "nice_to_know_ambiguities": ["Include budget table? Assuming yes."]
}
```

**ADR-028 S3 routing logic** (in `_advance_to_capture_intent`):
- `blocking_ambiguities` non-empty → route to **CLARIFY** state (must_show_human=True)
- `nice_to_know_ambiguities` only (no blocking) → auto-advance to CONFIGURE_SOURCES
  (assumptions stored on `normalised_intent.nice_to_know_assumptions`)
- Zero ambiguities → show CAPTURE_INTENT confirmation turn; user types "ok" to advance

**LLM involvement.** One `synthesis` call (`_CAPTURE_INTENT_PROMPT`).

**External I/O.** None.

**Output / next state.** Sets `_data.normalised_intent` and `_data.skill_name`.
Routes to CLARIFY or CONFIGURE_SOURCES depending on ambiguity split.

---

## CLARIFY

**Trigger.** `_advance_to_capture_intent` finds `blocking_ambiguities` non-empty, or
`_run_design_skill` finds `blocking_questions` non-empty.

**What runs.** `_advance_to_clarify(blocking_questions, next_state)` transitions to this
state and emits a conversational question (one per turn, prose not JSON).
`_handle_clarify_response(user_input)` records answers:

- Non-substantive replies (`ok`, `yes`, `no`, `continue`, etc.) are **rejected** — the
  handler re-displays the question with a prompt for a real answer or "skip"
- `skip` is accepted but flagged in logs (answer stored as "[SKIPPED — proceeding with best assumption]")
- Real answers are recorded in `_data.clarification_log` with `{question, answer, resolved_at}`

When all blocking questions are resolved, `_clarify_advance()` transitions to `next_state`
(CONFIGURE_SOURCES from CAPTURE_INTENT path, or REVIEW_DESIGN from DESIGN_SKILL path).

**LLM involvement.** None. CLARIFY is fully deterministic.

**must_show_human.** Always `True` — MCP clients must display the question to the human
and wait for a typed response.

**_SessionData changes.**
- `clarification_log: list[dict]` — audit trail of resolved questions (serialized in to_dict)
- `_clarify_questions: list[dict]` — in-flight question list with `{question, resolved, answer}`
- `_clarify_next_state: str` — state to advance to when all questions resolved

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

**What runs.** If the user provides an artifact path (filesystem or `artifact:<filename> id:<id>` format):

1. **Type check** — only `SUPPORTED_TYPES = {"pptx", "docx", "md", "txt"}` are accepted.
   Unsupported types → hard-reject turn (`must_show_human=True`), state stays at this state.
2. **Image-only check** (ADR-029 Phase 1) — calls `comparator.is_image_only(bytes, type)`.
   If the artifact contains no extractable text (image-only PPTX/DOCX), returns verbatim
   `IMAGE_ONLY_MESSAGE` with `must_show_human=True`. State does NOT advance. No vision-LLM
   fallback (ADR-029 ADR decision — text comparator only in Phase 1).
3. **Structural parse** — calls `analyze_artifact` (python-pptx/python-docx). Output is a
   **layout hint** (section order, column structure, heading hierarchy). Field names are NOT
   derived from the artifact at this state; they come from DESIGN_SKILL.
4. **Retention** (ADR-029 S5) — `_SessionData.artifact_reference_id` and
   `artifact_reference_type` are set. `artifact_reference_id` is either the ArtifactStore
   ID or a `"file:<abs_path>"` prefix for filesystem paths. These fields survive
   `to_dict`/`from_dict` (backward-compat: absent key → `None`).

If the user skips (`"no artifact"` or `"skip"`), `_data.artifact_layout` remains `None`
and `artifact_reference_id/type` are cleared to `None`.

**LLM involvement.** None — structural parse and image-only detection only.

**External I/O.** Reads artifact file from filesystem or ArtifactStore (`resolve()` → local path).

**Output / next state.** Sets `_data.artifact_layout`, `artifact_reference_id`, `artifact_reference_type`.
Transitions to DESIGN_SKILL.

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

6. **Diagnostic gate (ADR-029 Phase 1 — no longer the PROMOTE gate).** Read
   `exit_criteria` from the workflow YAML (`synthesis.exit_criteria.recall_threshold`,
   default 0.85; `synthesis.exit_criteria.faithfulness_threshold`, default 0.85).
   Compute whether thresholds are met and store in `eval_result.exit_criteria.passed`,
   but this field is now **diagnostic-only** — it does NOT block PROMOTE.
   The `eval_result.exit_criteria._note` field explains this explicitly.

7. **Surface to user.** Show auto-generated gold rows, metrics, and the disclaimer:
   "kind=auto_generated — these were created from the same LLM that did the
   extraction, so they measure consistency, not correctness. Human review
   encouraged before promoting to production fleet-wide."

8. **ArtifactComparator gap report (ADR-029 Phase 1 — the PRIMARY EVAL signal).**
   If `_data.artifact_reference_id` is set:
   - Read produced artifact bytes from the workflow output path (`wf_artifact_url`).
   - Read reference bytes from `artifact_reference_id` (strip `"file:"` prefix for
     filesystem paths, or use `artifact_store.resolve()` for ArtifactStore IDs).
   - Call `comparator.compare(ref_bytes, produced_bytes, ref_type)` → `ComparatorResult`.
   - Store `comparator_result.to_dict()` in `eval_result["comparator"]`.
   - The gap report (`structure_score`, `density_score`, `missing_sections`,
     `thin_sections`) is shown as the **primary signal** in the EVAL turn.
   - Intrinsic recall@k + faithfulness are shown below as diagnostic notes.
   If no reference artifact was uploaded, the intrinsic metrics are shown alone with
   a note that no structural comparison was possible.

9. **Terminal gate = user acceptance.** The EVAL turn options are always:
   `["accept", "ship as draft", "review design", "configure sources", "stop here"]`.
   - `"accept"` / `"promote"` / `"looks good"` → stamps `user_accepted=True` in
     `eval_result`, transitions to PROMOTE.
   - `"force promote"` → stamps `force_promoted=True`, transitions to PROMOTE
     (force-promote is checked **before** accept to avoid substring collision).
   - `"ship as draft"` → DONE immediately (no promote, no ADB write).
   - `"stop"` / `"exit"` / `"pause"` → DONE.
   - Reject / `"review design"` / `"configure sources"` / any other input →
     **ADR-029 Phase 2 (S6): failure-class classifier runs** (`_classify_and_route`).
     See ADR-029 Phase 2 section above for the six guardrails and routing map.
     If `self._llm is None`, returns an actionable error turn at EVAL (no silent skip).

10. **S6 routing-confirmation turn** (EVAL_ROUTE_PENDING internal state):
    After the classifier runs and guardrails pass, the state machine emits a routing
    turn (`must_show_human=True`) showing:
    - `failure_class` + `confidence`
    - `evidence` (must cite capability_inventory fields)
    - `why_not_alternative` (rules out the second-most-likely class)
    - `target_state` (per the routing map)
    The user must type `"confirm route to <STATE>"` to proceed. The session transitions
    state machine back to `target_state` (REVIEW_DESIGN / CONFIGURE_SOURCES / INSPECT_SOURCES)
    so the loop re-runs that segment. On DONE-as-draft paths, finalises cleanly.

**LLM involvement.**
- One `synthesis` call per sample for extraction (2-3 calls).
- One `synthesis` call as faithfulness judge (1-2 calls).

**External I/O.**
- Reads committed schema from ADB via `skill_store.read_artifact`.
- Calls `/api/v1/ask` on the local MCP server for workflow scoring.
- Writes gold set JSONL files to `eval/gold_sets/`.
- Writes updated eval_extraction + eval_workflow artifact content back to
  ADB via `skill_store.write_artifacts` (so the gold rows are durable).
- Reads produced artifact bytes from workflow output path (filesystem).
- Reads reference artifact bytes from `artifact_reference_id`.

**Output / next state.** Sets `_data.eval_result` with metrics + comparator result.
Always stays at EVAL; transitions only on explicit user acceptance (see terminal gate above).

---

## PROMOTE

ADB writes: `KBF_SKILL_SESSIONS.status`, `KBF_PERSONA_BUILDERS` upsert.

**ADR-029 Phase 1 changes (Folded Fix 2 — BUG-queue-e685d):**

1. **Invariant (a) — delta must exist.** Before calling `skill_store.promote()`,
   reads `persona_builder_delta` from ADB via `skill_store.read_artifact`. If the
   artifact is absent (root cause of BUG-queue-e685d), returns a hard-fail turn
   with `must_show_human=True` and stays at PROMOTE. The delta is what seeds the
   KB card; skipping it causes all-placeholder output.

2. **Invariant (b) — KB must be resolvable after upsert.** After
   `upsert_persona_builder_kb`, instantiates a fresh `ShimKb` and calls
   `find_kb(f"{persona}.{skill_name}")`.
   - If `ShimKb.all_cards()` is non-empty (real store) AND `find_kb` returns `None`
     → HARD-FAIL, stays at PROMOTE with `must_show_human=True`.
   - If `ShimKb.all_cards()` is empty (test env / empty store) → warning logged,
     proceed to DONE (cannot verify in test env).

**Belt-and-suspenders guard still applies:** refuses PROMOTE if
`ingest_result.status == "failed"`.

Note: `eval_result.exit_criteria.passed` is no longer a PROMOTE gate (ADR-029 S5).

---

## DONE

Terminal. Unchanged.

---

## _SessionData changes (ADR-027)

**New fields (ADR-027):**
- `normalised_intent: dict` — CAPTURE_INTENT output
- `source_samples: dict[str, list[dict]]` — cached from INSPECT_SOURCES (keyed by source_id)
- `source_capability: list[dict]` — LLM inventory from INSPECT_SOURCES
- `artifact_layout: dict | None` — structural parse from UPLOAD_ARTIFACT_EXAMPLE
- `design: dict | None` — full DESIGN_SKILL output

**New fields (ADR-028 S3):**
- `_clarify_questions: list[str] | None` — pending clarification questions
- `_clarify_next_state: str | None` — state to resume after CLARIFY
- `clarification_log: list[dict]` — Q&A log from all CLARIFY rounds

**New fields (ADR-029 Phase 1 S5):**
- `artifact_reference_id: str | None` — ArtifactStore ID or `"file:<abs_path>"` for filesystem
  paths. Set at UPLOAD_ARTIFACT_EXAMPLE; read at EVAL for comparator. `None` if user skipped.
  Backward-compat: absent key in serialised dict → `None`.
- `artifact_reference_type: str | None` — file extension without dot (e.g. `"pptx"`, `"docx"`).
  `None` if user skipped. Backward-compat: absent key in serialised dict → `None`.

**New fields (ADR-029 Phase 2 S6):**
- `eval_iteration_count: int = 0` — incremented each time the classifier runs in the reject path.
  Guardrail 4: when `>= _EVAL_MAX_ITERATIONS` (3), exits as draft before calling classifier.
  Persisted in `to_dict()` / `from_dict()`. Backward-compat default 0.
- `eval_cumulative_cost_usd: float = 0.0` — accumulates cost of classifier LLM calls (USD).
  Guardrail 5: when `> _EVAL_COST_CEILING_USD` (2.00), exits as draft before calling classifier.
  Persisted in `to_dict()` / `from_dict()`. Backward-compat default 0.0.
- `last_eval_failure_class: str | None = None` — the `failure_class` from the most recent
  classifier run. Guardrail 3: if current class == last class → pathological loop → DONE draft.
  Persisted in `to_dict()` / `from_dict()`. Backward-compat default None.
- `_eval_pending_route: str = "REVIEW_DESIGN"` — transient (not persisted). Target state
  waiting for user confirmation in EVAL_ROUTE_PENDING. Set by `_classify_and_route`.

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
| EVAL (failure classifier — S6, reject path only) | 1 per reject iteration (max 3) |
| **Total (happy path)** | **7-14** |
| **Total (3 reject iterations)** | **10-17** |

**Cost ceiling (S6 guardrail 5):** classifier calls cumulate against a $2.00 ceiling.
Classifier token budget: 512 output tokens per call (small; the prompt is ~1.5k tokens).
Estimated cost per classifier call: ~$0.01 (OCI synthesis model at current pricing).
