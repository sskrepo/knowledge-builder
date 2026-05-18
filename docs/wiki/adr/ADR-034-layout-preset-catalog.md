---
title: ADR-034 — Layout Preset Catalog as Single Source of Truth for Renderer Dispatch
status: accepted
created: 2026-05-17
accepted: 2026-05-17
owner: architect
deciders: user, backend-dev
tags: [adr, skill-builder, rendering, pptx, prompts, abstraction, ux]
related: [ADR-026, ADR-027, ADR-028, ADR-030, DECISION-014]
supersedes: ~
---

# ADR-034 — Layout Preset Catalog as Single Source of Truth for Renderer Dispatch

## Status

**Accepted — 2026-05-17.**  Fixes the UX/abstraction defect where internal renderer
preset identifiers (e.g. `weekly_exec_review_v1`) were hardcoded into the
`design_skill` prompt, causing the LLM to parrot them into CLARIFY questions shown to
skill authors (session synth-tpm-3b2c2c71, JSON-RPC id 69).

---

## A. Context — The Failure

### The Observed Failure (JSON-RPC id 69, session synth-tpm-3b2c2c71)

During `authorSkill`, the CLARIFY question shown to the skill author was:

> "…or should the layout be recreated purely from the named layout preset
> (weekly_exec_review_v1)?"

`weekly_exec_review_v1` is an internal renderer dispatch key.  It has no meaning to a
skill author.  The user should never see it.

### Root Cause — Hardcoded Preset ID in design_skill Prompt

`framework/config/prompts/skill_builder.yaml` (design_skill v1.1) contained two leaks:

| Line (approx) | Leak |
|---|---|
| Schema example | `"layout": "weekly_exec_review_v1 \| default"` |
| Rule | `Choose "weekly_exec_review_v1" layout only for exec-review PPTX skills.` |

The DESIGN_SKILL LLM call read both, emitted `weekly_exec_review_v1` in
`design.workflow_shape.layout`, and a blocking CLARIFY question was generated that
included the identifier verbatim.

The PptxRenderer dispatch (ADR-026 Fix 5) also had a hardcoded string comparison:
```python
if layout == "weekly_exec_review_v1":
```
This is not a user-facing bug (the renderer is internal), but it created a tight
coupling: the identifier had to be maintained consistently across the prompt, the
renderer, and any test fixture that mentioned it.  There was no single source of truth.

### Why This Matters

This is a UX/abstraction defect (MEDIUM severity per DECISION-013): an internal machine
identifier leaks into the conversational UI.  The user's directive:

> "there shouldnt be any preset sent to LLM in the first place which is not relevant
> to the user ask; if user hasn't specified, the LLM should look at the user ask and
> come up with something that's a good fit for the user ask, or look into existing
> presets and identify what's a good fit and then ask the user."

---

## B. Decision

**Create a layout preset catalog (`framework/renderers/layout_catalog.py`) as the
single source of truth.  The prompt receives catalog DESCRIPTIONS (not internal_ids).
The LLM reasons over descriptions to select the best-fit preset.  The renderer
dispatches via catalog lookup.  Users only ever see human_label + description.**

### Catalog schema (`LayoutPreset` dataclass)

| Field | Type | Role | User-visible? |
|---|---|---|---|
| `internal_id` | str | Renderer dispatch key | NO |
| `human_label` | str | Plain-language name | YES |
| `description` | str | What the layout looks like | YES |
| `when_to_use` | str | LLM reasoning guidance | YES (via prompt) |
| `output_format` | str | "pptx", "docx", etc. | No (filter only) |
| `structural_shape` | str | Structural description | YES (via prompt) |

### Registered presets (v1)

| internal_id | human_label | output_format |
|---|---|---|
| `weekly_exec_review_v1` | "Single-slide executive review (two-column Oracle style)" | pptx |
| `default` | "Standard multi-slide deck" | pptx |

### Prompt injection (`catalog_for_prompt`)

`layout_catalog.catalog_for_prompt(output_format=None)` returns a plain-language
multi-entry description of all matching presets.  It intentionally omits `internal_id`
from the rendered text — only `human_label`, `description`, `when_to_use`, and
`structural_shape` are included.

The `design_skill` prompt receives this text as `{layout_preset_catalog}` (required var,
added in v1.2).  The prompt instructs the LLM to:
- Reason over the user ask + artifact layout hint + catalog entries
- Select the catalog entry whose `when_to_use` best matches the intent
- Emit the corresponding `internal_id` in `workflow_shape.layout` (machine field)
- Emit a plain-language `layout_rationale` (human field)
- NEVER add a blocking_question about layout when an artifact is uploaded or intent
  is clear — proceed with the best-fit and document the rationale

### Renderer dispatch via catalog

```python
# Before (ADR-034): hardcoded
if layout == "weekly_exec_review_v1":
    return self._render_weekly_exec_review_v1(data)

# After (ADR-034): catalog-driven
preset = get_preset(layout)
if preset is None:
    log.warning("unknown layout id %r; falling back to default", layout)
elif preset.internal_id == "weekly_exec_review_v1":
    return self._render_weekly_exec_review_v1(data)
```

Adding a new preset in the future: add a `LayoutPreset` entry to `layout_catalog.py`
and a new `elif preset.internal_id == "new_id"` branch in `PptxRenderer.render()`.
No prompt changes required — the catalog injection is automatic.

### Clarify wording guard (`_sanitize_clarify_question`)

A static method on `SkillBuilderConversation` runs over every blocking question before
it is stored in `_clarify_questions` or emitted to the user.  It replaces every known
`internal_id` with its `human_label`.  This is a defense-in-depth measure — the prompt
already instructs the LLM not to emit internal ids; the guard catches any slip-through.

### Confidence gate (no-ask rule)

The `design_skill` prompt v1.2 includes an explicit rule:

> CRITICAL: do NOT add a blocking_question about layout selection unless the user's
> intent is completely ambiguous AND no artifact layout hint was provided.  When an
> artifact is uploaded or the intent clearly implies a specific output style, proceed
> with the best-fit layout from the catalog and record your reasoning in layout_rationale.

This implements the user's directive: only ask when genuinely ambiguous AND no artifact.

---

## C. Implementation

### Files Changed

| File | Change |
|---|---|
| `framework/renderers/layout_catalog.py` | NEW — LayoutPreset dataclass, LAYOUT_PRESETS list, `get_preset()`, `all_presets()`, `catalog_for_prompt()`, `internal_ids()` |
| `framework/renderers/pptx_renderer.py` | Import `get_preset`; dispatch in `render()` now goes via `get_preset(layout)` with WARNING for unknown ids |
| `framework/config/prompts/skill_builder.yaml` | `design_skill` v1.1 → v1.2: removed hardcoded preset rule + enum; added `layout_preset_catalog` required_var and `{layout_preset_catalog}` injection; updated layout rules; added `layout_rationale` machine field |
| `framework/skill_builder/conversation.py` | Import `catalog_for_prompt`; inject `layout_preset_catalog=_layout_catalog_for_prompt(output_fmt_hint)` in both primary and fallback `get_prompt` calls; add `_sanitize_clarify_question` static method; `_advance_to_clarify` sanitizes all question texts before storage |
| `framework/tests/unit/test_adr034_layout_catalog.py` | NEW — 23 tests (catalog structure, renderer dispatch, sanitizer, prompt injection, PromptRegistry parse) |
| `framework/tests/unit/test_adr028_stream_a.py` | Add `layout_preset_catalog` to 3 existing test calls |
| `framework/tests/unit/test_adr030_cutover.py` | Add `layout_preset_catalog` to 1 existing test call |
| `framework/tests/unit/test_persona_prompts_loader.py` | Add `layout_preset_catalog` to `_DESIGN_SKILL_BASE` fixture dict |
| `framework/tests/fixtures/prompts/design_skill_tpm_26ai.json` | Add `layout_preset_catalog` to fixture vars |
| `framework/tests/fixtures/prompts/design_skill_v1_1_ask_parameterized.json` | Add `layout_preset_catalog` to fixture vars |
| `framework/tests/fixtures/prompts/design_skill_v1_1_author_fixed.json` | Add `layout_preset_catalog` to fixture vars |

### Scope Discipline

This ADR does NOT implement full arbitrary-uploaded-template faithful rendering.
The renderer still only dispatches to the programmatic `_render_weekly_exec_review_v1`
path and the default multi-slide path.  Faithful rendering of arbitrary user-uploaded
PPTX templates remains the ADR-028 follow-up (multimodal artifact analysis depth).

---

## D. Alternatives Considered

### Option B — Sanitizer-only fix (no catalog abstraction)

Keep the hardcoded rule and enum; strip `weekly_exec_review_v1` from user output at
every surface.  Rejected because:
- The root cause (LLM told to use a specific hardcoded identifier) is unfixed.
- New presets require prompt edits.
- The LLM selects the preset because it was the only named option, not because it
  reasoned about fit — incorrect for new use cases.

### Option C — Remove layout selection from LLM; derive from artifact analysis only

Defer layout selection entirely to an ADR-028 multimodal artifact-analysis step.
Rejected for v1: ADR-028 depth for image-only slides is an open problem (§8).
ADR-034 achieves the user's directive today; Option C can replace the catalog
injection approach once ADR-028 depth is implemented.

---

## E. Consequences

### Positive

- Internal renderer identifiers can never appear in a user-facing CLARIFY question
  (both prompt-level prevention and surface-level sanitization guard).
- Adding a new layout preset = one edit to `layout_catalog.py` only.
  Prompt and renderer both update automatically.
- LLM layout selection is reasoned over purpose descriptions, not constrained to a
  single hardcoded identifier name — correct behavior for new use cases.
- `PptxRenderer` dispatch is explicit about unknown layout ids (WARNING, no silent
  wrong-output).

### Negative

- `design_skill` callers must supply `layout_preset_catalog` kwarg.  All call sites
  (one production, multiple tests) updated in this change.  Future callers must
  include it.
- `catalog_for_prompt` text grows with each new preset.  At current scale (2 presets)
  this is a negligible token cost.  If preset count grows to tens, consider lazy
  injection (only include presets for the relevant output_format — already supported
  via the `output_format` filter parameter).

### Reversibility

Setting `layout_preset_catalog=""` in any caller reverts to no catalog guidance (LLM
produces a free-form layout string).  The sanitizer and renderer dispatch remain in
place regardless.

---

## F. Test Coverage

`framework/tests/unit/test_adr034_layout_catalog.py` (23 tests):

**Group (a) — design_skill prompt de-leaking:**
- T1 — `Choose "weekly_exec_review_v1" layout only` rule absent from template
- T2 — `"layout": "weekly_exec_review_v1 | default"` enum absent from template
- T3 — `{layout_preset_catalog}` placeholder present in template
- T4 — `layout_preset_catalog` in `required_vars`
- T5 — `design_skill` version bumped from 1.1

**Group (b) — Catalog as single source of truth / renderer dispatch:**
- T6 — `get_preset("weekly_exec_review_v1")` returns correct preset
- T7 — `get_preset("default")` returns correct preset
- T8 — `get_preset("nonexistent")` returns None
- T9 — Renderer dispatches `weekly_exec_review_v1` → 1-slide output
- T10 — Renderer dispatches `default` → multi-slide output
- T11 — Renderer logs WARNING for unknown layout id; produces fallback bytes
- T12 — `all_presets()` includes both presets
- T13 — `internal_ids()` includes both ids

**Group (c) — Clarify question sanitizer:**
- T14 — Question without internal id: unchanged
- T15 — Question with `weekly_exec_review_v1`: replaced with human_label
- T16 — All known internal ids replaced in a single question
- T17 — `_advance_to_clarify` sanitizes stored questions AND emitted message

**Group (d) — `catalog_for_prompt` human-language output:**
- T18 — No internal_id appears in description lines of catalog text
- T19 — All human_labels present in catalog text
- T20 — `output_format` filter works; empty format returns no-presets message
- T21 — PPTX catalog contains both known presets

**Group (e) — PromptRegistry sanity:**
- T22 — PromptRegistry parses `skill_builder.yaml` without error
- T23 — `design_skill` prompt renders correctly when `layout_preset_catalog` supplied

---

## G. Related ADRs

- **ADR-026** — Layout-aware PPTX rendering (origin of `weekly_exec_review_v1` and
  the programmatic `_render_weekly_exec_review_v1` builder).  ADR-034 wraps its
  dispatch key in a catalog abstraction.
- **ADR-028** — ANALYZE_ARTIFACT depth and the human-loop conversation design.
  ADR-028 §Item 3 (CLARIFY state) is the surface through which the leak was observed.
  ADR-034 adds the sanitizer guard to that surface.  Full artifact-driven layout
  derivation (replacing catalog injection) is an ADR-028 follow-up.
- **ADR-030** — Prompt externalization and hot-reload registry.  ADR-034 follows
  ADR-030's pattern: prompt change = bump version in YAML + add required_var;
  call site injects the new var; PromptRegistry validates at load time.
- **DECISION-014** — The principle decision that established the no-internal-id-in-prompts
  rule.  ADR-034 is the implementation.

---

## References

- `framework/renderers/layout_catalog.py` — catalog module (new)
- `framework/renderers/pptx_renderer.py` — renderer dispatch (updated)
- `framework/config/prompts/skill_builder.yaml` — prompt v1.2 (updated)
- `framework/skill_builder/conversation.py` — injection + sanitizer (updated)
- `framework/tests/unit/test_adr034_layout_catalog.py` — 23 new tests
- Session synth-tpm-3b2c2c71, JSON-RPC id 69 — triggering user request
- DECISION-014 — principle decision

---

## Known Gap (RC2) — Resolved (2026-05-17, DECISION-019 Option A)

**Identified**: 2026-05-17, post-investigation of junk-PPTX bug (request id 146, skill `tpm.faaas_kiwi_project_pptx`, session `synth-tpm-b518aab6`).
**Status**: Resolved — DECISION-019 RC2 Option A implemented.

### Gap Description (historical)

ADR-034 §B stated the LLM should emit the `internal_id` in `workflow_shape.layout`, but this was a soft instruction. In practice the DESIGN_SKILL LLM emitted prose layout descriptions instead of catalog `internal_id` tokens. The committed `synthesis.layout` field carried the prose; `get_preset(prose)` returned `None`; renderer fell back to the 6-slide stub.

### Resolution: Constrained ID Output + Design-Time Validation (Option A)

Two changes implemented in one pass:

**1. Prompt v1.3 — OUTPUT SCHEMA CONSTRAINT section:**

`design_skill` prompt bumped from v1.2 to v1.3. A new `{layout_valid_ids}` var is injected at render time (populated by `layout_catalog.internal_ids()`). The valid enum appears ONLY in the OUTPUT SCHEMA CONSTRAINT section — not in the reasoning rules or examples. This preserves the DECISION-014 intent: the LLM reasons over `layout_preset_catalog` (human descriptions only), then maps to a machine ID as its final output step.

```
## OUTPUT SCHEMA CONSTRAINT

workflow_shape.layout MUST be exactly one of the following registered catalog internal_ids
(or null if no layout-dispatched output is required):
  {layout_valid_ids}

Emitting any value not in this list is a schema violation.
```

`layout_valid_ids` is a required_var in v1.3, populated at render time from `layout_catalog.internal_ids()`.

**2. Design-time validation in `_run_design_skill`:**

After parsing the DESIGN_SKILL LLM response, `conversation.py` validates `workflow_shape.layout`:

```python
if _designed_layout is not None and _designed_layout != "":
    _valid_ids = internal_ids()
    if _designed_layout not in _valid_ids:
        raise RuntimeError(
            f"DESIGN_SKILL: workflow_shape.layout={_designed_layout!r} is not a "
            f"registered catalog internal_id. Valid ids: {_valid_ids}. ..."
            "This is a design-time error (DECISION-019 RC2)..."
        )
```

This surfaces the error at design time (in the authoring session) rather than silently at execution time. The author can immediately correct the layout selection.

**DECISION-014 compliance note:** The catalog `internal_id` values appear in the OUTPUT SCHEMA CONSTRAINT section only — not in reasoning instructions, not as examples in the rules section. The `layout_preset_catalog` (human_label + description + when_to_use) remains in the reasoning section. This is the intended DECISION-014 mitigation: reason over descriptions, emit a machine token as the final structured field.
