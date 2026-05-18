---
queue_id: BUG-queue-b03d7
source: user_report
tool: synthesize_workflow/design_skill/pptx_renderer
filed_at: 2026-05-18T00:15:44
status: open
---

# BUG-queue-b03d7

**Tool**: `synthesize_workflow/design_skill/pptx_renderer` | **Filed**: 2026-05-18 | **Status**: open

DECISION-019 triple root-cause fix: RC1=author_fixed source_binding had no pinned_ref emitted (execu…

<details>
<summary>Full details</summary>

**Description**:
DECISION-019 triple root-cause fix: RC1=author_fixed source_binding had no pinned_ref emitted (executor fell to generic KB); RC2=design_skill prompt allowed prose layout IDs (no design-time validation); Finding-B=PptxRenderer silently fell back to stub on unresolvable layout ID. All three fixed in one coherent pass (RC1 Option A + RC2 Option A + Finding-B Option A). 28 new tests added, all pass. 0 new test regressions.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: req-d019-fix-86046d59

</details>
