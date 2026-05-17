---
queue_id: BUG-queue-80e55
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-17T21:07:53
status: open
---

# BUG-queue-80e55

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-17 | **Status**: open

User explicitly requested: use promoted tpm.faaas_kiwi_project_pptx skill and produce a FAaaS Kiwi p…

<details>
<summary>Full details</summary>

**Description**:
User explicitly requested: use promoted tpm.faaas_kiwi_project_pptx skill and produce a FAaaS Kiwi project update PPTX. The askKnowledgeBase call appears to enter execution but fails with OCI Generative AI Inference 401 INVALID_AUTHENTICATION_INFO, so no PPTX artifact is produced.

**Triggering input**:
```json
{
  "question": "Use the promoted tpm.faaas_kiwi_project_pptx skill to produce a new FAaaS Kiwi project update PPTX from https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project. Generate the PowerPoint file now.",
  "persona": "tpm",
  "page_id": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project",
  "maxResults": 10
}
```

**User ID**: anon-dev
**Request ID**: req-3e9380b7

</details>
