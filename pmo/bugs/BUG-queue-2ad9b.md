---
queue_id: BUG-queue-2ad9b
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-17T00:00:00
status: open
---

# BUG-queue-2ad9b

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-17 | **Status**: open

Routing-miss defect: consumer queries failed to route to the correct workflow skill because synthesi…

<details>
<summary>Full details</summary>

**Description**:
Routing-miss defect: consumer queries failed to route to the correct workflow skill because synthesize_workflow._build_skill_card() overwrote the LLM-generated DESIGN_SKILL card with a static template (authoring-intent wording). The static card contained phrases like "create a skill that..." rather than consumer-facing use descriptions, causing Tier-1 classifier token overlap to fail. Fix: _synthesize_preview now carries the DESIGN_SKILL card (including routing_queries) through unchanged (ADR-038 §B, DECISION-018). Related consumer report ref id 139.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: agent-rca-70bd018a

</details>
