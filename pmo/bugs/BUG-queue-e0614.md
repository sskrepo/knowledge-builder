---
queue_id: BUG-queue-e0614
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T21:29:58
status: open
---

# BUG-queue-e0614

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

DESIGN/PROCESS GAP (no isError requestId — behavioral, not a tool crash; correlation id = authoring …

<details>
<summary>Full details</summary>

**Description**:
DESIGN/PROCESS GAP (no isError requestId — behavioral, not a tool crash; correlation id = authoring session synth-tpm-1eee2379 for skill tpm.project_tracking_stakeholder_tracking_meeting_email).

Summary: The "Use a skill" (consumption) flow has no provision for ASK-TIME INGESTION of a user-provided source. This skill was authored with the explicit contract "accept a Confluence page at runtime and draft from THAT page." At consumption time the user supplied Confluence pageId=18625350641 (URL https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=18625350641). That page is not in the KB. Two independent content-based semantic askKnowledgeBase queries (persona=tpm) for that page's actual project (RODS Support for Dynamic Tables Replication, FAAASPMO-1137) returned tier-3/tier-4 with zero citations — so this is a true ingestion gap, not a query-phrasing artifact.

Defect 1 — ingest non-persistence: Authoring session synth-tpm-1eee2379 INGEST/DONE explicitly reported "Processed 1 pages: 1 new" and "KB populated: 1 pages ingested (mode: live). Routing will return real content." for pageId=18625350641 (configured source URL https://confluence.oraclecorp.com/confluence/x/8UsoVgQ). At runtime the consumption KB does NOT contain that page (only pageId=20030556732 is present). Build-time live ingest did not persist into the KB the consumption flow reads.

Defect 2 — silent wrong-source substitution (correctness risk): When the requested page is absent, the single-source skill did not fail or warn — it silently retrieved and drafted a confident, complete prep-agenda from a DIFFERENT project page (pageId=20030556732, "FA DB Upgrade 19c→26ai") while the user had asked for 18625350641 (RODS). A prep agenda for the wrong meeting presented as correct is worse than an explicit failure and contradicts the skill's stated "single-source, never guesses/substitutes" design.

Recommended fix (from the skill author/TPM): Add an ADR + an authoring-time question in the authorSkill flow: "Does this skill require ask-time ingestion of the user-provided source?" If yes, the compiled workflow must, at consumption time: (a) detect whether the provided page/source is already ingested; (b) if not, ingest it (or explicitly block and request ingestion) BEFORE retrieval/generation; and (c) never substitute a different source — if the requested source cannot be ingested/retrieved, return a clear no-answer/own-source-missing error instead of drafting from an unrelated page. This makes the runtime behavior match the skill's authored contract.

**Triggering input**:
_not recorded_

**User ID**: 218a5f843d6c3eee
**Request ID**: synth-tpm-1eee2379

</details>
