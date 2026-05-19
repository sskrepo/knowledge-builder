---
queue_id: BUG-queue-6b7a0
source: user_report
tool: authorSkill
filed_at: 2026-05-19T02:24:34
status: open
---

# BUG-queue-6b7a0

**Tool**: `authorSkill` | **Filed**: 2026-05-19 | **Status**: open

DECISION-013 BUG-2 (MEDIUM): routing over-trigger — single-fact RAG-status query routed to project_t…

<details>
<summary>Full details</summary>

**Description**:
DECISION-013 BUG-2 (MEDIUM): routing over-trigger — single-fact RAG-status query routed to project_tracking_stakeholder_status_email (email-agenda skill, Tier-1). Negative query 'What is the current RAG status for the project on this page?' over-triggers because it shares vocabulary ('rag', 'status', 'project', 'page') with the positive routing_queries, and the token-overlap penalty is insufficient when shared vocab dominates. do_not_use_for says 'single-fact lookups' but those tokens don't appear in the negative query. RCA layer: DESIGN_SKILL card-generation prompt (skill_builder.yaml:design_skill_card) did not instruct the LLM to produce do_not_invoke_if_phrases with concrete discriminative phrase fragments. The classifier already has hard-phrase exclusion logic (resolve_only). Fix: design_skill_card prompt v1.0->v1.1 now requires do_not_invoke_if_phrases with 3-7-word phrase fragments (e.g. 'what is the current rag', 'what is the current status'). Future-authored cards will carry these veto phrases. IMPORTANT: already-committed cards (including stuck sessions synth-tpm-8cb2adf7 / synth-tpm-afcacfc5) are NOT retroactively rewritten. User must re-design those skills in a fresh session to get corrected cards. Framework fix alone does not unblock already-committed stuck sessions. Status: fixed (framework layer). Fix commit: 2bfe2f4.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: decision013-bug2-7a73084d

</details>
