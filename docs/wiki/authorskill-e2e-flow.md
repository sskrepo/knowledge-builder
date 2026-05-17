---
title: authorSkill FSM — End-to-End Flow
status: reference
created: 2026-05-17
owner: architect
tags: [skill-builder, fsm, authorskill, state-machine, reference]
related: [ADR-027, ADR-028, ADR-029, ADR-030, ADR-031, ADR-032, ADR-033, ADR-034, ADR-035, ADR-038, DECISION-012, DECISION-013, DECISION-015, DECISION-017, DECISION-018]
---

# authorSkill FSM — End-to-End Flow

## Accuracy Notice

Generated from code at commit **b1adf33** (`framework/skill_builder/conversation.py`).

**DESIGN_SKILL card-gen ordering:** commit b1adf33 contains a transitional state where the card-gen call (`_generate_design_skill_card()`) appears BEFORE the design LLM call in the prior commit (78307d1). The ADR-038 / DECISION-018 locked design and the comment at conversation.py:2320-2329 both specify — and b1adf33 restores — that the correct ordering is:

1. Run the `design_skill` LLM call (produces `design`, sets `self._data.output_format` from `design["workflow_shape"]["output_format"]`)
2. Call `_generate_design_skill_card()` AFTER, so the card's `output_format` reflects the design-authoritative value, not the pre-design guess from `normalised_intent.output_kind`

The code at b1adf33 implements this correct ordering (conversation.py:2236 sets `self._data.design`, conversation.py:2314-2318 sets `self._data.output_format`, then conversation.py:2330 calls `_generate_design_skill_card()`). Any concurrent work in `conversation.py` that was mid-change during document generation should be verified against this ordering when that change lands.

---

## Summary Table

| State | LLM? (prompt key) | Key output | Human review? | Next state |
|---|---|---|---|---|
| IDENTIFY_PERSONA | No | `persona`, `skill_name` set | Yes (awaiting user) | CAPTURE_INTENT |
| CAPTURE_INTENT | Yes (`capture_intent`) | `normalised_intent`, `source_binding_mode`, `skill_name` | Yes (awaiting user or auto-advance on nice-to-know) | CLARIFY or CONFIGURE_SOURCES |
| CLARIFY | No LLM call (template only) | `clarification_log`, `source_binding_mode` resolved | Yes (`must_show_human=True` always) | CONFIGURE_SOURCES or REVIEW_DESIGN |
| CONFIGURE_SOURCES | Yes (`configure_sources`) | `sources` list | Yes (awaiting user) | INSPECT_SOURCES |
| INSPECT_SOURCES | Yes (`inspect_sources`, per source) | `source_samples`, `source_capability` | Yes (awaiting user) | UPLOAD_ARTIFACT_EXAMPLE |
| UPLOAD_ARTIFACT_EXAMPLE | No LLM | `artifact_reference_id/type/name`, `artifact_layout`, `artifact_required` | Yes (awaiting user) | DESIGN_SKILL |
| DESIGN_SKILL | Yes (`design_skill`) then Yes (`design_skill_card`) | `design`, `design_skill_card`, `fields`, `field_specs`, `output_format`, `trigger` | Yes (`must_show_human=True` for card review) | CLARIFY (if blocking_questions) or REVIEW_DESIGN |
| REVIEW_DESIGN | Optional LLM (`review_design_replan` on substantive edit) | Updated `design` | Yes (`must_show_human=True`) | CONFIGURE_TRIGGERS |
| CONFIGURE_TRIGGERS | No (deterministic parse) | `trigger`, `output_format` confirmed | Yes (awaiting user) | PREVIEW_EXTRACTION |
| PREVIEW_EXTRACTION | Yes (`review_extract` via `review_extractions`) | Extraction preview shown; `source_samples` read | Yes (`must_show_human=True`) | COMMITTED (via `_handle_commit_v2`) |
| CONFIRM | No (thin passthrough to `_handle_commit`) | Same as COMMITTED path | Yes | COMMITTED |
| COMMITTED | No | `committed_paths`, ADB + filesystem artifacts written | Yes (awaiting user) | VALIDATE |
| VALIDATE | No (deterministic link check + ADR-032 contract check) | `validation_result` | Yes (awaiting user) | INGEST |
| INGEST | No (adapter call, not LLM) | `ingest_result`, KB pages stored | Yes (awaiting user) | EVAL |
| EVAL | Yes (`eval_judge` per field per sample; `failure_classifier` on reject path) | `eval_result`, gold sets written, `routing_self_test_passed` | Yes (`must_show_human=True`) | PROMOTE or DONE (draft) or EVAL_ROUTE_PENDING |
| PROMOTE | No | `skill_store.promote()`, KB card registered, `routing_self_test_passed` hard-blocks if False | Yes (awaiting user) | DONE |
| DONE | No | Terminal | N/A | — |

**CONFIRM** is in `STATES` (position 10, conversation.py:257) but in the ADR-027 new machine the happy path does not set `self._state = "CONFIRM"` at any point — `PREVIEW_EXTRACTION` calls `_handle_commit_v2()` directly which transitions to COMMITTED. CONFIRM is reachable only if an external caller or legacy path explicitly lands there; `_handle_confirm_response` (conversation.py:3924) is a thin passthrough that calls `_handle_commit()`.

**Legacy states** (`ANALYZE_ARTIFACT`, `REVIEW_FIELDS`, `REVIEW_SCHEMA`, `CHECK_REUSE`, `PREVIEW`) are defined in `_STATES_LEGACY` (conversation.py:272-288). They are retained in the dispatch table (conversation.py:570-576) for in-flight pre-ADR-027 sessions only. New sessions never enter them.

**Internal transient state** `EVAL_ROUTE_PENDING` (conversation.py:267) is used inside the S6 routing loop but is NOT in `STATES` and is not user- or API-visible. It is only entered by `_classify_and_route()` when the classifier proposes a reroute that requires user confirmation.

---

## Per-State Walkthrough

---

### 1. IDENTIFY_PERSONA

**Entry condition:** Session `start()` called without `intent_description`, or with no persona set. Initial `self._state = "IDENTIFY_PERSONA"` set in `__init__` (conversation.py:495).

**Handler:** `_prompt_identify_persona()` (conversation.py:860) produces the initial turn; `_handle_identify_persona()` (conversation.py:878) handles user response.

**Input consumed:**
- User input: `"<persona_name> — <intent>"` or just `"<persona_name>"`
- Reads `_list_available_personas()` to validate persona names

**Processing (deterministic):**
1. Parse persona name from user input using `re.split(r"\s*[—–\-:]\s*", ...)` (conversation.py:882)
2. Validate against known personas; if unknown, re-prompt
3. Set `self._data.persona`, `self._data.synth_id = _make_synth_id(persona, created_at)` (conversation.py:896)
4. If intent is present in the same input, store `self._data.intent_description`

**LLM calls:** No LLM — deterministic.

**Outcome / artifacts produced:**
- `_SessionData.persona` set
- `_SessionData.synth_id` set
- If intent also provided: transition to CAPTURE_INTENT immediately

**Exit condition → next state:**
- If persona + intent both present: `self._state = "CAPTURE_INTENT"`, calls `_advance_to_capture_intent()`
- If persona only: stay at IDENTIFY_PERSONA, re-prompt for intent
- `awaiting_user=True` on all turns

**ADR/DECISION refs:** ADR-027 (no-stub-mode; must have LLM for subsequent states), ADR-015 (conversation contract).

---

### 2. CAPTURE_INTENT

**Entry condition:** Persona and intent text both present. Transition from IDENTIFY_PERSONA via `_advance_to_capture_intent()` (conversation.py:945), or via `start()` when both are supplied directly.

**Handler:** `_advance_to_capture_intent()` (auto-running LLM call, not a user-response handler); `_handle_capture_intent()` (conversation.py:1105) handles subsequent user confirmation or amendment.

**Input consumed:**
- `self._data.intent_description` (raw text)
- `self._data.persona` (for prompt overlay resolution)
- PromptRegistry overlay: `persona_key_fields` from `persona_overlays.yaml` (MissingVarsError degrades gracefully to empty string, conversation.py:967-986)

**Processing:**

*Deterministic (before LLM):*
- Fetch persona fragment vars from PromptRegistry overlay

*LLM call 1 — `capture_intent` prompt:*
- Called at conversation.py:987-1003
- Output parsed from JSON; strip markdown fences with regex (conversation.py:995-997)

*Deterministic (after LLM):*
1. Derive `skill_name` slug from `scope_domains` + `output_kind` (conversation.py:1006-1015)
2. Store `self._data.normalised_intent = normalised` (conversation.py:1017)
3. Persist `source_binding_mode` and `source_binding_signal` from LLM output (conversation.py:1030-1036); backward-compat default `"author_fixed"` if key absent
4. Classify `blocking_ambiguities` vs `nice_to_know_ambiguities` (conversation.py:1039-1047)
5. If ambiguities contain the source-binding question pattern (`"source page fixed at authoring time or supplied"`), annotate question dict with `context="source_binding_mode"` (conversation.py:1058-1066)
6. Route: blocking → CLARIFY; nice-to-know only → auto-advance to CONFIGURE_SOURCES; zero ambiguities → show confirmation turn, `awaiting_user=True`

**LLM call details:**

| Prompt key | `required_vars` | Key inputs | Expected output shape |
|---|---|---|---|
| `capture_intent` | `[persona, intent, persona_key_fields]` | raw intent text, persona, persona key fields from overlay | JSON: `{output_kind, audience, cadence, scope_domains, success_criteria, blocking_ambiguities, nice_to_know_ambiguities, source_binding_mode, source_binding_signal}` |

(conversation.py:969-973; skill_builder.yaml lines 23-81)

**Outcome / artifacts produced:**
- `_SessionData.normalised_intent` — structured goal object
- `_SessionData.skill_name` — slugified candidate name
- `_SessionData.source_binding_mode` — `"author_fixed"` | `"ask_parameterized"` | `"ambiguous"`
- `_SessionData.source_binding_signal` — one-line evidence string
- `_SessionData._clarify_questions` — if routing to CLARIFY

**Exit condition → next state:**
- `blocking_ambiguities` non-empty → CLARIFY (with `_clarify_next_state = "CONFIGURE_SOURCES"`)
- `nice_to_know_ambiguities` only → auto-advance to CONFIGURE_SOURCES (no user confirmation required)
- Zero ambiguities → CAPTURE_INTENT confirmation turn shown; user types `"ok"` → CONFIGURE_SOURCES
- User amendment at confirmation turn → re-run `_advance_to_capture_intent()` with amended intent (conversation.py:1117-1118)
- `must_show_human=False` when `blocking_ambiguities` is empty (conversation.py:1101)
- `must_show_human=True` only at CLARIFY turns (not at CAPTURE_INTENT confirmation turn)

**ADR/DECISION refs:** ADR-027, ADR-028 (S3 — blocking vs nice-to-know distinction), ADR-030 (prompt registry), ADR-032 (source_binding_mode classification).

---

### 3. CLARIFY

**Entry condition:** Invoked from CAPTURE_INTENT (when `blocking_ambiguities` non-empty) or from DESIGN_SKILL (when `design.blocking_questions` non-empty). Entered via `_advance_to_clarify(blocking_questions, next_state)` (conversation.py:1148).

**Handler:** `_handle_clarify_response()` (conversation.py:1212).

**Input consumed:**
- `self._data._clarify_questions` — list of `{question, resolved, answer?, context?, options?}` dicts, restored across ADB round-trips (BUG-queue-f0591 fix; conversation.py:382-386)
- `self._data._clarify_next_state` — `"CONFIGURE_SOURCES"` or `"REVIEW_DESIGN"` (persisted in `to_dict()`/`from_dict()`)
- User answer text

**Processing (deterministic):**
1. ADR-034 guard: `_sanitize_clarify_question()` (conversation.py:1125-1146) strips internal layout preset IDs from question text before storing or displaying
2. Reject non-substantive single-word replies (`_NON_ANSWERS` set at conversation.py:1207-1210); re-ask the question
3. BUG-queue-f4987: detect early `"artifact:<filename> id:<artifact_id>"` syntax; stash in `_pending_artifact_stash`, re-ask question (conversation.py:1260-1299)
4. Mark first unresolved question as answered; if `context == "source_binding_mode"`, resolve `source_binding_mode` to `"author_fixed"` or `"ask_parameterized"` using keyword matching (conversation.py:1329-1368)
5. Append to `clarification_log` for audit trail
6. If more questions remain → re-enter CLARIFY with updated list
7. If all resolved → `_clarify_advance()` (conversation.py:1419)

**LLM calls:** No LLM call. The `clarify` prompt in `skill_builder.yaml` is a text template only (`model: none`); the call site uses `spec.text` directly without calling `llm.chat` (conversation.py:1183). Deterministic throughout.

**Outcome / artifacts produced:**
- `_SessionData.clarification_log` — audit trail entries appended
- `_SessionData.source_binding_mode` — resolved from `"ambiguous"` to `"author_fixed"` or `"ask_parameterized"` if the source-binding question was answered
- `_SessionData._pending_artifact_stash` — set if user supplied early artifact reference

**Exit condition → next state:**
- All questions resolved AND `_clarify_next_state == "CONFIGURE_SOURCES"` → `_advance_to_configure_sources_v2()`
- All questions resolved AND `_clarify_next_state == "REVIEW_DESIGN"` → `_prompt_review_design()`
- BUG-queue-f0591 hardening: if design is set but `_clarify_questions` is empty (persistence regression), surface `must_show_human=True` error instead of silently rewinding (conversation.py:1388-1406)
- All turns: `must_show_human=True`, `awaiting_user=True` (conversation.py:1200-1202)
- Unexpected `_clarify_next_state` value: falls back to CONFIGURE_SOURCES with a warning (conversation.py:1437-1441)

**ADR/DECISION refs:** ADR-028 (S3 — CLARIFY state design), ADR-032 (source_binding_mode resolution), ADR-034 (layout preset ID sanitization).

---

### 4. CONFIGURE_SOURCES

**Entry condition:** From CLARIFY (all blocking questions resolved, next_state = CONFIGURE_SOURCES) or directly from CAPTURE_INTENT (no blocking ambiguities). Entered via `_advance_to_configure_sources_v2()` (conversation.py:1445).

**Handler:** `_handle_configure_sources_response()` (conversation.py:3807).

**Input consumed:**
- `self._data.normalised_intent` — for LLM source proposal
- `self._data.intent_description` — for auto-extraction of Confluence references
- `self._data.persona` — for persona adapter list (`_get_persona_adapters()`)
- User additions/corrections

**Processing:**

*Deterministic (before LLM):*
1. Auto-extract Confluence URLs/page IDs from intent text if `sources` is empty (conversation.py:1450-1453)
2. Build `adapter_list` from persona builder YAML (conversation.py:1547-1563)

*LLM call — `configure_sources` prompt:*
- Called at conversation.py:1465-1491

*Deterministic (after LLM):*
1. Merge proposed sources with auto-extracted ones, deduplicating by page (conversation.py:1494-1507)
2. If no sources after merge: prompt user to supply manually
3. Show proposed source list with rationale; wait for user to confirm or add more
4. User input `"done"` triggers inspection; additional URLs/descriptors are parsed via `_parse_source_descriptor()` and appended

**LLM call details:**

| Prompt key | `required_vars` | Key inputs | Expected output shape |
|---|---|---|---|
| `configure_sources` | `[persona, normalised_intent, adapter_list, intent_text]` | normalised_intent JSON, adapter list JSON, raw intent | JSON array of `{kind, pages?, space?, labels?, jql?, rationale}` |

(conversation.py:1465; skill_builder.yaml lines 86-124)

**Outcome / artifacts produced:**
- `_SessionData.sources` — list of source descriptor dicts populated/extended
- `awaiting_user=True`; `must_show_human=False`

**Exit condition → next state:**
- User types `"done"` → if `normalised_intent` is set (new machine): `_run_inspect_sources()` → INSPECT_SOURCES
- User types `"done"` → if no `normalised_intent` (legacy machine): CONFIGURE_TRIGGERS (conversation.py:3814-3816)
- Otherwise: stay at CONFIGURE_SOURCES, add parsed source to `self._data.sources`

**ADR/DECISION refs:** ADR-027 (INSPECT_SOURCES before design), ADR-032 (sources carry space info needed for `space_allow_list` derivation at COMMIT).

---

### 5. INSPECT_SOURCES

**Entry condition:** From CONFIGURE_SOURCES when user types `"done"`. Entered via `_run_inspect_sources()` (conversation.py:1582) — this is a transition action, not a response handler.

**Handler:** `_handle_inspect_sources_response()` (conversation.py:1567) for user confirmation; `_run_inspect_sources()` runs automatically on entry.

**Input consumed:**
- `self._data.sources` — source descriptors to inspect
- `KBF_ENV` environment variable (for adapter selection)
- `self._data.normalised_intent` (injected into capability analysis prompt)

**Processing:**

*Deterministic:*
1. For each Confluence source entry, collect page IDs/URLs to inspect (up to 2 per source entry)
2. Hard-fail if `fetch_samples()` raises (no fallback to synthetic samples per ADR-027; conversation.py:1635-1642)

*LLM call — `inspect_sources` prompt per source page:*
- Called at conversation.py:1663-1686
- Per-sample cap: 20,000 chars; total cap: 40,000 chars (raised from old 3k/6k per ADR-031 C6; conversation.py:1651-1659)
- Response parsed via `_parse_llm_json_response()` (truncation detection active; conversation.py:1680-1684)

*Deterministic (after LLM):*
1. Store `source_samples_cache` → `self._data.source_samples` (keyed `"confluence:{source_id}"`)
2. Store `capability_list` → `self._data.source_capability`
3. Format capability inventory for user display

**LLM call details:**

| Prompt key | `required_vars` | Key inputs | Expected output shape |
|---|---|---|---|
| `inspect_sources` | `[source_id, persona, normalised_intent, sample_content]` | source sample text (up to 40k chars), intent JSON | JSON: `{source_id, available_fields[{field,type,confidence,evidence}], missing_fields, suggested_fields, summary}` |

Confidence taxonomy: `high` | `medium` | `synthesisable` | `low` (skill_builder.yaml lines 165-178).

**Outcome / artifacts produced:**
- `_SessionData.source_samples` — `{cache_key: [sample_dict]}` cached for PREVIEW_EXTRACTION and EVAL
- `_SessionData.source_capability` — per-source capability inventory dicts (feeds DESIGN_SKILL)
- Turn shown with field availability summary; `awaiting_user=True`

**ADR/DECISION refs:** ADR-027 (sources inspected before design; no synthetic fallback), ADR-031 (C6: raised sample caps; C3: truncation detection), ADR-035 (access-verify gate — source access must succeed before DESIGN is permitted; hard-fail on fetch failure enforces this).

---

### 6. UPLOAD_ARTIFACT_EXAMPLE

**Entry condition:** From INSPECT_SOURCES when user confirms. Entered via `_advance_to_upload_artifact_example()` (conversation.py:1752).

**Handler:** `_handle_upload_artifact_example()` (conversation.py:1822).

**Input consumed:**
- `self._data.normalised_intent` + `self._data.output_format` — for `_is_artifact_required()` (conditional-required rule)
- `self._data._pending_artifact_stash` — BUG-queue-f4987: early artifact reference stashed at CLARIFY
- User input: `"skip"` | `"artifact:<filename> id:<artifact_id>"` | local file path

**Processing (deterministic):**

ADR-035 three-path logic:

1. **Skip path:** If `_pending_artifact_stash` exists and user sends `"skip"`, auto-apply the stash (rewrite `user_input`; conversation.py:1856-1872). If no stash and no existing binding and artifact not required: `_clear_reference_artifact()` + advance to DESIGN_SKILL. If binding already exists and user skips: RE-ENTRY GUARD preserves existing binding (conversation.py:1883-1892).

2. **Artifact path (`artifact:` prefix or filesystem path):** Resolve artifact ID via `ArtifactStore.resolve()` or filesystem; read bytes. Image-only hard-reject: `comparator.is_image_only()` checked BEFORE `analyze_artifact()` (conversation.py:2046-2071). Text-bearing: call `analyze_artifact()` → get `fields` + `mapping` → call `_bind_reference_artifact()` atomically (conversation.py:2082-2088).

3. **Conditional-required gate (ADR-035):** When `artifact_required=True`, the skip option is suppressed and skip attempts are blocked (conversation.py:1893-1913). Artifact is required when `output_kind` is `pptx`/`docx`, OR intent text contains template/reference keywords (conversation.py:843-854).

**LLM calls:** No LLM — deterministic.

**Outcome / artifacts produced:**
- `_SessionData.artifact_reference_id` — ArtifactStore key or `"file:<abs_path>"`
- `_SessionData.artifact_reference_type` — `"pptx"` | `"docx"` | `"md"` | `"txt"`
- `_SessionData.artifact_reference_name` — filename
- `_SessionData.artifact_layout` — `{sections, slide_count, mapping}` from `analyze_artifact()`
- `_SessionData.artifact_required` — cached boolean
- `_SessionData._pending_artifact_stash` — cleared on consumption
- `must_show_human=True` on error turns (invalid type, image-only); standard `awaiting_user=True` otherwise

**Exit condition → next state:** On any successful path (skip accepted, stash applied, or artifact parsed and bound): `_run_design_skill()` → DESIGN_SKILL.

**ADR/DECISION refs:** ADR-027 (artifact optional at this stage), ADR-029 (S5 — image hard-reject; artifact retention), ADR-035 (DECISION-015 — re-entry guard, conditional-required gate, single-source-of-truth binding via `_bind_reference_artifact()` / `has_bound_reference_artifact()`).

---

### 7. DESIGN_SKILL

**Entry condition:** From UPLOAD_ARTIFACT_EXAMPLE (skip or bound artifact). Entered via `_run_design_skill()` (conversation.py:2127) — auto-running transition action.

**Handler:**
- `_run_design_skill()` (conversation.py:2127) — automatic on entry
- `_generate_design_skill_card()` (conversation.py:2362) — called within `_run_design_skill()`
- `_prompt_review_skill_card()` (conversation.py:2464) — surfaces card to author
- `_handle_design_skill_response()` (conversation.py:2513) — handles user card review/edit/confirm

**Input consumed:**
- `self._data.normalised_intent`, `source_capability`, `artifact_layout` (may be null)
- Existing KB cards from `ShimKb.cards_visible_to(persona)` (capped at 10; conversation.py:2148)
- Layout preset catalog from `_layout_catalog_for_prompt(output_fmt_hint)` (ADR-034; conversation.py:2176)
- PromptRegistry persona overlay: `persona_key_fields`, `persona_extraction_style`, `persona_few_shot_example`

**Processing — ORDERING IS CRITICAL (ADR-038 / DECISION-018):**

1. **LLM call 1 — `design_skill` prompt** (conversation.py:2179-2226): full design LLM call
2. **Validate** `design` output: must have `schema.properties` (conversation.py:2230-2234)
3. **Set session fields from design** (conversation.py:2236-2318):
   - `self._data.design = design`
   - `self._data.fields = list(properties.keys())`
   - `self._data.field_specs = {f: dict(spec) for f, spec in properties.items()}`
   - Filter `reuse_plan` against real ShimKb to drop hallucinated KB references (conversation.py:2254-2311)
   - `self._data.reuse_result = validated_reuse_plan`
   - `self._data.output_format = ws.get("output_format", "markdown")` — design-authoritative value set HERE
   - `self._data.trigger = ws.get("trigger", ...)`
4. **LLM call 2 — `design_skill_card` prompt** (conversation.py:2330, via `_generate_design_skill_card()`): called AFTER step 3 so card uses design-authoritative `output_format`, not pre-design guess. Fallback to minimal card on failure (never halts FSM)
5. **Route to CLARIFY** if `design.blocking_questions` non-empty (conversation.py:2350-2356); `next_state="REVIEW_DESIGN"`
6. **Otherwise** show card review turn (`_prompt_review_skill_card()`, conversation.py:2360)

**LLM call details:**

| # | Prompt key | `required_vars` | Key inputs | Expected output shape |
|---|---|---|---|---|
| 1 | `design_skill` | `[persona, normalised_intent, source_capability, artifact_layout, existing_kb_cards, persona_key_fields, persona_extraction_style, persona_few_shot_example, layout_preset_catalog]` | intent JSON, capability JSON, layout hint JSON (or "null"), existing cards JSON, persona overlay vars, catalog text | JSON: `{schema{title,properties,required}, source_bindings, workflow_shape{output_format,layout,layout_rationale,trigger,retriever}, reuse_plan{covered,gaps}, unsupportable_fields, blocking_questions, open_questions, source_binding_mode}` |
| 2 | `design_skill_card` | `[skill_name, persona, task_description, output_format, intent_summary]` | skill name, persona, intent text (<=500 chars), design-authoritative output_format, intent JSON (<=1000 chars) | JSON: `{summary, use_when, example_invocations[2], routing_queries{positive[5], negative[3]}}` |

(skill_builder.yaml lines 191-302 for `design_skill`; lines 408-463 for `design_skill_card`)

**Outcome / artifacts produced:**
- `_SessionData.design` — full design object
- `_SessionData.fields`, `_SessionData.field_specs` — schema field list and specs
- `_SessionData.reuse_result` — validated reuse plan (hallucinated KB refs dropped)
- `_SessionData.output_format` — design-authoritative output format
- `_SessionData.trigger` — trigger configuration
- `_SessionData.design_skill_card` — consumer-facing card with `routing_queries`; persisted via `to_dict()`/`from_dict()`
- Card review turn shown with `must_show_human=True`, `awaiting_user=True`
- Author may confirm (`"ok"`) or submit JSON edits to the card (conversation.py:2543-2562)

**Exit condition → next state:**
- `design.blocking_questions` non-empty → CLARIFY (`next_state="REVIEW_DESIGN"`)
- No blocking questions → card review turn shown at DESIGN_SKILL state; user confirms → `_prompt_review_design()` → REVIEW_DESIGN
- Session restored without design (`design is None`): re-run `_run_design_skill()` (conversation.py:2524-2525)
- Session restored without card (`design_skill_card is None`): skip card review, go directly to `_prompt_review_design()` (backward compat; conversation.py:2530-2531)

**ADR/DECISION refs:** ADR-027 (integrated DESIGN_SKILL LLM call), ADR-028 (CLARIFY after design for blocking questions), ADR-030 (prompt registry; persona overlays), ADR-031 (C2: truncation detection on design call — hard-fail on truncation), ADR-034 (layout preset catalog injected; human_label sanitization in CLARIFY guard), ADR-038 / DECISION-018 (card generation at DESIGN_SKILL; `routing_queries`; `must_show_human` card review gate).

---

### 8. REVIEW_DESIGN

**Entry condition:** From DESIGN_SKILL (card confirmed) or from CLARIFY when `_clarify_next_state = "REVIEW_DESIGN"`. Entered via `_prompt_review_design()` (conversation.py:2571).

**Handler:** `_handle_review_design_response()` (conversation.py:2659).

**Input consumed:**
- `self._data.design` — full design object
- `has_bound_reference_artifact()` — single-source-of-truth check (conversation.py:2609-2615); NEVER reads `design.workflow_shape.layout` text for artifact-bound signal (ADR-035)
- User edit commands or `"ok"`

**Processing:**

*Deterministic (trivial edits via `_apply_design_patch()`, conversation.py:2675):*
- `describe <field> as <text>` — update `properties[fname]["description"]`
- `set type of <field> to <type>` — update `properties[fname]["type"]`
- `rename field <old> to <new>` — rename in properties, fields list, field_specs, source_bindings
- `remove field <name>` — remove from all structures
- `set trigger to <cron>` — update `workflow_shape.trigger`
- Any successful trivial edit re-renders the design turn (no LLM)

*LLM call — `review_design_replan` on substantive edits:*
- Called at conversation.py:2751-2826 when `_apply_design_patch()` returns None
- Applies diff returned by LLM to `design` in-place

**LLM call details (substantive edit path only):**

| Prompt key | `required_vars` | Key inputs | Expected output shape |
|---|---|---|---|
| `review_design_replan` | `[current_design, edit_request, updated_source_capability]` | current design JSON, user edit text | JSON diff: `{schema_add, schema_remove, schema_update, source_bindings_add, source_bindings_remove, workflow_shape_update, reuse_plan_update, open_questions}` |

(skill_builder.yaml lines 306-341; ADR-031 C4: truncation detection active)

**Outcome / artifacts produced:**
- Updated `self._data.design`, `fields`, `field_specs`, `reuse_result`, `trigger` (in-memory; persisted via `to_dict()`)
- `must_show_human=True` (always; conversation.py:2655)
- `awaiting_user=True`

**Exit condition → next state:** User types `"ok"` / `"looks good"` / `"continue"` / `"yes"` / `"proceed"` → `_advance_to_configure_triggers()` → CONFIGURE_TRIGGERS.

**ADR/DECISION refs:** ADR-027 (REVIEW_DESIGN is mandatory human gate), ADR-028 (Item 2: `must_show_human` always True), ADR-031 (C4: replan truncation detection), ADR-035 (reference artifact binding shown from `has_bound_reference_artifact()` only — single source of truth).

---

### 9. CONFIGURE_TRIGGERS

**Entry condition:** From REVIEW_DESIGN (user confirms design). Entered via `_advance_to_configure_triggers()` (conversation.py:3832).

**Handler:** `_handle_configure_triggers_response()` (conversation.py:3867).

**Input consumed:**
- `(self._data.design or {}).get("workflow_shape", {})` — DESIGN_SKILL's trigger/output_format proposal shown as default
- User choice: `"ok"` (accept proposal) or override string (e.g. `"3, pptx, 0 16 * * 5"`)

**Processing (deterministic):**
1. If `"ok"`: adopt `design.workflow_shape.trigger` and `design.workflow_shape.output_format`
2. If override: parse via `_parse_trigger_input(user_input)` → set `self._data.trigger` and `self._data.output_format`

**LLM calls:** No LLM — deterministic.

**Outcome / artifacts produced:**
- `_SessionData.trigger` — confirmed/overridden trigger config
- `_SessionData.output_format` — confirmed/overridden output format
- `awaiting_user=True`

**Exit condition → next state:**
- If `source_capability` or `source_samples` populated (new machine): `_advance_to_preview_extraction()` → PREVIEW_EXTRACTION (conversation.py:3886-3887)
- If neither (legacy machine): `_advance_to_preview()` → PREVIEW (legacy state; conversation.py:3888)

**ADR/DECISION refs:** ADR-027 (trigger confirmed after design; new machine paths to PREVIEW_EXTRACTION).

---

### 10. PREVIEW_EXTRACTION

**Entry condition:** From CONFIGURE_TRIGGERS (new machine). Entered via `_advance_to_preview_extraction()` (conversation.py:2864) — auto-running transition action.

**Handler:** `_handle_preview_extraction_response()` (conversation.py:2949).

**Input consumed:**
- `self._data.source_samples` — cached from INSPECT_SOURCES (up to 3 samples used; conversation.py:2906)
- `self._data.fields` — ALL designed fields (not just `reuse_result.gaps`; conversation.py:2893)
- `self._data.field_specs`

**Processing:**

*Deterministic:*
1. Build schema from ALL fields + field_specs via `synthesize_extraction_schema()` (conversation.py:2894-2897)
2. Hard-fail if `all_samples` is empty (no synthetic fallback per ADR-027; conversation.py:2881-2886)

*LLM call — `review_extract` prompt (via `review_extractions()` in review.py):*
- Up to 3 samples passed (conversation.py:2906-2907)
- ContentFilterRejection from LLM provider surfaces as clean `must_show_human=True` turn (state NOT advanced; conversation.py:2908-2909)

*Deterministic (after LLM):*
1. Format extraction results for display (per-sample: extracted fields, missing fields, field coverage %, issues)
2. Show preview turn

**LLM call details:**

| Prompt key | `required_vars` | Key inputs | Expected output shape |
|---|---|---|---|
| `review_extract` | `[field_lines, text]` | field descriptions formatted as lines, source document text | JSON object `{field_name: extracted_value, ...}` |

(skill_builder.yaml lines 625-648; called from review.py `review_extractions()`)

**Outcome / artifacts produced:**
- Extraction preview shown to user (live data from real sources)
- `must_show_human=True`, `awaiting_user=True` (conversation.py:2944-2946)

**Exit condition → next state:**
- `"ok"` / `"commit"` / `"yes"` / `"looks good"` / `"proceed"` → `_handle_commit_v2()` → calls `_synthesize_preview()` then `_handle_commit()` → COMMITTED
- `"back"` / `"design"` → `_prompt_review_design()` → REVIEW_DESIGN
- Content-filter recovery: `"change sources"` → CONFIGURE_SOURCES; `"stop"` → DONE
- CONFIRM state: `_handle_commit_v2()` is called from PREVIEW_EXTRACTION directly; CONFIRM state (in `STATES` list at position 10) is NOT entered in the new machine's happy path. `_handle_confirm_response` is a thin passthrough that calls `_handle_commit()` and is only reached if a session is explicitly set to state `"CONFIRM"` by an external caller.

**ADR/DECISION refs:** ADR-027 (live extraction preview with real data; no synthetic fallback), ADR-028 (Item 2: `must_show_human=True`), ADR-029 (ContentFilterRejection surface).

---

### 11. CONFIRM (in STATES list — thin passthrough)

**Entry condition:** Not entered in new machine happy path. In `STATES` list at position 10 (conversation.py:257). `_handle_confirm_response` is in the dispatch table (conversation.py:560).

**Handler:** `_handle_confirm_response()` (conversation.py:3924) — one line: `return self._handle_commit()`.

**Note:** The new ADR-027 machine has PREVIEW_EXTRACTION call `_handle_commit_v2()` directly, bypassing CONFIRM. CONFIRM is present in `STATES` for completeness and backward compat. If `self._state` is externally set to `"CONFIRM"`, any user input triggers `_handle_commit()`.

---

### 12. COMMITTED

**Entry condition:** From PREVIEW_EXTRACTION via `_handle_commit_v2()` → `_handle_commit()` (conversation.py:2969-2974, 3927). Transition action writes all artifacts.

**Handler:** `_handle_committed_response()` (conversation.py:3965).

**Input consumed:**
- `self._data.synthesized_artifacts` — assembled by `_synthesize_preview()` (conversation.py:2972)
- `self._skill_store` — required (raises ValueError if None; __init__ enforces this)

**Processing (deterministic — `_synthesize_preview()` and `_write_artifacts()`):**

`_synthesize_preview()` (conversation.py:5985) assembles:
1. Extraction schema JSON (via `synthesize_extraction_schema()`)
2. Persona builder delta YAML (via `synthesize_persona_builder_diff()`)
3. Extraction gold set JSONL (via `seed_gold_set()`)
4. Workflow skill YAML (via `synthesize_workflow_skill()`; ADR-032: includes `source_binding` block for `ask_parameterized`)
5. **ADR-038 §B carry-through (LOAD-BEARING):** `wf_struct["skill_card"]` is OVERWRITTEN with `self._data.design_skill_card` (including `routing_queries`) if present (conversation.py:6084-6094). Static `_build_skill_card()` template does NOT win.
6. Workflow gold set JSONL (via `seed_workflow_gold()`)

`_write_artifacts()` (conversation.py:6115):
1. Serialize all artifacts to text
2. Write to filesystem
3. Write typed artifacts to `skill_store.write_artifacts()` (ADB) — HARD-FAIL if ADB write fails; session stays at PREVIEW (conversation.py:6171-6175)

**LLM calls:** No LLM — deterministic synthesis.

**Outcome / artifacts produced:**

| Artifact type | Path | ADB artifact_type |
|---|---|---|
| Extraction schema | `framework/parsers/schemas/{persona}/{skill_name}/v1.json` | `extraction_schema` |
| Persona builder delta | `framework/persona_builders/{persona}.yaml.new_kb` | `persona_builder_delta` |
| Extraction gold set | `eval/gold_sets/{persona}-{skill_name}-extraction.jsonl` | `eval_extraction` |
| Workflow skill YAML | `framework/workflow_skills/{persona}/{skill_name}.yaml` | `workflow_skill` |
| Workflow gold set | `eval/gold_sets/{persona}-{skill_name}-workflow.jsonl` | `eval_workflow` |

- `_SessionData.committed_paths` — list of relative paths written
- `_SessionData.synthesized_artifacts` — `{rel_path: content}` dict (retained for VALIDATE)

**Exit condition → next state:**
- `"stop"` / `"later"` / `"no"` / `"done"` / `"exit"` → DONE (session paused)
- `"just validate"` or any other input → `_run_validate()` → VALIDATE

**ADR/DECISION refs:** ADR-015 (ADB as source of truth), ADR-032 (source_binding block emission; `derive_space_allow_list()`), ADR-038 (skill_card carry-through; `routing_queries` preserved in ADB artifact).

---

### 13. VALIDATE

**Entry condition:** From COMMITTED (user confirms pipeline). Entered via `_run_validate()` (conversation.py:3983) — auto-running transition action.

**Handler:** `_handle_validate_response()` (conversation.py:4217).

**Input consumed:**
- `workflow_skill` artifact from `skill_store.read_artifact()` (ADB-backed; falls back to filesystem; conversation.py:3998-4032)
- `persona_builder_delta` from ADB or filesystem `.new_kb` fallback (conversation.py:4057-4105); merged into temp persona-builder dir for link validation
- `self._data.source_binding_mode` (for ADR-032 contract check)
- `self._data.synthesized_artifacts` (for in-memory YAML read during source_binding check; conversation.py:4139-4156)

**Processing (deterministic):**

1. **ADR-017 link validation** via `validate_workflow_links(wf_path_str, merged_pb_dir_str)` (conversation.py:4108): checks that all `requires_extractions` KB names are known in the persona builder index
2. **ADR-032 source_binding contract validation** via `_validate_source_binding_contract()` (conversation.py:4158): for `ask_parameterized` mode, validates all 6 required fields (`mode`, `input_param`, `ingest_on_demand`, `source_type`, `space_allow_list`, `ephemeral_ttl_seconds`) are present and that `input_param` matches a declared trigger input. For `author_fixed`, ensures YAML does NOT declare `ask_parameterized`.
3. **Confluence adapter availability check** (conversation.py:4163-4179): if `ask_parameterized + ingest_on_demand=true`, verify adapter is configured for target env via `_check_confluence_adapter_available()`. Hard-fail if missing.
4. Merge all errors; set `validation_result.passed`

**LLM calls:** No LLM — deterministic.

**Outcome / artifacts produced:**
- `_SessionData.validation_result` — `{passed, errors}`
- `awaiting_user=True`

**Exit condition → next state:**
- Passed: `_run_ingest()` → INGEST (or user may `"skip to eval"` → EVAL, or `"stop"`)
- Failed: show errors; `"retry"` → re-run VALIDATE; `"skip"` → INGEST anyway; `"stop"` → DONE

**ADR/DECISION refs:** ADR-017 (link validation), ADR-032 (P1-D source_binding contract — hard-fail discipline: never silently downgrade to author_fixed).

---

### 14. INGEST

**Entry condition:** From VALIDATE (user confirmed or skipped). Entered via `_run_ingest()` (conversation.py:4232) — auto-running.

**Handler:** `_handle_ingest_response()` (conversation.py:4420).

**Input consumed:**
- `self._data.sources` — Confluence sources
- `KBF_ENV` (selects live adapter vs fixture mode)
- `_build_confluence_adapter(kbf_env, REPO_ROOT)` — re-exported from `adapters.confluence.factory` (conversation.py:54)

**Processing (deterministic — adapter calls, no LLM):**
1. If no Confluence sources: skip with `status="completed", mode="stub"` (conversation.py:4241-4264)
2. Build `ConfluenceWikiIngestor` with `WikiMetadataStore` (conversation.py:4276-4285)
3. For each Confluence source: `ingestor.ingest_pages()` or `ingestor.ingest_space()` (conversation.py:4303-4360)
4. Zero-pages-returned treated as HARD-FAIL (conversation.py:4333-4350; fixes silent KB-empty promotion)
5. Any failed source blocks promotion; session stays at INGEST (conversation.py:4374-4397)

**LLM calls:** No LLM — adapter calls only.

**Outcome / artifacts produced:**
- `_SessionData.ingest_result` — `{status, items_processed, items_upserted, pages_new, pages_updated, pages_unchanged, mode, failures?}`
- KB pages written to WikiMetadataStore (populates retrieval layer)
- `awaiting_user=True`

**Exit condition → next state:**
- Success: `_run_eval()` → EVAL
- Failed: stay at INGEST; `"retry ingestion"` → re-run; `"stop"` → DONE (promotion blocked until INGEST succeeds; conversation.py:4429-4440)

**ADR/DECISION refs:** ADR-027 (INGEST before EVAL), ADR-032 (Confluence adapter factory re-exported).

---

### 15. EVAL

**Entry condition:** From INGEST (user confirms eval). Entered via `_run_eval()` (conversation.py:4446) — auto-running.

**Handler:** `_handle_eval_response()` (conversation.py:5204); on reject path: `_classify_and_route()` (conversation.py:5331); confirmation handler: `_handle_eval_route_confirm()` (conversation.py:5626).

**Input consumed:**
- `self._data.source_samples` — cached samples (re-fetched if empty; conversation.py:4480-4513)
- `extraction_schema` from ADB (falls back to in-memory field_specs; conversation.py:4522-4548)
- `self._data.design_skill_card.routing_queries` — for Path B self-test
- `workflow_skill` artifact from ADB — for Path A in-process execution
- `has_bound_reference_artifact()` — single-source-of-truth for comparator gate (ADR-035)

**Processing — three axes (ADR-038 §B):**

**INGEST-or-later gate (ADR-038 §B.4):** Entering state captured before mutation; if `_entering_state in {"COMMITTED", "VALIDATE"}`, raise RuntimeError (hard-fail, not silent skip; conversation.py:4668-4675).

**Path A — in-process execution (ADR-038 §B.2):**
1. Load `workflow_skill` YAML from ADB (conversation.py:4768-4772)
2. Build canonical question from `scope_domains` (conversation.py:4774-4775)
3. `WorkflowExecutor.execute_from_config(wf_cfg, exec_inputs)` (conversation.py:4779): in-process execution bypassing promoted-only router (no HTTP call)
4. Record `execution_status`, `wf_artifact_url`, `ask_latency_ms`; execution failure is HIGH-severity, NOT collapsed to soft note (conversation.py:4799-4805)

**Path B — routing self-test (ADR-038 §B.3):**
1. Load `ShimWorkflows(wf_dir, skill_store=self._skill_store)` (conversation.py:4689-4694)
2. For each positive query: `_shim.resolve_only(q, scope="ingest_or_later")` must return `skill_id == this_skill_id` at tier 1
3. For each negative query: must NOT resolve to this skill
4. `routing_self_test_passed = True` by default; set `False` on any assertion failure (conversation.py:4679-4755)
5. If no `positive_queries`: self-test skipped; `routing_self_test_passed` stays True (conversation.py:4746-4753)

**Extraction scoring (Path A — diagnostic):**
1. `_llm_extract()` per sample (up to 3; conversation.py:4555-4587)
2. Compute `recall_at_k` (field hit rate; conversation.py:4596-4604)
3. Faithfulness: LLM judge per field per sample using `eval_judge` prompt (conversation.py:4618-4651)
4. ContentFilterRejection surfaces as clean `must_show_human=True` turn (conversation.py:4558-4561)

**Comparator (ADR-029 §B.3):**
- `ArtifactComparator.compare(ref_bytes, produced_artifact_bytes, ref_type)` if both available (conversation.py:4960-4974)
- Gate: `has_bound_reference_artifact()` used for ref check (ADR-035 single-source-of-truth)

**Gold set write (Step 7):**
- Extraction gold rows: `eval/gold_sets/{persona}-{skill_name}-extraction.jsonl` (filesystem + ADB)
- Workflow gold row: includes Path A/B results (conversation.py:4851-4845)
- `eval_iteration_count`, `eval_cumulative_cost_usd` incremented on each classifier call

**LLM call details:**

| Prompt key | `required_vars` | When called | Expected output shape |
|---|---|---|---|
| `eval_judge` | `[field_name, field_description, extracted_value, source_snippet]` | Per field per extraction sample (faithfulness) | JSON: `{faithful, confidence, reason}` |
| `failure_classifier` (gate-locked) | `[normalised_intent, schema_properties, capability_inventory, gap_report, missing_sections, thin_sections]` | Only on S6 reject path (`_classify_and_route()`) | JSON: `{failure_class, confidence, evidence, alternative_class, why_not_alternative}` |

`failure_classifier` is gate-locked (`locked: true`, checksum: `sha256:aef837cdde856fe83039f19fff816a101fe886187a7ce6f741a39eaab71c1d1f`; skill_builder.yaml lines 476-589).

**Exit criteria (diagnostic only — NOT the PROMOTE gate; conversation.py:4887-4905):**
- `recall_threshold` / `faithfulness_threshold` from workflow YAML `synthesis.exit_criteria` (default 0.85/0.85)
- `exit_criteria.passed` is DIAGNOSTIC ONLY (ADR-029 Phase 1 superseded DECISION-010)

**PROMOTE gate (the actual gate):**
- User explicit `"accept"` → PROMOTE (ADR-029 Phase 1; conversation.py:5279)
- ADR-038 §F HARD BLOCKER: if `path_b_ran` and `routing_self_test_passed == False`, PROMOTE refused with `must_show_human=True`; no override provided (conversation.py:5286-5312)

**S6 reject routing (`_classify_and_route()`, conversation.py:5331):**

Six guardrails applied in order:
1. `confidence == "low"` or unknown class → `target_state = "REVIEW_DESIGN"` (guardrail 1; conversation.py:5507-5521)
2. `failure_class == "UNSUPPORTABLE"` → DONE as draft (guardrail 2; conversation.py:5525-5547)
3. Consecutive-same-class → DONE as draft (pathological loop; guardrail 3; conversation.py:5550-5571)
4. `eval_iteration_count >= _EVAL_MAX_ITERATIONS` (3) → DONE as draft (guardrail 4; conversation.py:5380-5397)
5. `eval_cumulative_cost_usd > _EVAL_COST_CEILING_USD` ($2.00) → DONE as draft (guardrail 5; conversation.py:5399-5419)
6. Show evidence + routing proposal to user (`must_show_human=True`); transition to `EVAL_ROUTE_PENDING` awaiting user confirmation (guardrail 6; conversation.py:5576-5624)

`_ROUTING_MAP` (conversation.py:223-230):
```
MISSING_FIELDS  → REVIEW_DESIGN
THIN_FIELDS     → REVIEW_DESIGN
WRONG_LAYOUT    → REVIEW_DESIGN
SOURCE_COVERAGE → CONFIGURE_SOURCES
WRONG_SOURCE    → INSPECT_SOURCES
UNSUPPORTABLE   → DONE_DRAFT (internal sentinel)
```

**Three-section EVAL report** (ADR-038 §B.6 — all three sections ALWAYS present):
1. `=== SECTION 1: ROUTING ASSERTIONS (Path B) ===`
2. `=== SECTION 2: EXECUTION (Path A) ===`
3. `=== SECTION 3: COMPARATOR (ADR-029) ===`
4. Diagnostic Metrics (informational only)

**Outcome / artifacts produced:**
- `_SessionData.eval_result` — full result dict including Path A/B results, comparator, gold set paths
- `_SessionData.routing_self_test_passed` — persisted for PROMOTE gate check
- Gold set files on filesystem and in ADB
- `must_show_human=True`, `awaiting_user=True`

**Exit condition → next state:**
- `"accept"` (and routing self-test passed or not run) → `_run_promote()` → PROMOTE
- `"accept"` (and routing self-test failed and Path B ran) → BLOCKED (conversation.py:5286-5312)
- `"ship as draft"` → DONE
- `"force promote"` (operator escape-hatch; retained for backward compat) → `_run_promote()` bypassing check
- Any other input → `_classify_and_route()` → `EVAL_ROUTE_PENDING` (user must confirm reroute)
- `"stop"` / `"exit"` / `"pause"` → DONE

**ADR/DECISION refs:** ADR-027 (DECISION-010 Option A — real extraction scoring), ADR-029 (S5: exit_criteria diagnostic-only; S6: classifier routing with guardrails), ADR-035 (comparator uses `has_bound_reference_artifact()`), ADR-038 (DECISION-018 — Path A/B; `routing_self_test_passed` hard block; three-section report).

---

### 16. PROMOTE

**Entry condition:** From EVAL when user `"accept"`s and routing self-test passed (or was not run). Entered via `_run_promote()` (conversation.py:5713).

**Handler:** `_handle_promote_response()` (conversation.py:5749).

**Input consumed:**
- `self._data.ingest_result` — checked for failed status before entering (belt-and-suspenders; conversation.py:5717-5733)
- User confirmation: `"yes"` / `"promote"` / `"ok"` / `"go"` vs `"no"` / `"keep as draft"`
- `persona_builder_delta` from ADB (REQUIRED — absence is hard-fail; conversation.py:5797-5808)

**Processing (deterministic — ADB operations):**

1. `_run_promote()`: pre-check for failed ingest (stays at INGEST if so). Show confirmation turn (no ADB write yet; conversation.py:5735-5746)
2. `_handle_promote_response()` on `is_yes`:
   a. `skill_store.promote(persona, skill_name)` — marks skill as promoted in ADB (conversation.py:5780)
   b. Read `persona_builder_delta` from ADB — HARD-FAIL if missing (BUG-queue-e685d; conversation.py:5797-5808)
   c. `skill_store.upsert_persona_builder_kb(persona, kb_name, content_yaml, status="production")` — writes KB card to ADB (conversation.py:5810-5815)
   d. **KB-resolvability check (ADR-033 Option B):** Load fresh `ShimKb(pb_dir, skill_store=self._skill_store)`; verify `find_kb(f"{persona}.{skill_name}")` returns a card. HARD-FAIL if ShimKb has cards from store but cannot find this one (conversation.py:5833-5862). Soft-warn if ShimKb loaded 0 cards (test env / empty store)
   e. Clean up stray `.new_kb` file from filesystem
3. On any exception: `self._state` stays at PROMOTE; user can retry (conversation.py:5899-5919)

**LLM calls:** No LLM — deterministic ADB operations.

**Outcome / artifacts produced:**
- Skill status updated to `"production"` in ADB
- KB card visible to `ShimKb` → routing layer can now resolve this skill
- `shim_workflows` will include this skill in `all_cards()` (ADR-033 promoted-only routing)
- Stray `.new_kb` file removed from filesystem
- Turn shown with KB population status note (empty KB vs populated)
- `awaiting_user=True`

**Exit condition → next state:** DONE (either promoted to production or kept as draft).

**ADR/DECISION refs:** ADR-033 (promoted-only routing; `ShimWorkflows` consumes promoted skills only), DECISION-018 (ADR-038 §F hard routing gate enforced before reaching PROMOTE).

---

### 17. DONE

**Entry condition:** From PROMOTE (success or draft), or from any state when user stops.

**Handler:** Lambda in dispatch table (conversation.py:567-569): `lambda _: ConversationTurn(state="DONE", message="Session complete.", done=True)`.

`done=True` on the ConversationTurn signals the client the session is terminal.

---

## Cross-Cutting Mechanics

### Session Persistence (`to_dict` / `from_dict`)

`to_dict()` (conversation.py:611) serializes the full session including all `_SessionData` fields, state string, and large artifacts (`synthesized_artifacts`, `design`, `source_samples`, `source_capability`). `from_dict()` (conversation.py:677) restores. Both require `skill_store` (hard-fail if None — BUG synth-tpm-14a54555). Persisted across ADB round-trips; enables resume across client restarts.

Key fields persisted for correctness:
- `clarify_questions` + `clarify_next_state` — BUG-queue-f0591: prevents CLARIFY from rewinding to CONFIGURE_SOURCES after design
- `pending_artifact_stash` — BUG-queue-f4987: artifact reference supplied at pre-upload state
- `design_skill_card` — ADR-038: carries `routing_queries` into ADB artifact
- `source_binding_mode` / `source_binding_signal` — ADR-032: backward-compat default `"author_fixed"` for pre-ADR-032 sessions

### source_binding_mode (ADR-032)

Two modes resolved at CAPTURE_INTENT:
- `"author_fixed"` (default; absent key = author_fixed per ADR-032 §H migration rule): source pages fixed at authoring. Workflow YAML has no `source_binding` block.
- `"ask_parameterized"`: consumer supplies source page at query time. Workflow YAML emits a full `source_binding` block with `{mode, input_param, ingest_on_demand, source_type, space_allow_list, ephemeral_ttl_seconds}`. Trigger input is replaced with typed `confluence_page_ref` input.

Resolution path: `capture_intent` LLM emits initial mode → if `"ambiguous"`, CLARIFY asks source-binding question → `_handle_clarify_response()` resolves to definitive value (keyword matching; conversation.py:1329-1368). Mode persisted and carried through VALIDATE (contract check) and `_synthesize_preview()` (YAML emission + `derive_space_allow_list()`).

### ADR-035 Source-and-Artifact Access-Verify Gate

INSPECT_SOURCES enforces source access by hard-failing on any `fetch_samples()` exception (conversation.py:1635-1642). This is the ADR-035 source-access gate: DESIGN_SKILL cannot proceed without confirmed source access.

Single-source-of-truth for reference artifact binding:
- **Bind:** `_bind_reference_artifact(artifact_id, type, name, layout, path)` — sets all four fields atomically (conversation.py:789-812)
- **Clear:** `_clear_reference_artifact(reason)` — clears all four fields atomically; requires explicit reason for audit log (conversation.py:814-831)
- **Check:** `has_bound_reference_artifact()` — `True` iff both `artifact_reference_id` and `artifact_reference_name` are non-None and non-empty (conversation.py:773-787). BOTH `REVIEW_DESIGN` and `_run_eval` must call this method; reading `design.workflow_shape.layout` text as artifact-bound signal is explicitly prohibited.

### DESIGN_SKILL Consumer-Facing Card, `routing_queries`, and `must_show_human` (DECISION-018 / ADR-038)

`_generate_design_skill_card()` runs at end of `_run_design_skill()` AFTER the design LLM call has set `self._data.output_format` from `design["workflow_shape"]["output_format"]`. This ordering is load-bearing: the card's `output_format` must reflect the design-authoritative value.

The card includes `routing_queries.positive` (5 queries) and `routing_queries.negative` (3 queries). These serve two roles:
1. **Tier-1 classifier signal** (via `ShimWorkflows.render_for_persona_prompt()` — positive only)
2. **EVAL Path B self-test** (both positive and negative)

`_prompt_review_skill_card()` returns a `must_show_human=True` turn — author must review/confirm the card before REVIEW_DESIGN. JSON edits to the card are applied and the review turn is re-shown.

`_synthesize_preview()` overwrites `wf_struct["skill_card"]` with `self._data.design_skill_card` after `synthesize_workflow_skill()` returns — ensuring the static `_build_skill_card()` template does NOT clobber the LLM-generated card in the ADB artifact (conversation.py:6084-6094).

### EVAL Path A vs Path B + Hard PROMOTE Routing Gate (ADR-038)

**Path A (in-process execution):** `WorkflowExecutor.execute_from_config(wf_cfg, exec_inputs)` — bypasses promoted-only router; uses ADB-committed config directly. Execution failure is HIGH-severity. Measures latency, produces artifact bytes for comparator.

**Path B (route dry-run):** `ShimWorkflows.resolve_only(query, scope="ingest_or_later")` — pure routing resolution without execution. Tests positive and negative routing queries from the design_skill_card. `routing_self_test_passed` stored on session.

**Hard PROMOTE gate (ADR-038 §F):** If `path_b_ran` (positive or negative queries existed) AND `routing_self_test_passed == False`, PROMOTE is refused in `_handle_eval_response()` with `must_show_human=True`. No override, no bypass. Fix path: update `routing_queries` in skill card at DESIGN_SKILL, re-commit, re-ingest, re-run EVAL.

**INGEST-or-later gate (ADR-038 §B.4):** `_entering_state` captured before `self._state = "EVAL"` mutation. If entering from `{"COMMITTED", "VALIDATE"}`, hard-fail RuntimeError (KB does not exist yet; no silent skip).

### shim_workflows Promoted-Only Routing (ADR-033)

`ShimWorkflows.all_cards()` returns only skills with `status = "production"` in ADB by default. A skill at EVAL state is not promoted and is invisible to the normal consumption router. Path A bypasses this by using `execute_from_config` directly. Path B uses `scope="ingest_or_later"` (considers INGEST-or-later skills without modifying the default `all_cards()` behavior). After PROMOTE, `ShimKb` and `ShimWorkflows` load the skill card via ADB (KB-resolvability invariant enforced in `_handle_promote_response()`).

---

## Legacy State Summary

| Legacy state | Pre-ADR-027 role | Disposition |
|---|---|---|
| `ANALYZE_ARTIFACT` | Parse uploaded artifact for field extraction | In dispatch table (conversation.py:571); runs for in-flight sessions; new sessions never enter |
| `REVIEW_FIELDS` | User reviews/edits field list | In dispatch table; legacy only |
| `REVIEW_SCHEMA` | User reviews/edits field descriptions | In dispatch table; legacy only |
| `CHECK_REUSE` | Detect reuse opportunities from existing KBs | In dispatch table; legacy only |
| `PREVIEW` | Show synthesized artifacts before commit | In dispatch table; `_advance_to_preview()` called from CONFIGURE_TRIGGERS for legacy sessions lacking `source_capability` |

`_LEGACY_ONLY_STATES = frozenset(_STATES_LEGACY) - frozenset(STATES)` = `{"ANALYZE_ARTIFACT", "REVIEW_FIELDS", "REVIEW_SCHEMA", "CHECK_REUSE", "PREVIEW"}` (conversation.py:291).
