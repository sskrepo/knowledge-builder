---
queue_id: BUG-queue-3d13e
source: user_report
tool: authorSkill
filed_at: 2026-05-12T23:56:22
status: open
---

# BUG-queue-3d13e

**Tool**: `authorSkill` | **Filed**: 2026-05-12 | **Status**: open

VALIDATE failure reproduces — user reported the prior bug (BUG-queue-51dd3) as fixed, but the same e…

<details>
<summary>Full details</summary>

**Description**:
VALIDATE failure reproduces — user reported the prior bug (BUG-queue-51dd3) as fixed, but the same error occurs on a fresh authorSkill session against localhost:8080. Either the fix is not deployed on this kbf instance, or the fix did not address the underlying issue. Repro: ran authorSkill end-to-end for persona 'tpm', skill name 'generate_a_weekly_exec_review_pptx_for_the_26ai_pr', refined all 14 field descriptions, configured Confluence source (space OCIFACP, labels 26ai/weekly-status/exec-review), trigger '3, pptx, 0 13 * * 1', committed 5 artifacts including framework/persona_builders/tpm.yaml.new_kb. Validate step failed with: "workflow references unknown KB: 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Ensure the persona builder YAML exists and the KB name matches." This is the same error reported in BUG-queue-51dd3. Note: the artifact path is identical to the prior session synth-tpm-443de2f6, so this commit overwrote the previous skill's artifacts — the .new_kb file is still not being merged into tpm.yaml before validation. Please confirm fix deployment status on this instance.

**Triggering input**:
```json
{
  "synthId": "synth-tpm-81f7dd71",
  "prior_bug": "BUG-queue-51dd3",
  "state_at_failure": "VALIDATE",
  "input_that_triggered": "yes, run full pipeline",
  "validation_errors": [
    "workflow references unknown KB: 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Ensure the persona builder YAML exists and the KB name matches."
  ],
  "committed_artifacts": [
    "framework/parsers/schemas/tpm/generate_a_weekly_exec_review_pptx_for_the_26ai_pr/v1.json",
    "framework/persona_builders/tpm.yaml.new_kb",
    "eval/gold_sets/tpm-generate_a_weekly_exec_review_pptx_for_the_26ai_pr-extraction.jsonl",
    "framework/workflow_skills/tpm/generate_a_weekly_exec_review_pptx_for_the_26ai_pr.yaml",
    "eval/gold_sets/tpm-generate_a_weekly_exec_review_pptx_for_the_26ai_pr-workflow.jsonl"
  ],
  "user_assertion": "user stated the previously filed bug was already fixed before retry"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: synth-tpm-81f7dd71

</details>
