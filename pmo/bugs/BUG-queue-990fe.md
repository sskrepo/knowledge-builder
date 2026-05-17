---
queue_id: BUG-queue-990fe
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T22:13:24
status: open
---

# BUG-queue-990fe

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

Fresh askKnowledgeBase call for tpm.project_tracking_stakeholder_status_email was explicitly scoped …

<details>
<summary>Full details</summary>

**Description**:
Fresh askKnowledgeBase call for tpm.project_tracking_stakeholder_status_email was explicitly scoped to pageId 18625350641 but returned no_answer saying the page is not in KB, while still citing pageId 20030556732. This indicates retrieval/routing contamination and inability to use the promoted skill for the target page.

**Triggering input**:
persona=tpm; functionalArea=project_tracking_stakeholder_status_email; requestedPageId=18625350641; returnedCitationPageId=20030556732; curlCallId=101

**User ID**: anon-dev
**Request ID**: req-noanswer-askKnowledgeBase-101

</details>
