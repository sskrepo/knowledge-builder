---
queue_id: BUG-queue-e685d
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T04:49:00
status: open
---

# BUG-queue-e685d

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

FOLLOW-UP / ESCALATION of BUG-queue-44364 with a concrete empty-output repro. Running workflow skill…

<details>
<summary>Full details</summary>

**Description**:
FOLLOW-UP / ESCALATION of BUG-queue-44364 with a concrete empty-output repro. Running workflow skill 26ai_fa_db_upgrade_to_26ai_pptx (persona tpm, output pptx, layout weekly_exec_review_v1) via askKnowledgeBase now SUCCEEDS at the call level (tier_used=1 workflow_skill, confidence 0.95, latency ~10.4s) and RETRIEVES the source correctly — the response citation contains the full Confluence page 20030556732 (project metadata: 'FA DB Upgrade from 19c to 26ai', FAAASPMO-1190, FAAASINGES-2526, full WBS/milestones/RAID, executive summary, assumptions). HOWEVER the rendered artifact /Users/sravansunkaranam/.kbf/outputs/26ai_fa_db_upgrade_to_26ai_pptx.pptx is an all-placeholder slide: title renders the raw skill slug '26ai_fa_db_upgrade_to_26ai_pptx' instead of project_name 'FA DB Upgrade to 26ai'; Scope='(scope not extracted)'; Key Milestones='• (none)'; ORM='• (not specified)'; Risk / Mitigation='• (none)'; footer 'Generated 2026-05-16'. NONE of the 32 schema fields bind despite the source being fetched in the same call. This confirms the field-extraction tier (OCI JSON_OBJECT _llm_extract) emits an empty/all-blank object while the retrieval tier works — same root cause as BUG-queue-44364 (eval failure on child pageId=20090907433 with all-empty JSON after sanitization). Impact: skill is unusable for real decks until the extraction fix lands. Additional defects observed: (1) output written to ~/.kbf/outputs/ not the configured ~/Downloads/; (2) filename ignores configured pattern '{YYYY-MM-DD} 26ai Weekly Exec Review.pptx'; (3) empty-string fields render inconsistent placeholders ('(none)' vs '(not specified)' vs '(scope not extracted)') instead of the designed ['TBD'] fallback. Request: prioritize the OCI JSON_OBJECT extraction fix, then re-run/eval skill 26ai_fa_db_upgrade_to_26ai_pptx (synth-tpm-9571f396) and confirm fields populate; also fix output path/filename and unify the empty-field fallback to 'TBD'.

**Triggering input**:
```json
{
  "persona": "tpm",
  "skill": "26ai_fa_db_upgrade_to_26ai_pptx",
  "question": "Generate the 26ai project status PPTX (FA DB Upgrade to 26ai, slide-15 layout) for week ending 2026-05-15 PT",
  "artifact_path": "/Users/sravansunkaranam/.kbf/outputs/26ai_fa_db_upgrade_to_26ai_pptx.pptx",
  "observed_slide_text": [
    "26ai_fa_db_upgrade_to_26ai_pptx",
    "Scope",
    "(scope not extracted)",
    "Key Milestones",
    "\u2022 (none)",
    "ORM",
    "\u2022 (not specified)",
    "Risk / Mitigation",
    "\u2022 (none)",
    "Generated 2026-05-16"
  ],
  "related_bug": "BUG-queue-44364",
  "tier_used": 1,
  "confidence": 0.95,
  "latency_ms": 10428
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: none-surfaced-tpm-26ai_fa_db_upgrade_to_26ai_pptx-emptyoutput

</details>
