---
queue_id: BUG-queue-5b233
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-13T00:57:23
status: open
---

# BUG-queue-5b233

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-13 | **Status**: open

Same failure class as BUG-queue-1b878 (raw OCI 400 leaked through), filing separately because the us…

<details>
<summary>Full details</summary>

**Description**:
Same failure class as BUG-queue-1b878 (raw OCI 400 leaked through), filing separately because the user-supplied input differs and either case may be reproduced independently while triaging the content-filter handling.\n\nTriggering input: question = \"<script>alert(1)</script> and ../../etc/passwd\". Result: tool error with the raw Oracle GenAI 400 'Inappropriate content detected!!!' payload, including OCI endpoint, region, SDK version, and opc-request-id.\n\nThe immediate user-visible effect is the same: callers passing arbitrary strings (script-tag/path-traversal-looking content) crash the tool instead of receiving tier_4 no_answer.

**Triggering input**:
```json
{
  "question": "<script>alert(1)</script> and ../../etc/passwd"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: B713252BDC124D4994BA70B2DA991438/37393D5E5B0ED2CFF34F5B8430E54797/EA37714B79DD45361CB87D0A3903FD7C

</details>
