---
queue_id: BUG-queue-4fd5e
source: user_report
tool: deleteSkill
filed_at: 2026-05-13T06:22:33
status: open
---

# BUG-queue-4fd5e

**Tool**: `deleteSkill` | **Filed**: 2026-05-13 | **Status**: open

deleteSkill removes artifacts from the DB-backed skill store but does not decrement / refresh the pe…

<details>
<summary>Full details</summary>

**Description**:
deleteSkill removes artifacts from the DB-backed skill store but does not decrement / refresh the per-persona skill count that authorSkill IDENTIFY_PERSONA reads. Repro: (1) before this session, listSkills showed tpm persona with several skills; authorSkill IDENTIFY_PERSONA reported "tpm — TPM Knowledge Builder (6 skills)". (2) Deleted 3 tpm skills via deleteSkill (BUG-queue-N/A — successful deletes for weekly_26ai_executive_review_ppt_using_faaas_proje, generate_a_weekly_exec_review_pptx_for_the_26ai_pr, weekly_26ai_exec_review_ppt, weekly_26ai_exec_review_from_tpm_weekly_ops; that is 4 deletes). (3) After deletes, listSkills correctly shows 3 tpm skills remaining. (4) But authorSkill IDENTIFY_PERSONA STILL reports "tpm — TPM Knowledge Builder (6 skills)" (sessions synth-new-c20938dd, synth-new-7eebc043, synth-new-70b6bfeb all show tpm: 6). Same disconnect we filed in BUG-queue-e8298 in the other direction: authorSkill writes to filesystem YAML / persona builders; listSkills/getSkill/deleteSkill operate on the DB store. The two registries do not stay in sync — promote doesn't update DB, and delete doesn't update YAML / persona-builder counts. Recommended fix: make deleteSkill also update the persona builder YAML (or whatever source IDENTIFY_PERSONA queries) so the count reflects reality. Long-term: collapse the two registries into one source of truth.

**Triggering input**:
```json
{
  "related_bug": "BUG-queue-e8298 (inverse: authorSkill writes filesystem; listSkills reads DB store)",
  "deletes_performed_this_session": [
    "tpm.weekly_26ai_executive_review_ppt_using_faaas_proje",
    "tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr",
    "tpm.weekly_26ai_exec_review_ppt",
    "tpm.weekly_26ai_exec_review_from_tpm_weekly_ops"
  ],
  "listSkills_after_deletes_tpm_count": 3,
  "authorSkill_IDENTIFY_PERSONA_tpm_count_after_deletes": 6,
  "sessions_observed_with_stale_count": [
    "synth-new-c20938dd",
    "synth-new-7eebc043",
    "synth-new-70b6bfeb"
  ]
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: deleteSkill-leaves-persona-count-stale

</details>
