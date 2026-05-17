---
queue_id: BUG-queue-2cb26
source: user_report
tool: authorSkill
filed_at: 2026-05-13T21:08:14
status: open
---

# BUG-queue-2cb26

**Tool**: `authorSkill` | **Filed**: 2026-05-13 | **Status**: open

authorSkill COMMIT step now reliably fails with KeyError: 'extraction_schema'. Repro: drive the stat…

<details>
<summary>Full details</summary>

**Description**:
authorSkill COMMIT step now reliably fails with KeyError: 'extraction_schema'. Repro: drive the state machine through ANALYZE_ARTIFACT (provide standard 15 fields), REVIEW_FIELDS (ok), REVIEW_SCHEMA (auto-generated rich descriptions, bump workstream_status maxLength to 2000 and metrics_snapshot maxLength to 2000, ok), CHECK_REUSE (yes, continue), CONFIGURE_SOURCES (confluence OCIFACP with labels: 26ai, weekly-status, exec-review; done), CONFIGURE_TRIGGERS (3, pptx, 0 13 * * 1) → arrives at PREVIEW. Sending 'yes, commit' returns: "❌ Commit failed — skill was NOT saved to the durable store. KeyError: 'extraction_schema'. Filesystem files may or may not have been written; ADB rejected the write. Fix the underlying issue (ADB connectivity, schema, etc.) and retry." Calling 'retry commit' returns the identical error every time. This is a regression — earlier sessions today (synth-tpm-dd1d4865, synth-tpm-0e0c5cc1, synth-tpm-14a54555) committed successfully with the exact same input pattern. Affected session: synth-tpm-5bd6eb13. Likely cause: the persisted DTO building during COMMIT references a key 'extraction_schema' that was renamed/moved in a recent change but the COMMIT writer wasn't updated. Suggested fix: locate the dict access keyed by 'extraction_schema' in the COMMIT path and align it with the current schema-spec field name (maybe 'schema', 'parser_schema', 'fields', or 'field_specs'). Priority: high — blocks all new skill authoring on this instance.

**Triggering input**:
```json
{
  "affected_session": "synth-tpm-5bd6eb13",
  "state_at_failure": "PREVIEW \u2192 COMMIT transition",
  "error": "KeyError: 'extraction_schema'",
  "retry_command": "retry commit",
  "retry_result": "identical KeyError",
  "earlier_sessions_that_succeeded": [
    "synth-tpm-dd1d4865",
    "synth-tpm-0e0c5cc1",
    "synth-tpm-14a54555"
  ],
  "schema_state_at_commit": "15 standard exec-review fields with auto-rich descriptions and 2 maxLength bumps (workstream_status, metrics_snapshot \u2192 2000)"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: commit-KeyError-extraction_schema

</details>
