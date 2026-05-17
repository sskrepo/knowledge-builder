---
queue_id: BUG-queue-f3c6c
source: user_report
tool: authorSkill
filed_at: 2026-05-17T17:54:30
status: open
---

# BUG-queue-f3c6c

**Tool**: `authorSkill` | **Filed**: 2026-05-17 | **Status**: open

CLARIFY question leaked internal renderer preset identifier weekly_exec_review_v1 to skill author. S…

<details>
<summary>Full details</summary>

**Description**:
CLARIFY question leaked internal renderer preset identifier weekly_exec_review_v1 to skill author. Session synth-tpm-3b2c2c71, JSON-RPC id 69. Root cause: design_skill prompt v1.1 hardcoded the identifier in schema example (layout: weekly_exec_review_v1 | default) and in layout rule. LLM parroted it into workflow_shape.layout and into a blocking_question shown to the user. Fixed by ADR-034: layout_catalog.py as single source of truth; prompts receive catalog descriptions not ids; _sanitize_clarify_question guard added.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-adr034

</details>
