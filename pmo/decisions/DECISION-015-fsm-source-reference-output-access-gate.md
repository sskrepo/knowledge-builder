# DECISION-015: FSM Source + Reference + Output Access-Verification Gate

**Status**: DECIDED
**Date**: 2026-05-17
**Decided by**: User (directive given in DECISION-015 session, 2026-05-17)
**Informed by**: RCA of session synth-tpm-c3ef4ef2 (artifact bound at UPLOAD_ARTIFACT_EXAMPLE,
  silently cleared on FSM re-entry; REVIEW_DESIGN and _run_eval read decoupled fields and disagreed)
**Implemented in**: ADR-035

---

## Context

Session `synth-tpm-c3ef4ef2`: user uploaded reference artifact `art-92062549` at
`UPLOAD_ARTIFACT_EXAMPLE`; it bound successfully (`artifact_reference_id` set,
`design.workflow_shape.layout` baked with the artifact name as a text string).  The FSM
later re-entered `UPLOAD_ARTIFACT_EXAMPLE` (path: CONFIGURE_SOURCES → INSPECT_SOURCES →
UPLOAD_ARTIFACT_EXAMPLE across design iterations).  The skip/reset branch in
`_handle_upload_artifact_example` (~line 1700-1705) nulled `artifact_layout`,
`artifact_path`, and `artifact_reference_id`.  `REVIEW_DESIGN` kept *displaying* the
artifact name (reads the never-cleared `design.workflow_shape.layout` text string ~line 2112)
while `_run_eval` (~line 4312, gate ~4443-4448) reads only `artifact_reference_id` (null)
and emitted "No reference artifact was uploaded — structural comparison not available."

Two root problems: (a) silent clearing of an already-bound artifact on FSM re-entry;
(b) "is an artifact bound" is read from two decoupled fields so `REVIEW_DESIGN` and
`_run_eval` could disagree.

---

## Approved Design

### 1. CONFIGURE_SOURCES must declare all three binding categories

`CONFIGURE_SOURCES` establishes (and the user confirms):
- (i) Input sources (Confluence pages, Jira queries)
- (ii) Reference artifact(s) — conditional on the rule below
- (iii) Output destination (delivery kind + config)

### 2. INSPECT_SOURCES must access-verify every declared item

Before DESIGN_SKILL is permitted, `INSPECT_SOURCES` must:
- Access-verify input sources (existing sample-fetch)
- Resolve reference artifact bytes from the artifact store (if declared)
- Verify output destination is reachable/writable per its delivery kind
- Produce a per-item access status (`source_access_status`)

### 3. Hard gate before DESIGN_SKILL

The FSM MUST NOT transition to `DESIGN_SKILL` until every REQUIRED item is verified
accessible.  If any required item is missing or inaccessible, route the user to
clarify/provide it (including `UPLOAD_ARTIFACT_EXAMPLE` for a missing required reference).

**Conditional-required rule for reference artifact:**
- REQUIRED (no skip option) when: output is a structured/templated artifact (`pptx` or `docx`) OR
  the user referenced a template/reference/example in their intent
- NOT REQUIRED (skip allowed) for pure text/email/markdown skills where no reference was
  ever declared

### 4. Single source of truth for "artifact bound"

Introduce one authoritative check (`has_bound_reference_artifact()` method) that BOTH
`REVIEW_DESIGN` display and `_run_eval` consult.  The invariant: the method returns True
IFF both `artifact_reference_id` AND `artifact_reference_name` are non-None/non-empty.
These are set atomically via `_bind_reference_artifact()` and cleared atomically via
`_clear_reference_artifact()`.  Reading `design.workflow_shape.layout` text is explicitly
prohibited as an artifact-bound signal.

### 5. Re-entry guard

Once a reference artifact is successfully bound, re-entering `UPLOAD_ARTIFACT_EXAMPLE`
MUST NOT silently clear it.  Default behavior on re-entry = keep the existing binding;
clearing/replacing requires an explicit deliberate user action (providing a new artifact
reference), not a bare "skip".  No silent loss anywhere (project rule: no silent
degradation).

### 6. Stash consistency (BUG-queue-f4987 stash)

The `_pending_artifact_stash` (from BUG-queue-f4987) must feed the single-source-of-truth
binding (via `_bind_reference_artifact()`) and must not be lost or double-cleared.

---

## Options Considered

### Option A — Single-truth method + atomic bind/clear + conditional-required gate (CHOSEN)

- Add `has_bound_reference_artifact()` method as the authoritative check
- Add `_bind_reference_artifact()` / `_clear_reference_artifact()` atomic helpers
- Add `artifact_reference_name` field (set at bind time, cleared at clear time)
- Re-entry guard: skip on re-entry = preserve binding
- Conditional-required: `_is_artifact_required()` derives from output_kind / intent text
- INSPECT_SOURCES accesses reference artifact bytes before allowing DESIGN_SKILL
- Hard gate: if required artifact missing after INSPECT_SOURCES → route back

**Pros**: Correct by construction — REVIEW_DESIGN and EVAL use the same method.
  Surgical — minimal new fields (artifact_reference_name, source_access_status,
  artifact_required, declared_output_destination). Backward-compat defaults for
  pre-ADR-035 sessions. Conditional-required rule avoids over-blocking text skills.

**Cons**: Requires updating all skip/error branches in _handle_upload_artifact_example.
  Mitigated: all branches are in one method and now all go through atomic helpers.

### Option B — Store a single "is_artifact_bound" boolean flag

- Add a boolean field `is_artifact_bound` to _SessionData
- Set True on successful bind, False on clear
- Both REVIEW_DESIGN and EVAL read `is_artifact_bound`

**Rejected**: A boolean flag adds a redundant truth alongside `artifact_reference_id`.
  Any divergence between the boolean and the id is a new category of bug.  Option A's
  method derives truth from the underlying fields — no additional field can diverge.

### Option C — Lift artifact binding into its own micro-state

- Add a new FSM state BIND_REFERENCE_ARTIFACT (between INSPECT_SOURCES and DESIGN_SKILL)
- This state owns the single binding; all re-entries go through it

**Rejected**: Adds a full FSM state for what is essentially a guard + atomic write.
  The guard + helpers approach in Option A achieves the same invariant without the
  session-schema churn of a new state.

---

## Decision

**Option A: Single-truth method + atomic bind/clear + conditional-required gate.**

### Principles established (standing practice going forward)

1. **`has_bound_reference_artifact()` is the single authoritative check.**
   REVIEW_DESIGN and _run_eval must both call this method.
   Reading `design.workflow_shape.layout` text to infer artifact binding is prohibited.

2. **All artifact binding goes through `_bind_reference_artifact()` / `_clear_reference_artifact()`.**
   Direct assignment to `artifact_reference_id` / `artifact_reference_type` outside
   these helpers is prohibited in new code.

3. **Re-entry must not silently clear a bound artifact.**
   Once bound, re-entering UPLOAD_ARTIFACT_EXAMPLE preserves the binding by default.
   Explicit new artifact reference = replace; bare "skip" = keep.

4. **Skip affordance is conditional.**
   For required artifacts (pptx/docx output or template referenced in intent), the
   'skip' option is suppressed in both `_advance_to_upload_artifact_example` and
   `_handle_upload_artifact_example`.

5. **No silent degradation.**
   Every branch that previously silently cleared the artifact and called `_run_design_skill()`
   now either hard-routes the user to provide the required artifact (when required), or
   explicitly calls `_clear_reference_artifact(reason=...)` with a logged reason (when not required).

---

## Consequences

- `_SessionData` gains four new fields (all backward-compat default-to-None/empty):
  `artifact_reference_name`, `source_access_status`, `artifact_required`,
  `declared_output_destination`
- `to_dict()` / `from_dict()` updated; pre-ADR-035 sessions load cleanly
- `_prompt_review_design()` now shows "Reference artifact: {name}" from the binding,
  not from `design.workflow_shape.layout` text
- `_run_eval` "no comparator" message reflects `has_bound_reference_artifact()` result
- Existing text/email/markdown skills are NOT blocked (conditional-required = False)
- Session `synth-tpm-c3ef4ef2` is recovered by recovery script
  `framework/cli/recover_bound_artifact_session.py`

---

## Related

- ADR-035 (implementation)
- ADR-027, ADR-028, ADR-029 (artifact example/analyze/comparator foundation)
- ADR-032, ADR-034 (recent FSM + rendering changes)
- BUG-queue-f4987 (stash auto-apply behavior, preserved and made consistent)
- DECISION-013 (agent-discovered defect channel)

---

*See also DECISION-013 (bug channel), DECISION-014 (no internal preset leak).*
