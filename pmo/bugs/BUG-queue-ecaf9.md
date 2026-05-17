---
queue_id: BUG-queue-ecaf9
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-13T03:12:52
status: open
---

# BUG-queue-ecaf9

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-13 | **Status**: open

askKnowledgeBase fails with OCI Generative AI Inference 401 (INVALID_AUTHENTICATION_INFO) when query…

<details>
<summary>Full details</summary>

**Description**:
askKnowledgeBase fails with OCI Generative AI Inference 401 (INVALID_AUTHENTICATION_INFO) when querying real-content questions. Repro: called askKnowledgeBase with persona='tpm', question='What is the status of the 26ai project this week?' (and a longer variant naming the full set of exec-review fields). Both calls returned a 401 from POST https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com/20231130/actions/chat (Oracle-PythonSDK/2.174.0). Earlier in this session the same tool returned grounded 'no relevant context found' results (tier 1/2) without hitting the LLM at all — so this is a regression introduced sometime in the last hour, or specific to queries that route to the deeper LLM-synthesis tier. Suggested investigation: (1) verify the OCI principal / API key used by the kbf service for GenAI inference hasn't expired or been rotated; (2) check whether different code paths use different credentials (since the early queries didn't 401); (3) confirm the eu-frankfurt-1 region endpoint matches the kbf service's configured region. Same OCI Gen AI client issue may be related to the earlier LLM review failure noted in BUG-queue-51dd3 ('OciGenAiLLMClient' object has no attribute 'complete') — possible deployment in same area. Priority: blocks consumption flow for grounded questions.

**Triggering input**:
```json
{
  "tool": "askKnowledgeBase",
  "persona": "tpm",
  "questions_tried": [
    "26ai project weekly exec review \u2014 current status, RAG, accomplishments, milestones, risks, blockers, dependencies, exec asks, metrics, workstream status",
    "What is the status of the 26ai project this week?"
  ],
  "error_signature": "OCI generative_ai_inference 401 INVALID_AUTHENTICATION_INFO",
  "request_endpoint": "POST https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com/20231130/actions/chat",
  "opc_request_ids": [
    "9C8838A9ED9243398AB2A8F07DCDCD8C",
    "E8244B319EC64895B9E129C4ED75B7FD"
  ],
  "related_bug": "BUG-queue-51dd3 (OciGenAiLLMClient client misconfig)",
  "context": "Skill tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr was promoted to production earlier in this session; trying to invoke it via askKnowledgeBase yields the 401."
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: askKB-26ai-status-401

</details>
