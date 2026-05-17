---
queue_id: BUG-queue-6510d
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T12:41:32
status: open
---

# BUG-queue-6510d

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

Architect-RCA companion to user report BUG-queue-2ad9a. shim_workflows had no status gate: all_cards…

<details>
<summary>Full details</summary>

**Description**:
Architect-RCA companion to user report BUG-queue-2ad9a. shim_workflows had no status gate: all_cards() returned ALL on-disk cards including drafts, so the Tier-1 LLM router received draft+promoted skills indistinguishably. A promoted .eml skill silently returned a .pptx artifact (wrong-output silent substitution). Fix: ShimWorkflows made ADB-aware (mirrors ShimKb/ADR-015 Option B); all_cards() now filters to list_promoted_workflow_skills() from ADB. Disk YAML is authoring-only.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-8c2bec1

</details>
