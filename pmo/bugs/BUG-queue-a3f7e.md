---
queue_id: BUG-queue-a3f7e
source: user_report
tool: kb-cli
filed_at: 2026-05-17T00:00:00
status: open
---

# BUG-queue-a3f7e

**Tool**: `kb-cli` | **Filed**: 2026-05-17 | **Status**: open

kb-cli export-skills fails with KeyError: extraction_schema on any skill artifact that does not have…

<details>
<summary>Full details</summary>

**Description**:
kb-cli export-skills fails with KeyError: extraction_schema on any skill artifact that does not have an extraction_schema key at the top level of the workflow_skill YAML. Newly designed skills using the ADR-038 card format may not have extraction_schema if the synthesis step does not write it. cmd_export_skills does not guard with .get(). Status: open (not yet fixed — incidental finding during ADR-038 implementation).

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: agent-rca-70bd018c

</details>
