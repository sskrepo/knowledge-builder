---
queue_id: BUG-queue-ae5cd
source: user_report
tool: authorSkill
filed_at: 2026-05-16T20:26:30
status: open
---

# BUG-queue-ae5cd

**Tool**: `authorSkill` | **Filed**: 2026-05-16 | **Status**: open

test_non_email_skill_has_no_source_binding asserted on-disk existence of framework/workflow_skills/t…

<details>
<summary>Full details</summary>

**Description**:
test_non_email_skill_has_no_source_binding asserted on-disk existence of framework/workflow_skills/tpm/26ai_confluence_pptx.yaml. That skill is promoted in ADB and its authoring YAML was never committed (ADB is the source of truth). When untracked authoring byproducts were cleaned off disk, the test failed on a missing transient artifact — not a product regression. Failure count regressed from 8 to 9 baseline. Caught by agent trust-but-verify.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: agent-rca-fcae5e0

</details>
