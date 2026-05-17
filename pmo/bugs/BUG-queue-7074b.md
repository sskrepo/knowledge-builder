---
queue_id: BUG-queue-7074b
source: user_report
tool: reviewSkillSession
filed_at: 2026-05-16T20:21:33
status: open
---

# BUG-queue-7074b

**Tool**: `reviewSkillSession` | **Filed**: 2026-05-16 | **Status**: open

The broad 'except Exception as exc' in _run_llm_review embedded raw OCI error details (opc-request-i…

<details>
<summary>Full details</summary>

**Description**:
The broad 'except Exception as exc' in _run_llm_review embedded raw OCI error details (opc-request-id, endpoint, status) directly into the persisted BugToFile.detail, and misclassified a provider content-safety block as a skill defect (llm_review_failed: minor). Fix: detect content-filter error, emit advisory check (llm_review_content_filtered) with KBF- correlation ID only and no provider internals.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-d59bbda

</details>
