---
queue_id: BUG-queue-f7d90
source: user_report
tool: authorSkill
filed_at: 2026-05-18T22:01:00
status: open
---

# BUG-queue-f7d90

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: open

Issue-1a: _run_eval constructed WorkflowExecutor with no retrievers or wiki_store, so _retrieve_auth…

<details>
<summary>Full details</summary>

**Description**:
Issue-1a: _run_eval constructed WorkflowExecutor with no retrievers or wiki_store, so _retrieve_author_fixed_pinned Strategy 1 was entirely skipped (self.retrievers={} is falsy) even when the page was correctly written by _run_ingest. Result: false ingest_result:success with ConfluencePageNotInKBError at EVAL time for all author_fixed+!ingest_on_demand skills. Filed JSONL-only in prior session (ADB unavailable); re-filing now ADB is reachable. Fixed: executor Strategy 1b (direct WikiMetadataStore lookup by canonical_id) + _run_eval wires wiki_store + mcp_server wires wiki_store into production executor.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: architect-remediation-BUG-queue-f7d90

</details>
