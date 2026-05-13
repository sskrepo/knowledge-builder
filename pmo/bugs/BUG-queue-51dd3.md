---
queue_id: BUG-queue-51dd3
source: user_report
tool: authorSkill
filed_at: 2026-05-12T23:20:48
status: open
---

# BUG-queue-51dd3

**Tool**: `authorSkill` | **Filed**: 2026-05-12 | **Status**: open

authorSkill VALIDATE step fails because the workflow references a KB that does not yet exist in the …

<details>
<summary>Full details</summary>

**Description**:
authorSkill VALIDATE step fails because the workflow references a KB that does not yet exist in the live persona file. Repro: ran the authorSkill state machine end-to-end for persona 'tpm' to create skill 'generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Commit step (step 8) succeeded and wrote 5 artifacts including framework/persona_builders/tpm.yaml.new_kb (a candidate file with a .new_kb suffix). Validate step (step 11) then failed with: "workflow references unknown KB: 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Ensure the persona builder YAML exists and the KB name matches." The author flow appears to commit the new KB as a .new_kb candidate but does not merge it into framework/persona_builders/tpm.yaml before validation runs, so the lookup misses every time. Expected: VALIDATE should either (a) auto-merge .new_kb candidates into the persona YAML before running the KB-name check, or (b) treat .new_kb files as a valid source for KB resolution during validation. Suggested fix: in the validation step, resolve KB names against both tpm.yaml and tpm.yaml.new_kb (or finalize the merge as part of COMMIT). Severity: blocks the full pipeline (validate/ingest/eval/promote) for every newly authored skill.

**Triggering input**:
```json
{
  "synthId": "synth-tpm-443de2f6",
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
  ]
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: synth-tpm-443de2f6

</details>
