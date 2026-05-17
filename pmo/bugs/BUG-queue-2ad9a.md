---
queue_id: BUG-queue-2ad9a
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T18:49:13
status: open
---

# BUG-queue-2ad9a

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

Promoted skill tpm.project_tracking_weekly_stakeholder_status_email cannot be used to generate the r…

<details>
<summary>Full details</summary>

**Description**:
Promoted skill tpm.project_tracking_weekly_stakeholder_status_email cannot be used to generate the requested .eml. One consumption call routed to tier=1 but returned artifact_path /Users/sravansunkaranam/.kbf/outputs/26ai_confluence_pptx.pptx and Answer said output_eml had no support; stricter call with functionalArea project_tracking_weekly_stakeholder_status_email fell back to no_answer. Expected an .eml artifact/content from project_tracking_weekly_stakeholder_status_email and no automatic email sending.

**Triggering input**:
```json
{
  "skill": "tpm.project_tracking_weekly_stakeholder_status_email",
  "source": "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=20030556732",
  "expected_output": ".eml artifact",
  "observed_1": "tier=1 workflow_skill but artifact_path points to 26ai_confluence_pptx.pptx; output_eml no support",
  "observed_2": "tier=4 no_answer when functionalArea forced"
}
```

**User ID**: anon-dev
**Request ID**: askKnowledgeBase-49-50

</details>
