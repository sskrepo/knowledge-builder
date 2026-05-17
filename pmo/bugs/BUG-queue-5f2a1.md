---
queue_id: BUG-queue-5f2a1
source: user_report
tool: authorSkill
filed_at: 2026-05-17T00:00:00
status: open
---

# BUG-queue-5f2a1

**Tool**: `authorSkill` | **Filed**: 2026-05-17 | **Status**: open

EVAL always produced null structure_score and skill_matched=False because _run_eval called POST /api…

<details>
<summary>Full details</summary>

**Description**:
EVAL always produced null structure_score and skill_matched=False because _run_eval called POST /api/v1/ask which uses all_cards() (promoted-only ADR-033 invariant). A skill under authoring at EVAL state is not promoted, so it was never returned, causing wf_tier=2, skill_matched=False, structure_score=null on every EVAL run. Fix: replaced HTTP /api/v1/ask call with Path-A (in-process WorkflowExecutor.execute_from_config) and Path-B (resolve_only with scope=ingest_or_later). ADR-038, DECISION-018. Related session ref id 130.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: agent-rca-70bd018b

</details>
