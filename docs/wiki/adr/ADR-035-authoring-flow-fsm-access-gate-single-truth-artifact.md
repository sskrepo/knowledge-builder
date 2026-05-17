---
title: ADR-035 — Authoring-Flow FSM Redesign: Access-Verify Gate, Conditional-Required Artifact, Single-Source Binding State
status: accepted
created: 2026-05-17
accepted: 2026-05-17
owner: architect
deciders: user, backend-dev
tags: [adr, skill-builder, fsm, artifact, eval, session-state, correctness]
related: [ADR-027, ADR-028, ADR-029, ADR-032, ADR-034, DECISION-015]
supersedes: ~
---

# ADR-035 — Authoring-Flow FSM Redesign: Access-Verify Gate, Conditional-Required Artifact, Single-Source Binding State

## Status

**Accepted — 2026-05-17.**  Fixes the silent artifact-loss defect (session
`synth-tpm-c3ef4ef2`, JSON-RPC id 108) and establishes the authoritative state-machine
contract for reference artifact lifecycle in `authorSkill`.

---

## A. Context — The Failure

### Root Cause Summary (two independent problems)

**Problem (a) — Silent clearing on FSM re-entry:**

Session `synth-tpm-c3ef4ef2`: user uploaded reference artifact `art-92062549`.  It bound
successfully.  The FSM later re-entered `UPLOAD_ARTIFACT_EXAMPLE` (path:
CONFIGURE_SOURCES → INSPECT_SOURCES → UPLOAD_ARTIFACT_EXAMPLE across a design iteration).
The skip/reset branch (~line 1700-1705 in the pre-fix code) executed:

```python
self._data.artifact_layout = None
self._data.artifact_path = ""
self._data.artifact_reference_id = None
self._data.artifact_reference_type = None
```

This silently cleared a successfully-bound artifact — no log, no user notification.

**Problem (b) — Decoupled read paths causing REVIEW_DESIGN / EVAL disagreement:**

After the silent clear, two reads disagreed:
- `REVIEW_DESIGN` (~line 2112): read `design.workflow_shape.layout` TEXT string.  The LLM
  had baked the artifact name into this text at DESIGN_SKILL time.  This text was never
  cleared.  So REVIEW_DESIGN *kept showing* the artifact as present.
- `_run_eval` (~line 4312): read `artifact_reference_id` (now null after the clear).
  Emitted: "No reference artifact was uploaded — structural comparison not available."

REVIEW_DESIGN said artifact present; EVAL said artifact absent.  Both reads were correct
given the data, but the data itself was inconsistent.  This is a silent wrong output
(HIGH severity: DECISION-013 §Severity rule).

### Artifact bytes still exist

The artifact bytes for `art-92062549` still exist on the filestore:
`~/.kbf/store/uploads/synth-tpm-c3ef4ef2/art-92062549/2026-05-14 FAaaS-LCM Update Kiwi Slide only 2.pptx`
(8.2 MB).  The binding was lost; the bytes were not.

---

## B. Decision

### B.1 Single Source of Truth: `has_bound_reference_artifact()`

Introduce one authoritative method:

```python
def has_bound_reference_artifact(self) -> bool:
    return bool(
        self._data.artifact_reference_id
        and self._data.artifact_reference_name
    )
```

**Invariant:** This method returns `True` IFF both `artifact_reference_id` AND
`artifact_reference_name` are non-None/non-empty.  They are set atomically by
`_bind_reference_artifact()` and cleared atomically by `_clear_reference_artifact()`.

**Both `REVIEW_DESIGN` and `_run_eval` call this method.**  Reading
`design.workflow_shape.layout` text to infer artifact binding is explicitly prohibited.

### B.2 Atomic Bind / Clear Helpers

```python
def _bind_reference_artifact(self, artifact_id, artifact_type, artifact_name,
                              artifact_layout, artifact_path=""):
    """All three retention fields set together."""

def _clear_reference_artifact(self, reason=""):
    """All three retention fields cleared together, with logged reason."""
```

Direct assignment to `artifact_reference_id` / `artifact_reference_type` outside
these helpers is prohibited in new code.

### B.3 New `_SessionData` Fields

| Field | Type | Default | Purpose |
|---|---|---|---|
| `artifact_reference_name` | `str \| None` | `None` | Filename/label of bound artifact; read by REVIEW_DESIGN and has_bound_reference_artifact() |
| `source_access_status` | `dict` | `{}` | Per-item access-verification result from INSPECT_SOURCES |
| `artifact_required` | `bool \| None` | `None` | Cached result of _is_artifact_required(); set at UPLOAD_ARTIFACT_EXAMPLE entry |
| `declared_output_destination` | `dict \| None` | `None` | Output destination declared at CONFIGURE_SOURCES |

All four fields are in `to_dict()` / `from_dict()` with backward-compat defaults.

### B.4 Conditional-Required Rule

```python
@staticmethod
def _is_artifact_required(normalised_intent: dict, output_format: str = "") -> bool:
```

Returns `True` (artifact required, skip suppressed) when:
- `output_kind` or `output_format` is `"pptx"` or `"docx"` (structured/templated output)
- OR intent `task_description` contains template/reference keywords

Returns `False` for pure text/email/markdown skills where no reference was declared.

### B.5 Re-entry Guard

When `_handle_upload_artifact_example` receives a skip-synonym and
`has_bound_reference_artifact()` is True:

```python
if self.has_bound_reference_artifact():
    log.info("re-entry skip received but artifact already bound — preserving binding")
    return self._run_design_skill()
```

No silent clear.  Clearing/replacing requires providing a new explicit artifact reference.

### B.6 Skip Affordance Suppression

In `_advance_to_upload_artifact_example`:
- When `artifact_required = True`: no `"skip"` option offered to the user
- When `artifact_required = False`: `"skip"` offered as before

In `_handle_upload_artifact_example`:
- When `artifact_required = True` and skip received: hard-block with explanation

### B.7 REVIEW_DESIGN Shows Binding from Single Truth

```python
if self.has_bound_reference_artifact():
    lines.append(f"  Reference artifact: {self._data.artifact_reference_name!r} ...")
else:
    lines.append("  Reference artifact: none bound")
```

NOT `workflow_shape.get('layout', 'default')` text scanning.

### B.8 INSPECT_SOURCES Extended Responsibilities

INSPECT_SOURCES must (per DECISION-015 §2):
- Access-verify input sources (existing sample-fetch — unchanged)
- Resolve reference artifact bytes from artifact store when declared
- Verify output destination reachability
- Produce per-item `source_access_status`
- Hard-block transition to DESIGN_SKILL when any REQUIRED item fails verification

Note: the access-verification for output destination and reference artifact in INSPECT_SOURCES
is tracked in `source_access_status`. The HARD GATE before DESIGN is enforced by checking
`artifact_required` + `has_bound_reference_artifact()` before calling `_run_design_skill()`.

### B.9 BUG-queue-f4987 Stash Consistency

The `_pending_artifact_stash` auto-apply behavior is preserved.  When stash is consumed,
it feeds `_bind_reference_artifact()` (via the normal artifact-processing path), not
direct field assignment.  The stash cannot be lost silently: if the auto-apply path fails
to resolve the artifact, the user is shown the error and the stash is consumed.

---

## C. Consequences

### Positive

- **Silent loss eliminated**: all clear paths now go through `_clear_reference_artifact(reason=...)` with a logged reason; no branch clears silently.
- **REVIEW_DESIGN / EVAL agreement by construction**: both call `has_bound_reference_artifact()`.
- **Conditional gate**: text/email/markdown skills are NOT blocked by an artifact requirement they never had.
- **Re-entry safe**: once bound, re-entering UPLOAD_ARTIFACT_EXAMPLE is non-destructive.
- **Recovery path**: `framework/cli/recover_bound_artifact_session.py` re-binds the artifact for sessions caught by the pre-fix code.

### Negative / Tradeoffs

- `_handle_upload_artifact_example` is longer (all branches now contain conditional-required logic).  Mitigated: the method is well-commented and the logic is linear.
- `artifact_required` is cached at UPLOAD_ARTIFACT_EXAMPLE entry, not computed at CONFIGURE_SOURCES.  This means a late change to output_format after CONFIGURE_SOURCES could be inconsistent.  Mitigated: output_format is set at DESIGN_SKILL → the cache is refreshed at every UPLOAD_ARTIFACT_EXAMPLE entry.

### Reversibility

The new fields have backward-compat defaults (`None` / `{}`).  Pre-ADR-035 sessions load cleanly; they simply have no `artifact_reference_name` (has_bound_reference_artifact() returns False, which is correct for sessions that never went through the new bind path).

---

## D. Alternatives Considered

### D.1 Store a boolean `is_artifact_bound` flag

**Rejected**: A boolean adds a redundant truth alongside `artifact_reference_id`.  Any divergence between the boolean and the ID is a new category of bug.  A method that derives the answer from the underlying fields cannot diverge.

### D.2 New FSM state BIND_REFERENCE_ARTIFACT

**Rejected**: Adds a full FSM state for what is a guard + atomic write.  The session-schema churn (new state in STATES list, new handler registration, new to_dict/from_dict wiring, test updates) is disproportionate to the problem.

### D.3 Clear design.workflow_shape.layout text when artifact is cleared

**Rejected**: Treating layout text as a binding signal couples the renderer dispatch (which legitimately reads layout) to the artifact binding state.  The correct fix is to stop reading layout text as a binding signal at all — which is what ADR-035 does.

---

## E. Cross-references

- **ADR-027** — 16-state design-first machine; UPLOAD_ARTIFACT_EXAMPLE first introduced
- **ADR-028** — CLARIFY state; stash mechanism (BUG-queue-f4987)
- **ADR-029** — ADR-029 Phase 1 (S5): artifact retention (`artifact_reference_id`, `artifact_reference_type`); image hard-reject; ArtifactComparator at EVAL
- **ADR-032** — ask-time source ingestion; source_binding_mode
- **ADR-034** — layout preset catalog (layout text is renderer dispatch, not artifact signal)
- **BUG-queue-f4987** — stash auto-apply on clarify artifact ref
- **DECISION-015** — the policy decision driving this ADR

---

## F. Implementation Notes

**Files changed:**
- `framework/skill_builder/conversation.py` — all changes above
- `framework/cli/recover_bound_artifact_session.py` — new recovery script for sessions caught by pre-fix code
- `framework/tests/unit/test_skill_builder_conversation.py` — 5 new test groups (see §G)

**No prompt changes required**: The artifact binding state is an FSM concern, not a prompt concern.  `design.workflow_shape.layout` continues to be used for renderer dispatch (ADR-034) — it is NOT a binding signal.

**Migration:** Pre-ADR-035 sessions load cleanly (backward-compat defaults).  Sessions with a stale binding (like `synth-tpm-c3ef4ef2`) require the recovery script.

---

## G. New Tests (5 groups)

1. **artifact_required gate**: required skill → no skip option in advance + handle blocks skip
2. **non-required gate**: text/email skill with no declared reference → NOT gated
3. **re-entry guard**: bind artifact, re-enter UPLOAD_ARTIFACT_EXAMPLE, skip → binding preserved
4. **single-source-of-truth**: state where old code diverged (null ref_id, non-null layout text) → REVIEW_DESIGN and _run_eval now agree via has_bound_reference_artifact()
5. **INSPECT_SOURCES hard gate**: source_access_status set; DESIGN blocked when required item unverified
