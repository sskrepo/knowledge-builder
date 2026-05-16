---
title: "ADR-028 + ADR-029 — Implementation Blueprint"
status: active
created: 2026-05-15
owner: architect
deciders: dev-manager, dev-team
tags: [impl-plan, adr-028, adr-029, skill-builder, conversation, eval]
---

# ADR-028 + ADR-029 — Implementation Blueprint

This document is the executable work breakdown the main session uses to fan out
parallel backend-dev agents. It is self-contained: a dev agent receiving this
document plus the two accepted ADRs should be able to implement their assigned
tasks without further architect input.

Raise a DECISION file if you encounter a choice the blueprint does not answer.
Do NOT guess past open questions.

---

## 0. Serialization constraint — the central rule

`framework/skill_builder/conversation.py` is touched by every ADR-028 item and
by ADR-029. It is the most complex file in the framework (~3600+ lines). Parallel
agents editing the same file simultaneously will produce merge conflicts that are
harder to resolve than implementing the features linearly.

**RULE: all changes to `conversation.py` are ONE serial stream, in dependency
order. Everything else (test files, new modules, YAML, tool description) is
parallel-safe.**

Serial stream order (must be executed in sequence within a single agent context):

```
S1  Item 4 — synthesisable confidence level  (conversation.py + INSPECT_SOURCES prompt)
S2  Item 2 — awaiting_user + must_show_human (conversation.py ConversationTurn dataclass
             + all state handlers that emit turns + mcp_tools.py tool description)
S3  Item 3 — CLARIFY state                   (conversation.py new state + prompt constants)
S4  Item 1 — persona_prompts.yaml injection   (conversation.py _CAPTURE_INTENT_PROMPT +
             _DESIGN_SKILL_PROMPT + loader; persona_prompts.yaml already committed)
S5  ADR-029 Phase 1 — artifact retention +   (conversation.py _handle_upload_artifact_example
             text comparator + image-only     + _run_eval + new comparator module)
             hard-reject + gap report
S6  ADR-029 Phase 2 — constrained routing +  (conversation.py _run_eval routing logic;
             loop guardrails                  depends on S5)
```

Parallel-safe side streams (each is an independent agent):

```
P1  persona_prompts.yaml review / stub tests    (config file + loader unit tests)
    [ALREADY COMMITTED — wire and test only]
P2  ArtifactComparator module                   (new file: framework/skill_builder/comparator.py)
    [build and unit-test in isolation; S5 imports it]
P3  Test suite expansion                        (framework/tests/unit/test_skill_builder_*)
    [QA agent writes tests for S1-S4 against stubs; must complete before S5 starts]
P4  authorskill-flow.md wiki update             (docs/wiki/authorskill-flow.md)
    [Architect updates state-machine doc after S3 lands]
```

---

## 1. Dependency DAG

```
[S1: Item 4 synthesisable] ──────────────────────────────────────────────┐
                                                                          │
[S2: Item 2 must_show_human] ────────────────────────────────────────────┤
         │                                                               │
         ▼                                                               │
[S3: Item 3 CLARIFY state] ──┐                                          │
         │                    │                                          │
         ▼                    │  (parallel, both depend on S2)          │
[S4: Item 1 persona injection]◄──── P1: persona_prompts.yaml tests       │
         │                                                               │
         ▼                                                               │
[S5: ADR-029 Phase 1] ◄─────────────────────────────── P2: comparator   │
    (needs S2 + S4 done,                               module           │
     and S1 for MISSING_FIELDS                                           │
     routing to work correctly)                                          │
         │                    ◄────────────────────── P3: test suite     │
         ▼                                                               │
[S6: ADR-029 Phase 2] ◄──────────────────────────────────────────────────┘
    (needs S5 done;           [classifier validation gate — explicit task
     CLASSIFIER VALIDATION     required before routing is enabled]
     GATE must pass)
```

Legend: `──►` = serial dependency. `◄────` = parallel work that must complete
before the target starts.

---

## 2. Task specifications

Each task: files touched, change description, required test, acceptance check,
blocking/blocked-by.

---

### S1 — Item 4: Synthesisable confidence level

**Parallel-safe:** No. First task in the serial stream.

**Files:**
- `framework/skill_builder/conversation.py` — `_INSPECT_SOURCES_PROMPT` constant
  (add `synthesisable` to the confidence taxonomy instruction) and
  `_DESIGN_SKILL_PROMPT` constant (add inclusion rule for `synthesisable` fields
  with mandatory aggregation instruction in the field description).
- No new files.

**Change (4 sentences):**
Extend the `_INSPECT_SOURCES_PROMPT` confidence taxonomy from three levels
(`high`, `medium`, `missing`) to four by adding `synthesisable`: a field whose
value must be derived by combining or aggregating content present in the source
but not available as a single labelled element. Update `_DESIGN_SKILL_PROMPT`
to include a rule: "For fields with confidence=synthesisable, the extraction
instruction MUST explicitly state 'Derive this value by [aggregating / combining /
summarising] the following content: [specific source element].' A synthesisable
field with no aggregation instruction is a prompt defect." The DESIGN_SKILL
inclusion rule changes from "only high or medium confidence fields" to "high,
medium, or synthesisable confidence fields." No schema or dataclass changes
are needed — `source_capability` already carries an opaque list of field dicts.

**Test that must ship:**
- Unit test in `framework/tests/unit/test_skill_builder_conversation.py`:
  `test_synthesisable_field_included_in_design` — mock `_INSPECT_SOURCES_PROMPT`
  response that tags a field (`risks`) as `confidence=synthesisable`; assert that
  the resulting `_DESIGN_SKILL_PROMPT` call includes `risks` in the schema output
  and that its description contains the word "Derive" or "aggregate".
- Unit test: `test_missing_field_excluded` — assert that a field with
  `confidence=missing` (no `synthesisable` tag) is still excluded.

**Acceptance check:**
Re-run the real-world case: author the `tpm.26ai_fa_db_upgrade_to_26ai_pptx`
skill. The INSPECT_SOURCES response for the WBS table source should now tag
`risks` and `next_steps` as `synthesisable`. The DESIGN_SKILL output should
include both fields with aggregation instructions in their descriptions. The
produced PPTX should have non-empty Risks and Next Steps sections.

**Blocks:** S5 (MISSING_FIELDS routing depends on synthesisable fields being
in the schema; if they are absent, the CHANGE PROPOSAL will always route back
to REVIEW_DESIGN even though the schema is already correct).

**Blocked by:** Nothing.

---

### S2 — Item 2: `awaiting_user` + `must_show_human` on ConversationTurn

**Parallel-safe:** No (serial stream). Can start immediately after S1 merges.

**Files:**
- `framework/skill_builder/conversation.py` — `ConversationTurn` dataclass,
  all state handler methods that call `self._make_turn(...)` or construct a
  `ConversationTurn` directly.
- `framework/skill_builder/mcp_tools.py` — `authorSkill` tool description string.

**Change (4 sentences):**
Add two boolean fields to the `ConversationTurn` dataclass: `awaiting_user: bool
= True` (True on every turn that requires a human response; False only for
deterministic auto-transitions where no human input is needed) and
`must_show_human: bool = False` (True for turns the client must never auto-answer).
Set `must_show_human=True` on these specific turns: CAPTURE_INTENT (when
`blocking_ambiguities` is non-empty — after S3 this becomes CLARIFY; for now
set it whenever `ambiguities` is non-empty), REVIEW_DESIGN (always), and
PREVIEW_EXTRACTION (always). Update the `authorSkill` tool description in
`mcp_tools.py` to include: "CRITICAL: when `mustShowHuman=true` is in the
response, you MUST display the full `message` field to the actual human user
and wait for their typed response before calling authorSkill again. Do NOT
summarise, paraphrase, auto-answer, or infer a response. The human must see and
explicitly respond to this turn." The camelCase serialisation boundary already
converts `must_show_human` → `mustShowHuman` in the JSON response (per existing
snake→camel logic); verify this in the serialization layer.

**Test that must ship:**
- Unit test: `test_must_show_human_set_on_review_design` — assert REVIEW_DESIGN
  turn has `must_show_human=True`.
- Unit test: `test_must_show_human_set_on_preview_extraction` — same for
  PREVIEW_EXTRACTION.
- Unit test: `test_awaiting_user_false_on_auto_transition` — assert any turn
  that the state machine emits without waiting for input (e.g. DESIGN_SKILL
  auto-starting after UPLOAD_ARTIFACT_EXAMPLE) has `awaiting_user=False`.
- Unit test: `test_must_show_human_camel_case_serialized` — assert that the
  JSON response contains `mustShowHuman` (not `must_show_human`).

**Acceptance check:**
In a live session, the MCP client receives `mustShowHuman: true` on the
REVIEW_DESIGN turn. A Claude Code client configured to follow MCP tool
descriptions displays the full design to the user before advancing.

**Blocks:** S3 (CLARIFY state sets `must_show_human=True`; the field must exist
first). S5 (EVAL CHANGE PROPOSAL turns must also set `must_show_human=True`).

**Blocked by:** S1 (ordering in serial stream only; no logical dependency).

---

### S3 — Item 3: CLARIFY state (17th state)

**Parallel-safe:** No (serial stream). Starts after S2 merges.

**Files:**
- `framework/skill_builder/conversation.py` — new `CLARIFY` state constant,
  `_CLARIFY_PROMPT` (new prompt constant), `_advance_to_clarify()` handler,
  `_handle_clarify_response()` handler, update to `_handle_capture_intent()`
  to route to CLARIFY when `blocking_ambiguities` is non-empty instead of
  directly to CONFIGURE_SOURCES, update to `_CAPTURE_INTENT_PROMPT` to
  distinguish `blocking_ambiguities` from `nice_to_know_ambiguities`, update
  to `_DESIGN_SKILL_PROMPT` to return `blocking_questions` (schema-structure-
  altering) separately from `open_questions` (cosmetic), update to
  `_run_design_skill()` to route to CLARIFY when `blocking_questions` is
  non-empty before transitioning to REVIEW_DESIGN.

**Change (4 sentences):**
Add CLARIFY as a new state between CAPTURE_INTENT and CONFIGURE_SOURCES (and
optionally between DESIGN_SKILL and REVIEW_DESIGN). The `_CAPTURE_INTENT_PROMPT`
output schema is extended: `ambiguities` splits into `blocking_ambiguities`
(questions whose answers would change the schema structure, output kind, or
source selection) and `nice_to_know_ambiguities` (proceed with assumption,
flag for user awareness). `_handle_capture_intent()` routes to CLARIFY when
`blocking_ambiguities` is non-empty; the CLARIFY handler asks one blocking
question per turn, sets `must_show_human=True`, records the user's answer on
`_data.clarification_log` (new `_SessionData` field: `list[dict]`), and
marks the question resolved; when all blocking questions are resolved, CLARIFY
transitions to CONFIGURE_SOURCES. Similarly, `_DESIGN_SKILL_PROMPT` returns
`blocking_questions` (separate from `open_questions`); if non-empty,
`_run_design_skill()` routes to CLARIFY before REVIEW_DESIGN, using the same
CLARIFY handler with the same per-question loop. The `_CLARIFY_PROMPT` emits
a conversational message ("Before I proceed, I need to clarify one thing: ..."),
not a JSON blob — plain prose the human can read and respond to.

**New `_SessionData` field:**
```python
clarification_log: list[dict] = field(default_factory=list)
# Each entry: {"question": str, "answer": str, "resolved_at": str (ISO)}
```
This field must be added to `to_dict()` and `from_dict()` for session persistence.

**Test that must ship:**
- Unit test: `test_clarify_state_entered_on_blocking_ambiguity` — mock
  CAPTURE_INTENT response with one blocking_ambiguity; assert state transitions
  to CLARIFY, not CONFIGURE_SOURCES.
- Unit test: `test_clarify_advances_after_all_questions_resolved` — simulate
  two blocking questions; assert state stays at CLARIFY after first answer,
  advances to CONFIGURE_SOURCES after second.
- Unit test: `test_clarify_skipped_on_no_blocking_ambiguities` — mock
  CAPTURE_INTENT response with only nice_to_know; assert direct transition to
  CONFIGURE_SOURCES.
- Unit test: `test_clarify_sets_must_show_human` — assert every CLARIFY turn
  has `must_show_human=True`.
- Unit test: `test_clarification_log_persisted` — assert `clarification_log`
  is included in `to_dict()` output and round-trips through `from_dict()`.

**Acceptance check:**
In a live session with an ambiguous intent ("create a weekly summary for 26ai"),
the system must pause at CLARIFY and ask "Which Confluence space should I use
as the primary source — FAAAS or 26AI-LEGACY?" before proceeding. Typing "ok"
without answering must NOT advance the state.

**Blocks:** S4 (persona injection uses the extended CAPTURE_INTENT output
schema; S3 changes what fields CAPTURE_INTENT returns). Logically S4 should
consume the new `blocking_ambiguities` / `nice_to_know_ambiguities` split.

**Blocked by:** S2.

---

### S4 — Item 1: Persona prompt fragment injection

**Parallel-safe:** No (serial stream). Starts after S3 merges.

**Files:**
- `framework/skill_builder/conversation.py` — `_CAPTURE_INTENT_PROMPT` (add
  `{persona_key_fields}` kwarg injection), `_DESIGN_SKILL_PROMPT` (add
  `{persona_key_fields}`, `{persona_extraction_style}`, `{persona_few_shot_example}`
  kwargs), new helper `_load_persona_prompt_fragments(persona: str) -> dict`
  that reads `framework/config/persona_prompts.yaml` and returns the stanza for
  the given persona (raises `KeyError` if persona not found, falling back to
  empty strings — graceful degradation; logs a warning).
- `framework/config/persona_prompts.yaml` — already committed; no code change.

**Change (4 sentences):**
Add a module-level YAML loader that reads `persona_prompts.yaml` once at import
time (or lazily on first call) and caches the stanzas in a module-level dict.
Extend `_CAPTURE_INTENT_PROMPT` to include a section:
"Persona guidance: This persona's canonical output always includes these fields —
{persona_key_fields}. Use this list as a starting point for understanding what
dimensions matter most." Extend `_DESIGN_SKILL_PROMPT` to include a section:
"Persona extraction style: {persona_extraction_style}\n\nWorked example of a
well-designed field for this persona:\n{persona_few_shot_example}" — inserted
in the system instructions block, not the data block. If the persona is not in
`persona_prompts.yaml`, the kwargs default to empty strings and a warning is
logged (the prompt degrades gracefully to the current static template).

**Test that must ship (P1 parallel stream owns loader tests; S4 owns integration):**
- Unit test in S4: `test_persona_fragments_injected_into_design_skill_prompt` —
  mock `_load_persona_prompt_fragments("tpm")` returning the tpm stanza; assert
  that the constructed `_DESIGN_SKILL_PROMPT` string contains the tpm
  `extraction_style` text.
- Unit test: `test_unknown_persona_graceful_degradation` — assert that a persona
  not in the YAML (e.g. `"unknown_persona"`) does not raise; the prompt is
  constructed with empty persona fields and a warning is logged.

**Acceptance check:**
In a DESIGN_SKILL call for persona=tpm, the LLM receives instructions that
include "Use exec-safe language throughout" and the `blocking_issues` few-shot
example. The generated schema should include `orm_status` and `rag_summary`
without the user having to ask for them.

**Blocks:** S5 (the full prompt surface that S5 must score against is not
stable until S4 is complete).

**Blocked by:** S3.

---

### P1 — persona_prompts.yaml loader unit tests (parallel stream)

**Parallel-safe:** Yes. Independent of S1-S4. Can start now.

**Files:**
- `framework/tests/unit/test_persona_prompts_loader.py` (new file)

**Change (2 sentences):**
Write unit tests for the `_load_persona_prompt_fragments` loader that S4 will
implement: test that every persona in `framework/config/persona_prompts.yaml`
loads correctly, that `key_fields` is a non-empty list, that `extraction_style`
is a non-empty string, and that `few_shot_example` is a non-empty string. Test
the graceful-degradation path (unknown persona returns empty-string defaults).

**Note:** The loader implementation is in S4. P1 writes the tests against the
interface contract; they will fail until S4 ships (that is expected). P1 can
run in parallel with S1-S3.

**Blocked by:** Nothing (write tests against the contract).

---

### P2 — ArtifactComparator module (parallel stream)

**Parallel-safe:** Yes. Independent of S1-S4. Must complete before S5 starts.

**Files:**
- `framework/skill_builder/comparator.py` (new file)
- `framework/tests/unit/test_comparator.py` (new file)

**Change (4 sentences):**
Implement `ArtifactComparator` as a standalone class with no dependency on
`conversation.py`. It accepts two text-bearing artifact byte payloads
(`reference_bytes: bytes, produced_bytes: bytes, artifact_type: str`) and
returns a `ComparatorResult` dataclass with: `structure_score: float` (0.0-1.0,
fraction of reference sections present in produced artifact),
`density_score: float` (0.0-1.0, content volume ratio per section),
`missing_sections: list[str]` (sections in reference absent from produced),
`thin_sections: list[str]` (sections present but density < 0.5x reference),
and `gap_report: str` (human-readable summary for the CHANGE PROPOSAL). The
comparator MUST also implement `is_image_only(bytes, artifact_type) -> bool`
using the same zero-text-shapes detection pattern as `_analyze_pptx` — this
is the gating function for the hard-reject path. For PPTX: use `python-pptx`
to extract text runs per slide; structure comparison is slide-title matching
with synonym normalisation (e.g. "Next Steps" == "Action Items" — use a
hardcoded synonym map, not an LLM call, to keep the comparator deterministic).
For DOCX: use `python-docx` heading extraction. For MD: heading-level parsing.

**Image-only detection contract:**
```python
def is_image_only(self, artifact_bytes: bytes, artifact_type: str) -> bool:
    """Return True if the artifact has zero extractable text (image-only).
    Uses the same pattern as _analyze_pptx: count text runs across all shapes;
    return True if count == 0. Raises ValueError for unsupported artifact_type."""
```

**Test that must ship:**
- Unit test: `test_structure_score_perfect_match` — reference and produced have
  identical section names; assert `structure_score == 1.0`.
- Unit test: `test_structure_score_missing_sections` — produced missing 3 of 7
  reference sections; assert `structure_score == 4/7` and `missing_sections`
  contains the 3 absent section names.
- Unit test: `test_density_score_thin_section` — produced section has 20 words
  vs reference 100 words; assert `density_score < 0.5` and the section appears
  in `thin_sections`.
- Unit test: `test_is_image_only_true` — fixture PPTX with only picture shapes,
  no text runs; assert `True`.
- Unit test: `test_is_image_only_false` — fixture PPTX with text shapes; assert
  `False`.
- Unit test: `test_synonym_normalisation` — reference has "Next Steps"; produced
  has "Action Items"; assert structure score counts it as a match.

**Blocked by:** Nothing.

**Blocks:** S5 imports `ArtifactComparator`.

---

### P3 — Test suite expansion for S1-S4 (parallel stream, QA agent)

**Parallel-safe:** Yes. Runs in parallel with S1-S4 and P2. Must complete before
S5 starts (the test suite must be green for S1-S4 before ADR-029 code lands).

**Files:**
- `framework/tests/unit/test_skill_builder_conversation.py` — extend existing
  test file with test classes for each new capability.

**Tests to write (all against stubs/mocks — no live LLM):**
- `TestSynthesisableField` (for S1): described in S1 above.
- `TestMustShowHuman` (for S2): described in S2 above.
- `TestClarifyState` (for S3): described in S3 above.
- `TestPersonaPromptInjection` (for S4): described in S4 above.

**Acceptance check:** `pytest framework/tests/unit/test_skill_builder_conversation.py
-k "Synthesisable or MustShowHuman or ClarifyState or PersonaPrompt" --tb=short`
must show all new tests collected and all S1-S4 tests green (some will be red
until the implementation lands — this is expected; the goal is to have the
test contracts written so the dev can iterate against them).

**Blocked by:** Nothing (write against contract; fail until impl lands).

---

### S5 — ADR-029 Phase 1: Artifact retention + text comparator + image-only hard-reject + gap report

**Parallel-safe:** No (serial stream). Starts after S4 merges, P2 tests green,
P3 tests green for S1-S4.

**Files:**
- `framework/skill_builder/conversation.py` — `_SessionData` (new field
  `artifact_reference_id: str | None = None`), `_handle_upload_artifact_example`
  (retain artifact bytes in ArtifactStore; add `is_image_only` check; hard-reject
  with message if image-only), `_run_eval` (read reference artifact from
  ArtifactStore; read produced artifact from `wf_artifact_url`; call
  `ArtifactComparator`; build gap report; surface to user with
  `must_show_human=True`; demote `exit_criteria.passed` from PROMOTE gate to
  diagnostic signal).
- `framework/skill_builder/comparator.py` — imported, no changes needed (P2).
- `framework/skill_builder/mcp_server.py` or wherever `ArtifactStore.cleanup()`
  is called — defer cleanup until after EVAL completes (not on session DONE if
  EVAL has not run yet).

**Change (4 sentences):**
`_handle_upload_artifact_example` now calls `ArtifactComparator.is_image_only()`
on the uploaded bytes before calling `analyze_artifact`. If image-only, it
returns a turn with `must_show_human=True` and the exact hard-reject message:
"Image-based reference artifacts are not supported yet (no Vision-LLM backend).
Please upload a text-bearing reference (text-extractable PPTX/DOCX/MD)." — and
does NOT advance the state. If text-bearing, the ArtifactStore write is retained
past UPLOAD_ARTIFACT_EXAMPLE by storing the artifact ID in
`_data.artifact_reference_id`; the `ArtifactStore.cleanup()` call is deferred
to after EVAL by passing a `retain_reference=True` flag. In `_run_eval`, after
running the existing extraction and faithfulness steps (retained as diagnostic
signals), the comparator is called: read the reference artifact via
`self._artifact_store.read(self._data.artifact_reference_id)`, read the produced
artifact via parsing the file at `wf_artifact_url`, call
`ArtifactComparator.compare()`, and build a human-readable gap report ("The
produced PPT has N sections; your reference had M. Missing: X, Y, Z. Thin
sections: A, B."). The gap report turn has `must_show_human=True`. The
`exit_criteria.passed` boolean is demoted: it is still computed and shown as
a diagnostic signal but it is no longer the gate for PROMOTE; the user's
explicit "accept" response becomes the gate.

**New EVAL exit logic:**
```python
# After comparator runs:
if not user_accepted:
    return self._make_turn(
        state="EVAL",
        message=gap_report,
        must_show_human=True,
        data={
            "structure_score": comparator_result.structure_score,
            "density_score": comparator_result.density_score,
            "missing_sections": comparator_result.missing_sections,
            "thin_sections": comparator_result.thin_sections,
            "intrinsic_recall": eval_result["recall_at_k"],        # diagnostic only
            "intrinsic_faithfulness": eval_result["faithfulness"],  # diagnostic only
        },
        options=["accept", "review design", "configure sources", "ship as draft"],
    )
# "accept" → PROMOTE; "ship as draft" → DONE (draft status)
# "review design" / "configure sources" → S6 routing (Phase 2)
```

**Test that must ship:**
- Unit test: `test_image_only_reference_hard_rejected` — upload image-only bytes;
  assert state does NOT advance, message contains the exact hard-reject string,
  `must_show_human=True`.
- Unit test: `test_artifact_reference_id_retained_in_session_data` — upload
  text-bearing artifact; assert `_data.artifact_reference_id` is non-None and
  survives a `to_dict()` + `from_dict()` round-trip.
- Unit test: `test_eval_gap_report_surface_to_user` — mock comparator returning
  `missing_sections=["Risks", "Next Steps"]`; assert EVAL turn message contains
  "Missing: Risks, Next Steps" and `must_show_human=True`.
- Unit test: `test_eval_numeric_scores_shown_as_diagnostic` — assert EVAL turn
  data contains `intrinsic_recall` and `intrinsic_faithfulness` but they do NOT
  control the transition to PROMOTE.
- Unit test: `test_user_accept_transitions_to_promote` — send "accept" response
  at EVAL turn; assert state transitions to PROMOTE.

**Acceptance check:** Full end-to-end test with the `tpm.26ai_fa_db_upgrade_to_26ai_pptx`
skill and a text-bearing reference artifact. The EVAL turn must show the gap
report (not just recall@k), and typing "accept" must advance to PROMOTE. Typing
"ok" without reviewing the gap report must NOT advance (the turn has
`must_show_human=True`).

**Blocks:** S6.

**Blocked by:** S4 (conversation.py serial stream), P2 (comparator module),
P3 (test suite green for S1-S4).

---

### ADR-029 Phase 1 classifier validation gate (explicit task — NOT optional)

**STATUS: GATE PASSED — 2026-05-15**
- Run 1: MISSING_FIELDS (high confidence)
- Run 2: MISSING_FIELDS (high confidence)
- Run 3: MISSING_FIELDS (high confidence)
- 0/3 runs returned SOURCE_COVERAGE or WRONG_SOURCE
- Prompt: `_FAILURE_CLASSIFIER_PROMPT` in `framework/skill_builder/conversation.py`
- Gate test: `framework/tests/unit/test_failure_classifier_gate.py`
- S6 may proceed.

**Owner:** Backend Dev (same as S5) + QA. Must execute between S5 and S6.

**Purpose:** The failure-class classifier in S6 is load-bearing. A bad diagnosis
sends the user to the wrong state. This gate requires the classifier prompt to be
validated against the known real-world failure case before the routing layer is
enabled.

**Known real case to validate against:**
Skill: `tpm.26ai_fa_db_upgrade_to_26ai_pptx`
Reference artifact: text-bearing reference (must use a text-extractable version
for this test — the original was image-only).
Known root cause: `risks` and `next_steps` were excluded from the schema because
DESIGN_SKILL treated synthesisable WBS content as `confidence=missing`.
Expected failure class: `MISSING_FIELDS` (fields were absent from the schema
entirely) — NOT `SOURCE_COVERAGE` (the content existed in the WBS table).
Expected routing: REVIEW_DESIGN (to add the missing fields with synthesisable
instructions, now possible after S1 lands).

**Validation procedure:**
1. Run EVAL on the above skill with a text-bearing reference that has Risks and
   Next Steps sections.
2. Feed the comparator output (missing_sections=["Risks", "Next Steps"]) +
   the source capability inventory (which shows the WBS table content IS available
   as `confidence=synthesisable` after S1) to the classifier prompt.
3. Assert the classifier returns `MISSING_FIELDS` (not `SOURCE_COVERAGE`,
   not `THIN_FIELDS`).
4. If the classifier returns the wrong class on this known case, the classifier
   prompt must be revised before S6 routing is enabled. S6 is BLOCKED until
   this gate passes.

**Classifier prompt requirements (baked into the validation):**
- The classifier receives: (a) the gap report from the comparator, (b) the full
  source capability inventory (including `synthesisable` confidence tags), and
  (c) the current schema. It must NOT receive only the reference vs produced diff.
- The classifier must emit structured output:
  ```json
  {
    "failure_class": "MISSING_FIELDS",
    "confidence": "high",
    "evidence": "Risks and Next Steps are absent from the schema. The source
                 capability inventory shows these fields are synthesisable from
                 the WBS table (confidence=synthesisable), not genuinely absent
                 from the source. Therefore, the fix is to add them to the schema
                 (REVIEW_DESIGN), not to add more source pages (CONFIGURE_SOURCES).",
    "alternative_class": "SOURCE_COVERAGE",
    "why_not_alternative": "Source capability inventory confirms WBS table content
                            is present (confidence=synthesisable); content exists,
                            it was not designed into the schema."
  }
  ```
- `evidence` and `why_not_alternative` are REQUIRED fields — the classifier must
  justify its choice and explicitly rule out alternatives. The diagnosis is
  surfaced to the user as part of the CHANGE PROPOSAL (must_show_human=True).

**Gate: S6 is blocked until this test passes on the known real case.**

---

### S6 — ADR-029 Phase 2: Constrained routing + loop guardrails

**Parallel-safe:** No (serial stream). Starts after S5 merges AND classifier
validation gate passes.

**Files:**
- `framework/skill_builder/conversation.py` — `_run_eval` (add troubleshooting
  LLM call with the classifier prompt; add constrained routing map; add loop
  guardrails: `eval_iteration_count` on `_SessionData`, `_cost_ceiling_check()`,
  consecutive-same-class detector, "ship as draft" escape hatch).
- `framework/skill_builder/conversation.py` — `_SessionData` (new fields:
  `eval_iteration_count: int = 0`, `last_eval_failure_class: str | None = None`,
  `eval_cumulative_cost_usd: float = 0.0`).

**Change (4 sentences):**
After the gap report is surfaced and the user responds with something other than
"accept" or "ship as draft", run the troubleshooting LLM call: pass the gap
report, the source capability inventory, and the current schema to the classifier
prompt (from the validation gate above) and receive a structured `failure_class`
+ `evidence`. The constrained routing map (code, not LLM choice) translates the
failure class to the target state: `MISSING_FIELDS`/`THIN_FIELDS`/`WRONG_LAYOUT`
→ REVIEW_DESIGN; `SOURCE_COVERAGE` → CONFIGURE_SOURCES; `WRONG_SOURCE` →
INSPECT_SOURCES; `UNSUPPORTABLE` → DONE as draft. Before routing, increment
`eval_iteration_count`; if it reaches `max_eval_iterations` (default: 3 from
workflow YAML, fallback hardcoded: 3), transition to DONE as draft. Check
`eval_cumulative_cost_usd` against `cost_ceiling_usd` (default: 2.00); if
exceeded, transition to DONE as draft. Detect consecutive same class: if
`last_eval_failure_class == current_failure_class`, emit message "EVAL has
cycled twice on the same failure class (`{class}`). This likely means the root
cause is structural. The skill is saved as draft for manual review." and
transition to DONE as draft. Surface the classifier's `evidence` and
`why_not_alternative` to the user in the routing turn (`must_show_human=True`).

**Constrained routing map (code, not prompt):**
```python
ROUTING_MAP = {
    "MISSING_FIELDS":   "REVIEW_DESIGN",
    "THIN_FIELDS":      "REVIEW_DESIGN",
    "WRONG_LAYOUT":     "REVIEW_DESIGN",
    "SOURCE_COVERAGE":  "CONFIGURE_SOURCES",
    "WRONG_SOURCE":     "INSPECT_SOURCES",
    "UNSUPPORTABLE":    "DONE_DRAFT",
}
```

**PROMOTE guard update:**
Remove `exit_criteria.passed` as a PROMOTE gate. The only PROMOTE gates are:
(a) user explicitly sends "accept" at EVAL, and (b) `ingest_result.status == "success"`.

**Test that must ship:**
- Unit test: `test_routing_missing_fields_to_review_design` — classifier returns
  `MISSING_FIELDS`; assert state transitions to REVIEW_DESIGN.
- Unit test: `test_routing_source_coverage_to_configure_sources` — assert
  SOURCE_COVERAGE → CONFIGURE_SOURCES.
- Unit test: `test_max_iterations_reached_ships_as_draft` — simulate 3 EVAL
  iterations; assert 4th iteration does not start; state is DONE with
  `status=draft`.
- Unit test: `test_cost_ceiling_ships_as_draft` — set `eval_cumulative_cost_usd`
  above ceiling; assert next EVAL attempt exits as draft.
- Unit test: `test_consecutive_same_class_loop_detected` — same failure class
  on two consecutive iterations; assert the pathological-loop message is shown
  and state is DONE draft.
- Unit test: `test_promote_gate_is_user_accept_not_numeric` — assert that a
  session with `exit_criteria.passed=False` but user sends "accept" successfully
  transitions to PROMOTE.

**Acceptance check:** In a controlled session, deliberately introduce a
MISSING_FIELDS gap and verify the classifier diagnoses it correctly, the routing
sends the user back to REVIEW_DESIGN (not CONFIGURE_SOURCES), and the loop
terminates correctly after 3 iterations if the user does not accept.

**Blocks:** Nothing (terminal task in the serial stream).

**Blocked by:** S5 + classifier validation gate.

---

## 3. Parallel fan-out recommendation

**Recommended streams for the main session to dispatch:**

| Stream | Agent role | Tasks owned | Can start |
|---|---|---|---|
| **Stream A** (serial) | Backend Dev (senior) | S1 → S2 → S3 → S4 → S5 → [gate] → S6 | Immediately |
| **Stream B** | Backend Dev (mid) | P2: ArtifactComparator module | Immediately |
| **Stream C** | QA Engineer | P1: persona_prompts loader tests + P3: test suite for S1-S4 | Immediately |

Three parallel streams, one serialization point: S5 cannot start until Stream A
(through S4), Stream B (P2), and Stream C (P3) are all complete. The classifier
validation gate between S5 and S6 is a Stream A + QA joint checkpoint.

**Serialization points (must wait for all streams):**

```
[Immediately]     Stream A (S1), Stream B (P2), Stream C (P1+P3) start in parallel
                         │                │                │
                         ▼                ▼                ▼
[Serialization 1] Stream A reaches end of S4.
                  Stream B completes P2.
                  Stream C completes P3 (S1-S4 tests green).
                  ──► All three must merge before S5 starts.
                         │
                         ▼
[S5 + gate]       Stream A runs S5, then classifier validation gate.
                  QA Engineer (Stream C) participates in the gate validation.
                         │
                         ▼
[S6]              Stream A runs S6 (serial, no new parallel streams needed).
```

**Wall-clock estimate (working days):**
- Stream A (S1-S4): 5-6 days
- Stream B (P2): 2-3 days (completes well before S5 is ready)
- Stream C (P1+P3): 2-3 days (completes well before S5 is ready)
- Serialization 1 wait: effectively zero (B and C finish before A)
- S5: 2-3 days
- Classifier validation gate: 1 day
- S6: 2 days
- **Total wall-clock (single-threaded critical path):** 10-12 days
- **With parallelism (3 streams):** 8-9 days (S1-S4 serial path dominates)

---

## 4. ADR-029 failure-classifier risk (load-bearing quality note)

The classifier quality is load-bearing. A wrong diagnosis routes the user to the
wrong state, wastes an EVAL iteration, and may trigger the pathological-loop
detector prematurely. The following requirements are NON-NEGOTIABLE:

1. **The classifier MUST receive the source capability inventory**, not just the
   reference vs produced diff. Without the capability inventory, the classifier
   cannot distinguish MISSING_FIELDS (field absent from schema; source has it)
   from SOURCE_COVERAGE (field absent from schema AND source lacks it).

2. **The classifier MUST emit structured per-class evidence.** Unstructured
   diagnosis text is not acceptable — the routing code cannot use it safely and
   the human cannot evaluate it quickly.

3. **The diagnosis MUST be surfaced to the human** (`must_show_human=True`) before
   routing. A misdiagnosis the user can see and correct is far less damaging than
   a misdiagnosis that silently reroutes the session.

4. **The classifier prompt MUST be validated against the known real case** (the
   26ai PPT — WBS content existed but was not designed into the schema) before
   the routing layer is enabled. This is the classifier validation gate between
   S5 and S6. If the classifier returns `SOURCE_COVERAGE` on this case (wrong),
   the prompt must be revised until it returns `MISSING_FIELDS` (correct).

5. **Low-confidence diagnoses route to REVIEW_DESIGN by default.** If the
   classifier returns `confidence=low`, the routing map defaults to
   REVIEW_DESIGN (the safest reroute: the user can inspect the schema and
   identify the actual problem themselves). Do not expose the user to a low-
   confidence reroute to CONFIGURE_SOURCES or INSPECT_SOURCES.

---

## 5. Wiki update required after S3 lands

After S3 merges, the Architect must update `docs/wiki/authorskill-flow.md` to
document the new 17-state machine: add the CLARIFY state description (trigger,
what runs, LLM involvement, must_show_human, output/next state, `_SessionData`
changes). This is a doc-only task and does not block any code stream.

---

## 6. Cross-references

- ADR-028 (Accepted): /docs/wiki/adr/ADR-028-authorskill-prompt-investment-human-loop-conversation.md
- ADR-029 (Accepted): /docs/wiki/adr/ADR-029-outcome-based-eval-acceptance-loop.md
- DECISION-011 (Resolved): /pmo/decisions/DECISION-011-authorskill-prompt-and-human-loop-direction.md
- DECISION-010 (Superseded): /pmo/decisions/DECISION-010-eval-gold-sets-auto-vs-human.md
- persona_prompts.yaml: /framework/config/persona_prompts.yaml
- conversation.py: /framework/skill_builder/conversation.py
- comparator.py (to create): /framework/skill_builder/comparator.py
- authorskill-flow.md: /docs/wiki/authorskill-flow.md (update after S3)
