---
queue_id: BUG-queue-30b34
source: user_report
tool: authorSkill
filed_at: 2026-05-13T00:30:43
status: open
---

# BUG-queue-30b34

**Tool**: `authorSkill` | **Filed**: 2026-05-13 | **Status**: open

Fourth reproduction of the same VALIDATE failure on localhost:8080. Prior bug references: BUG-queue-…

<details>
<summary>Full details</summary>

**Description**:
Fourth reproduction of the same VALIDATE failure on localhost:8080. Prior bug references: BUG-queue-51dd3 (first), BUG-queue-3d13e (regression follow-up), BUG-queue-1b0c0 (third repro with suggested priority bump). Notably, between BUG-queue-1b0c0 and this report, an unrelated improvement DID ship: the REVIEW_SCHEMA step now auto-generates high-quality, production-grade field descriptions instead of stub placeholders ('Field X — refine description'). That's a clear win and confirms the kbf service was redeployed at some point. However, the VALIDATE step still rejects committed skills with the identical error message: "workflow references unknown KB: 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Ensure the persona builder YAML exists and the KB name matches." Same root cause as before: framework/persona_builders/tpm.yaml.new_kb (with .new_kb suffix) is committed but never merged into framework/persona_builders/tpm.yaml before validation runs, so the KB-name lookup always misses. This is now demonstrably blocking every new tpm skill on this instance across 4 independent sessions. Strongly recommend: (1) bump priority to major/blocker, (2) confirm whether the merge step is supposed to run during COMMIT or VALIDATE and where it's actually failing, (3) add a deployment smoke test that creates a trivial skill end-to-end and verifies promote succeeds. Affected sessions across all 4 reports: synth-tpm-443de2f6, synth-tpm-81f7dd71, synth-tpm-51f37a45, synth-tpm-87d3a9aa.

**Triggering input**:
```json
{
  "synthId": "synth-tpm-87d3a9aa",
  "prior_bugs": [
    "BUG-queue-51dd3",
    "BUG-queue-3d13e",
    "BUG-queue-1b0c0"
  ],
  "instance": "localhost:8080",
  "state_at_failure": "VALIDATE",
  "input_that_triggered": "yes, run full pipeline",
  "validation_errors": [
    "workflow references unknown KB: 'tpm.generate_a_weekly_exec_review_pptx_for_the_26ai_pr'. Ensure the persona builder YAML exists and the KB name matches."
  ],
  "evidence_of_partial_deploy": "REVIEW_SCHEMA auto-generated descriptions are now production-grade (previously stubs) \u2014 confirms a redeploy happened between reports but did not include the VALIDATE/merge fix",
  "shared_artifact_path": "framework/persona_builders/tpm.yaml.new_kb is committed but never merged into tpm.yaml",
  "all_affected_sessions": [
    "synth-tpm-443de2f6",
    "synth-tpm-81f7dd71",
    "synth-tpm-51f37a45",
    "synth-tpm-87d3a9aa"
  ],
  "recommendation": "priority bump to major/blocker; add E2E deployment smoke test"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: synth-tpm-87d3a9aa

</details>
