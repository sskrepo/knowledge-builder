# DECISION-014: No Internal Renderer/Preset Identifiers in Prompts

**Status**: DECIDED
**Date**: 2026-05-17
**Decided by**: User (directive given in session synth-tpm-3b2c2c71, JSON-RPC id 69)
**Informed by**: CLARIFY message leak of `weekly_exec_review_v1` internal identifier
**Amends**: ADR-026 §Fix 5 (extends the layout-aware rendering principle to require a catalog abstraction)
**Implemented in**: ADR-034

---

## Context

During `authorSkill` for the TPM persona, the CLARIFY question shown to the skill
author contained the literal internal renderer preset identifier
`weekly_exec_review_v1`:

> "…or should the layout be recreated purely from the named layout preset
> (weekly_exec_review_v1)?"

This identifier is an internal renderer dispatch key, not a concept a skill author
needs to know about.  The leak happened because `framework/config/prompts/skill_builder.yaml`
hardcoded the string in two places in the `design_skill` prompt template:

1. The `workflow_shape.layout` schema example: `"layout": "weekly_exec_review_v1 | default"`
2. A rule: `Choose "weekly_exec_review_v1" layout only for exec-review PPTX skills.`

The DESIGN_SKILL LLM call read both, placed `weekly_exec_review_v1` in
`design.workflow_shape.layout`, and a CLARIFY question was generated that surfaced it
verbatim to the user.

The user's directive, verbatim:

> "there shouldnt be any preset sent to LLM in the first place which is not relevant
> to the user ask; if user hasn't specified, the LLM should look at the user ask and
> come up with something that's a good fit for the user ask, or look into existing
> presets and identify what's a good fit and then ask the user."

---

## Options Considered

### Option A — Remove the hardcoded rule; describe presets by purpose; inject catalog into prompt (CHOSEN)

- Create a **layout preset catalog** (`framework/renderers/layout_catalog.py`) as the
  single source of truth.  Each entry has: `internal_id` (renderer dispatch key, never
  shown to users), `human_label`, `description`, `when_to_use`, `output_format`,
  `structural_shape`.
- The prompt receives the catalog's DESCRIPTIONS (not the internal_id names) via a
  required template variable `{layout_preset_catalog}`.
- The LLM reasons over the catalog's `when_to_use` guidance and the user ask/artifact
  to select the best-fit entry.  It emits the `internal_id` in the machine field
  (so the renderer still dispatches) but selects it by description, not because
  it was the only name it was told.
- The clarify question guard (`_sanitize_clarify_question`) enforces that no
  `internal_id` survives into user-facing text at the surface level.

**Pros**: Catalog is the single source of truth — adding a new preset requires one
  edit to `layout_catalog.py` and zero prompt changes.  Renderer dispatch is unchanged.
  LLM selection is reasoned, not hardcoded.  Users only see plain language.

**Cons**: Adds a required template variable to every `design_skill` call site.
  Mitigated: all callers are in `conversation.py` (one site) + test fixtures (updated
  in the same change).

### Option B — Keep hardcoded rule; add a sanitizer-only fix

- Keep `weekly_exec_review_v1` in the prompt; strip it at output time.
- **Rejected**: the root cause (LLM being told to use a specific hardcoded identifier
  it then surfaces to the user) is unfixed.  New presets require prompt edits.
  The LLM picks a preset because it was the only name it was told, not because it
  reasoned about fit — this leads to incorrect preset selection for new use cases.

### Option C — Remove layout selection from LLM entirely; derive from artifact analysis

- Run a dedicated artifact-analysis pass (ADR-028) to choose the layout before DESIGN_SKILL.
- **Rejected for v1**: ADR-028's multimodal artifact analysis depth is a follow-up
  work item (open problem §8).  Option A achieves the user's directive today without
  waiting for full ADR-028 implementation.  Option C can replace Option A's catalog
  injection once ADR-028 depth is implemented.

---

## Decision

**Option A: Layout preset catalog as the single source of truth; prompts receive
descriptions only; LLM reasons over fit; clarify questions are sanitized.**

### Principles established (standing practice going forward)

1. **Prompts MUST NOT hardcode internal renderer/preset identifiers as instructions
   or defaults.**  An identifier like `weekly_exec_review_v1` is a renderer dispatch
   key and must never appear in a prompt template as a named option or rule.

2. **Layout selection is a reasoned step.**  From the user ask + any uploaded
   reference artifact, the LLM either derives an appropriate layout descriptor or
   best-fit-matches against a described preset catalog (presets described by
   purpose/structure, not bare internal names).

3. **Any confirmation surfaced to the user is in PLAIN LANGUAGE.**  Never an
   internal ID.  If layout confirmation is needed, phrase it as a structural
   description (e.g. "single dense summary slide vs. one slide per topic") — never
   a machine identifier.

4. **Only ask the user when genuinely ambiguous/low-confidence AND no artifact.**
   When the user supplied a reference artifact + clear intent, do NOT ask a layout
   question — proceed with the reasoned best-fit.

5. **Catalog is the single source of truth.**  Adding a new renderer preset =
   add to `framework/renderers/layout_catalog.py` only.  Prompt automatically
   receives the updated descriptions.  Renderer dispatch reads the same catalog.

---

## Consequences

- `design_skill` prompt v1.1 → v1.2: `layout_preset_catalog` added as required var.
- `_advance_to_clarify` sanitizes all question text before storage and surface, replacing
  any known internal_id with its human_label.
- `PptxRenderer.render()` dispatches via `layout_catalog.get_preset()` — adding a new
  preset no longer requires editing `pptx_renderer.py` unless it needs a new render path.
- All existing test fixtures and callers updated to supply `layout_preset_catalog`.

---

## Related

- ADR-034 (implementation: layout preset catalog design)
- ADR-026 §Fix 5 (original weekly_exec_review_v1 layout-aware rendering)
- ADR-028 (ANALYZE_ARTIFACT depth — future option for layout derivation from artifact)
- BUG-queue filed in same session per DECISION-013

---

*See also DECISION-013 (agent-discovered defect channel), ADR-026 (layout-aware PPTX origin).*
