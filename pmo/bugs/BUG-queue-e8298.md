---
queue_id: BUG-queue-e8298
source: user_report
tool: listSkills
filed_at: 2026-05-13T05:50:48
status: open
---

# BUG-queue-e8298

**Tool**: `listSkills` | **Filed**: 2026-05-13 | **Status**: open

After the ORA-00935 fix (BUG-queue-fcf05), listSkills now returns successfully but always with {"ski…

<details>
<summary>Full details</summary>

**Description**:
After the ORA-00935 fix (BUG-queue-fcf05), listSkills now returns successfully but always with {"skills": [], "total": 0}, regardless of filter. Same disconnect on getSkill: getSkill(persona='tpm', skillName='generate_a_weekly_exec_review_pptx_for_the_26ai_pr') returns "Skill not found" even though that exact skill was authored, committed, validated (ADR-017), ingested, eval'd, and promoted to production earlier in this same session (synthId synth-tpm-dd1d4865 — state DONE, step 15/15). Meanwhile, authorSkill IDENTIFY_PERSONA reports tpm: 6 skills, kbf_ops: 2, pm: 1 (total 9). So there are two distinct skill registries that aren't synchronized: (a) the on-disk persona-builder/workflow YAML files that authorSkill writes and IDENTIFY_PERSONA reads, and (b) the DB-backed skill store that listSkills/getSkill/deleteSkill query. authorSkill COMMIT and PROMOTE never populate the DB store, so newly authored/promoted skills are invisible to the new admin tools. Suggested fix: (1) make authorSkill PROMOTE write the skill metadata into the DB store as the final action; (2) backfill the DB store from existing on-disk YAMLs as a one-time migration so the 9 existing skills become visible. Without this, listSkills/getSkill/deleteSkill are functional but operate on an empty store — effectively non-functional in the real environment.

**Triggering input**:
```json
{
  "tool": "listSkills",
  "results": {
    "no_filter": {
      "skills": [],
      "total": 0
    },
    "persona_tpm": {
      "skills": [],
      "total": 0
    },
    "status_production": {
      "skills": [],
      "total": 0
    },
    "status_draft": {
      "skills": [],
      "total": 0
    }
  },
  "getSkill_followup": {
    "args": {
      "persona": "tpm",
      "skillName": "generate_a_weekly_exec_review_pptx_for_the_26ai_pr"
    },
    "response": "Skill 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr' not found."
  },
  "contradicting_evidence": {
    "authorSkill_IDENTIFY_PERSONA": {
      "tpm": 6,
      "kbf_ops": 2,
      "pm": 1
    },
    "promoted_this_session": "synth-tpm-dd1d4865 reached state DONE step 15/15 with skill tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr promoted to production"
  },
  "related_bug": "BUG-queue-fcf05 (the SQL fix that enabled this call to return without error)"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: listSkills-empty-after-fix

</details>
