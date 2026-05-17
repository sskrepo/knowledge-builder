---
queue_id: BUG-queue-5f2a1
source: agent_discovery
tool: authorSkill
filed_at: 2026-05-17T00:00:00
status: fixed
discovered_by: agent
severity: HIGH
---

# BUG-queue-5f2a1

**Tool**: `authorSkill` | **Filed**: 2026-05-17 | **Status**: fixed | **Severity**: HIGH

EVAL always produced null structure_score and skill_matched=False because _run_eval called POST /api/v1/ask which uses all_cards() (promoted-only invariant). A skill at EVAL is not yet promoted…

<details>
<summary>Full details</summary>

**Description**:
EVAL always produced null structure_score and skill_matched=False because _run_eval called POST /api/v1/ask which uses all_cards() (promoted-only ADR-033 invariant). A skill under authoring at EVAL state is not promoted, so it was never returned, causing wf_tier=2, skill_matched=False, structure_score=null on every EVAL run. Fix: replaced HTTP /api/v1/ask call with Path-A (in-process WorkflowExecutor.execute_from_config) and Path-B (resolve_only with scope=ingest_or_later). ADR-038, DECISION-018. Related session ref id 130.

**Root cause**:
_run_eval used urllib.request.urlopen to call POST /api/v1/ask. The /ask endpoint calls ShimWorkflows.all_cards() which is constrained to promoted-only skills per ADR-033. A skill at EVAL state is at most INGEST-state (not promoted), so it is invisible to all_cards(), making the routing always miss, wf_tier always 2, skill_matched always False, and structure_score always null. Fix: Path-A uses WorkflowExecutor.execute_from_config (bypasses router, uses skill config directly); Path-B uses resolve_only(scope=ingest_or_later) which includes draft/ingest-state skills.

**Fix commit**: feat(authorskill+eval): consumer-facing card + routing_queries at DESIGN_SKILL (DECISION-018, ADR-038)

**Discovered by**: agent (2026-05-17-adr038-implementation session)

**Request ID**: agent-rca-70bd018b

</details>
