---
queue_id: BUG-queue-18bc6
source: user_report
tool: authorSkill
filed_at: 2026-05-17T19:22:34
status: open
---

# BUG-queue-18bc6

**Tool**: `authorSkill` | **Filed**: 2026-05-17 | **Status**: open

Session synth-tpm-c3ef4ef2, JSON-RPC id 108. User uploaded reference artifact art-92062549 (2026-05-…

<details>
<summary>Full details</summary>

**Description**:
Session synth-tpm-c3ef4ef2, JSON-RPC id 108. User uploaded reference artifact art-92062549 (2026-05-14 FAaaS-LCM Update Kiwi Slide only 2.pptx, 8.2 MB). Artifact bound at UPLOAD_ARTIFACT_EXAMPLE. FSM re-entered UPLOAD_ARTIFACT_EXAMPLE (path: CONFIGURE_SOURCES -> INSPECT_SOURCES -> UPLOAD_ARTIFACT_EXAMPLE). Skip/reset branch silently nulled artifact_layout, artifact_path, artifact_reference_id. REVIEW_DESIGN kept displaying artifact (reads design.workflow_shape.layout text — never cleared). _run_eval read artifact_reference_id=None and emitted "No reference artifact was uploaded — structural comparison not available." Two root causes: (a) silent clearing of already-bound artifact on FSM re-entry; (b) REVIEW_DESIGN and _run_eval read decoupled fields and disagreed. Fix: ADR-035/DECISION-015 — has_bound_reference_artifact() single-source-of-truth; _bind_reference_artifact/_clear_reference_artifact atomic helpers; re-entry guard preserves binding on skip; conditional-required gate suppresses skip for pptx/docx; recovery script framework/cli/recover_bound_artifact_session.py; session synth-tpm-c3ef4ef2 recovered.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-6b5ddf3f

</details>
