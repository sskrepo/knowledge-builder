---
queue_id: BUG-queue-44364
source: user_report
tool: authorSkill
filed_at: 2026-05-15T20:32:56
status: open
---

# BUG-queue-44364

**Tool**: `authorSkill` | **Filed**: 2026-05-15 | **Status**: open

authorSkill EVAL step (13/16) fails deterministically for session synthId=synth-tpm-9571f396 (person…

<details>
<summary>Full details</summary>

**Description**:
authorSkill EVAL step (13/16) fails deterministically for session synthId=synth-tpm-9571f396 (persona tpm, skill 26ai_fa_db_upgrade_to_26ai_pptx). Skill was already committed and ingestion completed (4 pages). On 'yes, run eval', eval fails every time on the same sample: 'EVAL: extraction LLM call failed for sample https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=20090907433. Error: _llm_extract: could not parse LLM JSON response after sanitization. OCI JSON_OBJECT mode may have emitted unescaped control characters.' The LLM emitted a JSON object with all-empty string fields (project_name, current_phase, ... slack_channels []) that fails post-sanitization parsing. Reproduced twice identically (not transient). Note pageId=20090907433 is a child page auto-discovered from hub 20030556732 (not one of the two seed sources). Error references BUG-queue-573e3. Request: fix OCI JSON_OBJECT control-character escaping in _llm_extract and re-run eval / resume session synth-tpm-9571f396 server-side, as was done previously for this session.

**Triggering input**:
```json
{
  "synthId": "synth-tpm-9571f396",
  "input": "yes, run eval",
  "state": "INGEST->EVAL",
  "failing_sample": "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=20090907433"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: none-surfaced-see-synthId-synth-tpm-9571f396

</details>
