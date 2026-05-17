---
queue_id: BUG-queue-a3f7e
source: agent_discovery
tool: kb-cli
filed_at: 2026-05-17T00:00:00
status: open
discovered_by: agent
severity: LOW
---

# BUG-queue-a3f7e

**Tool**: `kb-cli` | **Filed**: 2026-05-17 | **Status**: open | **Severity**: LOW

kb-cli export-skills fails with KeyError: extraction_schema on any skill artifact that does not have an extraction_schema key at the top level of the workflow_skill YAML…

<details>
<summary>Full details</summary>

**Description**:
kb-cli export-skills fails with KeyError: extraction_schema on any skill artifact that does not have an extraction_schema key at the top level of the workflow_skill YAML. Newly designed skills using the ADR-038 card format may not have extraction_schema if the synthesis step does not write it. cmd_export_skills does not guard with .get(). Status: open (not yet fixed — incidental finding during ADR-038 implementation).

**Root cause**:
cmd_export_skills in framework/cli/kb_cli.py accesses dict keys with [] subscript instead of .get() on the workflow_skill artifact dict. When extraction_schema is absent (e.g., new skill designs with ADR-038 card but no extraction schema yet defined), this raises KeyError. Fix: change to .get("extraction_schema", {}) throughout cmd_export_skills.

**Fix commit**: not yet fixed

**Discovered by**: agent (2026-05-17-adr038-implementation session)

**Request ID**: agent-rca-70bd018c

</details>
