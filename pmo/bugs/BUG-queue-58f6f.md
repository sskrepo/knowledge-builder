---
queue_id: BUG-queue-58f6f
source: user_report
tool: authorSkill
filed_at: 2026-05-13T06:22:21
status: open
---

# BUG-queue-58f6f

**Tool**: `authorSkill` | **Filed**: 2026-05-13 | **Status**: open

'rename skill to <name>' command is parsed as a field list at the ANALYZE_ARTIFACT step. The system …

<details>
<summary>Full details</summary>

**Description**:
'rename skill to <name>' command is parsed as a field list at the ANALYZE_ARTIFACT step. The system message at ANALYZE_ARTIFACT explicitly tells the user: "Note: your skill has been auto-named '<long_auto_name>'. You can type 'rename skill to <shorter_name>' at any point before COMMIT to use a shorter, more descriptive name." Following that exact instruction at ANALYZE_ARTIFACT does NOT rename the skill — instead the entire phrase gets parsed as a comma-separated field-name list, producing a single bogus field. Repro: at state ANALYZE_ARTIFACT (step 2), send input "rename skill to weekly_26ai_exec_review". State advances to REVIEW_FIELDS with field list: ["rename_skill_to_weekly_26ai_exec_review"]. The skill name is NOT changed. Affected session: synth-tpm-7621f1d2. Recommended fix: either (a) detect 'rename skill to ...' as a command at every state where the message advertises it, and apply the rename without consuming it as field input, or (b) tighten the message so it only mentions rename at states where it actually works. Bonus: an explicit response confirming the rename happened (e.g., "Skill renamed to weekly_26ai_exec_review.") would prevent silent failures even when it does work.

**Triggering input**:
```json
{
  "affected_session": "synth-tpm-7621f1d2",
  "state_when_command_sent": "ANALYZE_ARTIFACT (step 2)",
  "input_sent": "rename skill to weekly_26ai_exec_review",
  "actual_result": "advanced to REVIEW_FIELDS with field 'rename_skill_to_weekly_26ai_exec_review' \u2014 skill not renamed",
  "expected_result": "skill renamed to weekly_26ai_exec_review, state machine stays at ANALYZE_ARTIFACT awaiting artifact/field list",
  "misleading_message": "your skill has been auto-named '<X>'. You can type 'rename skill to <shorter_name>' at any point before COMMIT"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: rename-skill-eaten-as-field-list

</details>
