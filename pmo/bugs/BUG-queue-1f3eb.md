---
queue_id: BUG-queue-1f3eb
source: user_report
tool: authorSkill
filed_at: 2026-05-18T01:03:14
status: open
---

# BUG-queue-1f3eb

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: open

CONFIGURE_SOURCES had no registry of supported connectors so it could not honestly reject unsupporte…

<details>
<summary>Full details</summary>

**Description**:
CONFIGURE_SOURCES had no registry of supported connectors so it could not honestly reject unsupported sources (e.g. Lumberjack). The system accepted any source_type string without error, proceeded through DESIGN_SKILL, and only failed at runtime when the ingestion pipeline tried to instantiate an adapter that does not exist. Capability-dishonesty failure: authoring silently produces skills referencing unsupported connectors that can never run = silent under-delivery.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: None

</details>
