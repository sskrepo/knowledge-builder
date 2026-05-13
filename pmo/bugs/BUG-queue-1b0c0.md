---
queue_id: BUG-queue-1b0c0
source: user_report
tool: authorSkill
filed_at: 2026-05-13T00:09:04
status: open
---

# BUG-queue-1b0c0

**Tool**: `authorSkill` | **Filed**: 2026-05-13 | **Status**: open

Third reproduction of the same VALIDATE failure on this kbf instance (localhost:8080) across three i…

<details>
<summary>Full details</summary>

**Description**:
Third reproduction of the same VALIDATE failure on this kbf instance (localhost:8080) across three independent authorSkill sessions. Prior bug references: BUG-queue-51dd3 (first report), BUG-queue-3d13e (regression follow-up). User has now twice asserted the fix was deployed, yet the error reproduces on every brand-new session. Either: (a) the fix is not actually deployed to this instance, (b) the fix only handles a different code path than the one this flow triggers, or (c) there is a deployment/cache step missed after the fix was merged. Repro is fully deterministic: run authorSkill end-to-end for persona 'tpm', skill name 'generate_a_weekly_exec_review_pptx_for_the_26ai_pr', any reasonable field set, any source config — committing always writes framework/persona_builders/tpm.yaml.new_kb (with the .new_kb suffix) and VALIDATE then complains "workflow references unknown KB: 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Ensure the persona builder YAML exists and the KB name matches." Suggested investigation: (1) confirm the deployed version on localhost:8080 includes the merge-or-resolve-against-.new_kb logic; check `git log` on the kbf service repo for the fix commit and verify it's part of the running build. (2) Verify the kbf service was restarted after the fix landed. (3) If the fix targets a different state machine path, the COMMIT-then-VALIDATE flow specifically still produces this error and needs its own fix. Three sessions affected so far: synth-tpm-443de2f6, synth-tpm-81f7dd71, synth-tpm-51f37a45 — all blocked at VALIDATE step 11/15, unable to complete the ingest/eval/promote pipeline. Priority: should probably be bumped above 'minor' given it blocks every new tpm skill end-to-end.

**Triggering input**:
```json
{
  "synthId": "synth-tpm-51f37a45",
  "prior_bugs": [
    "BUG-queue-51dd3",
    "BUG-queue-3d13e"
  ],
  "instance": "localhost:8080",
  "state_at_failure": "VALIDATE",
  "input_that_triggered": "yes, run full pipeline",
  "validation_errors": [
    "workflow references unknown KB: 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Ensure the persona builder YAML exists and the KB name matches."
  ],
  "user_assertion": "user stated twice that the prior bug had been fixed; error reproduces on every new session",
  "affected_sessions": [
    "synth-tpm-443de2f6",
    "synth-tpm-81f7dd71",
    "synth-tpm-51f37a45"
  ],
  "shared_artifact_path": "framework/persona_builders/tpm.yaml.new_kb (the .new_kb suffix is the root cause \u2014 never merged into tpm.yaml before VALIDATE)",
  "suggested_priority_bump": "minor -> major (blocks the full pipeline for every new tpm skill)"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: synth-tpm-51f37a45

</details>
