---
queue_id: BUG-queue-f4987
source: user_report
tool: authorSkill
filed_at: 2026-05-17T16:42:39
status: open
---

# BUG-queue-f4987

**Tool**: `authorSkill` | **Filed**: 2026-05-17 | **Status**: open

JSON-RPC id 69. Session synth-tpm-3b2c2c71. User supplied 'artifact:2026-05-14 FAaaS-LCM Update Kiwi…

<details>
<summary>Full details</summary>

**Description**:
JSON-RPC id 69. Session synth-tpm-3b2c2c71. User supplied 'artifact:2026-05-14 FAaaS-LCM Update Kiwi Slide only 2.pptx id:art-3c90afba' at CLARIFY (step 3/17, clarify_next_state=REVIEW_DESIGN, 2 unresolved questions). The artifact: reference was silently consumed as the free-text answer to clarify Q[0] instead of being stashed for the UPLOAD_ARTIFACT_EXAMPLE step. art-3c90afba EXISTS in filestore (~/.kbf/store/uploads/synth-tpm-3b2c2c71/art-3c90afba/) — the upload succeeded; only the binding to the session failed. CLASSIFICATION: (1) weekly_exec_review_v1 preset mention = EXPECTED (from design.workflow_shape.layout set by LLM in DESIGN_SKILL — not a wrong artifact reference); (2) silent swallow of artifact: ref at CLARIFY = DEFECT (silent degradation). FIX: _handle_clarify_response now detects artifact:<filename> id:<id> prefix, stashes it in _pending_artifact_stash (persisted via to_dict/from_dict), re-asks the clarify question, and notifies the user. _handle_upload_artifact_example auto-applies the stash when user skips at the upload step. Fix commit: fix(authorskill): don't silently swallow artifact: upload ref supplied at CLARIFY.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: rpc-id-69

</details>
