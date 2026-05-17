---
queue_id: BUG-queue-440da
source: user_report
tool: authorSkill
filed_at: 2026-05-15T20:08:33
status: open
---

# BUG-queue-440da

**Tool**: `authorSkill` | **Filed**: 2026-05-15 | **Status**: open

authorSkill commit fails repeatedly at state PREVIEW (step 9/16, "ok, commit") for session synthId=s…

<details>
<summary>Full details</summary>

**Description**:
authorSkill commit fails repeatedly at state PREVIEW (step 9/16, "ok, commit") for session synthId=synth-tpm-9571f396 (persona tpm, skill 26ai_fa_db_upgrade_to_26ai_pptx). The skill design is fully approved (32-field schema, sources, weekly_exec_review_v1 layout, trigger on-request + cron '0 8 * * 1' Monday 08:00 PT). Commit attempted 3 times; ADB durable-store writes rejected with alternating errors: attempt 1 ORA-03146 (invalid buffer length for TTC field), attempt 2 ORA-03138 (connection terminated due to security policy violation), attempt 3 ORA-03146 again. The alternation between a TTC buffer-length error and a security-policy connection termination suggests an unstable ADB connection or a driver/payload-size issue when persisting the committed skill (possibly the large 32-field schema/state blob). Session state is preserved at PREVIEW and was previously successfully resumed by the team after BUG-queue-573e3, so requesting the same: stabilize the ADB write path and commit/resume session synth-tpm-9571f396. Filesystem artifacts may be partially written per the error message.

**Triggering input**:
```json
{
  "synthId": "synth-tpm-9571f396",
  "input": "ok, commit",
  "state": "PREVIEW"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: none-surfaced-see-synthId-synth-tpm-9571f396

</details>
