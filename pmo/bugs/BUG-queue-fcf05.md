---
queue_id: BUG-queue-fcf05
source: user_report
tool: listSkills
filed_at: 2026-05-13T05:44:01
status: open
---

# BUG-queue-fcf05

**Tool**: `listSkills` | **Filed**: 2026-05-13 | **Status**: open

listSkills returns ORA-00935 ("group function is nested too deeply") regardless of arguments. Repro:…

<details>
<summary>Full details</summary>

**Description**:
listSkills returns ORA-00935 ("group function is nested too deeply") regardless of arguments. Repro: three separate calls — (1) listSkills with no args, (2) listSkills with persona='tpm', (3) listSkills with status='production'. All three returned the same isError response: "Failed to list skills: ORA-00935: group function is nested too deeply". Root cause is server-side: the SQL query backing this tool contains a nested aggregate function (e.g., COUNT/SUM inside another aggregate, or a non-aggregate inside an outer aggregate without a GROUP BY). Oracle reference: https://docs.oracle.com/error-help/db/ora-00935/. Likely fix: pre-aggregate in a subquery or CTE, then aggregate the result — never nest aggregates directly. Since listSkills was just shipped as part of the new listSkills/getSkill/deleteSkill trio, this is presumably a launch-day defect. Workaround until fixed: callers can infer counts from authorSkill IDENTIFY_PERSONA (skill_count per persona), but cannot get skill names without server-side filesystem access. Affected: any caller that wants to enumerate, audit, or pick a skill by name. Suggested priority: major (the tool is 100% non-functional).

**Triggering input**:
```json
{
  "tool": "listSkills",
  "calls_attempted": [
    {
      "args": {}
    },
    {
      "args": {
        "persona": "tpm"
      }
    },
    {
      "args": {
        "status": "production"
      }
    }
  ],
  "error": "ORA-00935: group function is nested too deeply",
  "oracle_doc": "https://docs.oracle.com/error-help/db/ora-00935/",
  "suggested_fix": "pre-aggregate in a subquery/CTE then aggregate the result"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: listSkills-ORA-00935

</details>
