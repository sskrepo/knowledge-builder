---
queue_id: BUG-queue-1b878
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-13T00:57:16
status: open
---

# BUG-queue-1b878

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-13 | **Status**: open

askKnowledgeBase surfaced a raw upstream Oracle Cloud Generative AI 400 ('Inappropriate content dete…

<details>
<summary>Full details</summary>

**Description**:
askKnowledgeBase surfaced a raw upstream Oracle Cloud Generative AI 400 ('Inappropriate content detected!!!') as the MCP tool error, instead of wrapping it in a KBF error envelope.\n\nIssues observed:\n1. The error leaks provider/implementation details to the caller: target_service=generative_ai_inference, request_endpoint includes the full OCI URL (https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com/...), Oracle-PythonSDK version, and OCI troubleshooting links.\n2. No KBF-level requestId field is present; only OCI's opc-request-id, which is what I'm submitting here as a best-effort.\n3. A user-style query string (in this case a benign-looking SQL fragment) should not crash the tool — it should route through the normal tiers and return tier_4 'no_answer' if it can't be grounded. Letting the content filter bubble up as a tool failure makes the tool brittle to arbitrary user input.\n\nTriggering input: question = \"'; DROP TABLE skills; --\" (no other params).\nA second, separate call with question = \"<script>alert(1)</script> and ../../etc/passwd\" produced the same failure mode (opc-request-id B713252BDC124D4994BA70B2DA991438/37393D5E5B0ED2CFF34F5B8430E54797/EA37714B79DD45361CB87D0A3903FD7C).\n\nSuggested fix: catch upstream 400s from the inference provider, log them server-side with a KBF requestId, and return a normal tier_4 response (or a structured KBF error containing only a requestId and a generic message) — do not pass the raw upstream JSON through to the MCP client.

**Triggering input**:
```json
{
  "question": "'; DROP TABLE skills; --"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: 69032FC228CA44A195F5EA4406A04CF7/1237E8AFCB3BCAC399D0885EF2537B43/CA750DFC3B9708300811583938DDF78D

</details>
