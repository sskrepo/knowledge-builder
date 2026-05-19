---
queue_id: BUG-queue-98ca0
source: user_report
tool: authorSkill
filed_at: 2026-05-18T22:45:58
status: open
---

# BUG-queue-98ca0

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: open

During authorSkill commit for synth-tpm-fbaafad2, user confirmed ok, commit. Tool failed with transi…

<details>
<summary>Full details</summary>

**Description**:
During authorSkill commit for synth-tpm-fbaafad2, user confirmed ok, commit. Tool failed with transient Confluence canonicalization failure for display-by-title URL https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project, saying live Confluence session is required and to retry when credentials are available. This blocked artifact assembly after successful preview extraction.

**Triggering input**:
```json
{
  "synthId": "synth-tpm-fbaafad2",
  "input": "ok, commit"
}
```

**User ID**: anon-dev
**Request ID**: req-bb707fa9

</details>
