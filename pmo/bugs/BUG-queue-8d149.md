---
queue_id: BUG-queue-8d149
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-17T06:25:46
status: open
---

# BUG-queue-8d149

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-17 | **Status**: open

Response contract gap: tier-4/no_answer path from routing miss did not carry server-side request_id.…

<details>
<summary>Full details</summary>

**Description**:
Response contract gap: tier-4/no_answer path from routing miss did not carry server-side request_id. Content-filter tier-4 path DID carry request_id (KBF-xxxx UUID). Routing-miss tier-4 (no matching skill) did not. Consumer could not correlate the no_answer response with server-side log entries. Fix: _build_ask_response() now generates KBF-<uuid> request_id for ALL tier-4 responses that lack one, ensuring consistent observability on both tier-4 paths.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: None

</details>
