---
queue_id: BUG-queue-2ad9b
source: agent_discovery
tool: askKnowledgeBase
filed_at: 2026-05-17T00:00:00
status: fixed
discovered_by: agent
severity: HIGH
---

# BUG-queue-2ad9b

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-17 | **Status**: fixed | **Severity**: HIGH

Routing-miss defect: consumer queries failed to route to the correct workflow skill because synthesize_workflow._build_skill_card() overwrote the LLM-generated DESIGN_SKILL card…

<details>
<summary>Full details</summary>

**Description**:
Routing-miss defect: consumer queries failed to route to the correct workflow skill because synthesize_workflow._build_skill_card() overwrote the LLM-generated DESIGN_SKILL card with a static template (authoring-intent wording). The static card contained phrases like "create a skill that..." rather than consumer-facing use descriptions, causing Tier-1 classifier token overlap to fail. Fix: _synthesize_preview now carries the DESIGN_SKILL card (including routing_queries) through unchanged (ADR-038 §B, DECISION-018). Related consumer report ref id 139.

**Root cause**:
synthesize_workflow._build_skill_card() was called unconditionally in synthesize_workflow_skill() and overwrote any LLM-generated skill_card present in the artifact dict. The static template used authoring-intent language ("This skill is designed to...") instead of consumer-facing description, producing low token-overlap scores in the Tier-1 classifier and causing routing to fall through to Tier-2 (LLM) for queries that should have matched at Tier-1. Fix: _synthesize_preview() explicitly replaces wf_struct["skill_card"] with design_skill_card after synthesize_workflow_skill() returns, if design_skill_card is set in session data.

**Fix commit**: feat(authorskill+eval): consumer-facing card + routing_queries at DESIGN_SKILL (DECISION-018, ADR-038)

**Discovered by**: agent (2026-05-17-adr038-implementation session)

**Request ID**: agent-rca-70bd018a

</details>
