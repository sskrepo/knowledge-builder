---
queue_id: BUG-queue-43ac1
source: user_report
tool: authorSkill
filed_at: 2026-05-18T21:48:17
status: open
---

# BUG-queue-43ac1

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: open

DECISION-013 BUG: ADR-039 bind-side canonicalization gap. derive_pinned_source stores raw display UR…

<details>
<summary>Full details</summary>

**Description**:
DECISION-013 BUG: ADR-039 bind-side canonicalization gap. derive_pinned_source stores raw display URL, never calls canonical_identity(). Session synth-tpm-58a9780c: source_binding.pinned_ref=raw display URL, canonical_ref=(none). Surfaces as ERROR_TRANSIENT/ConfluencePageNotInKBError at EVAL. HIGH severity.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: architect-decision013-BUG-queue-43ac1

</details>
