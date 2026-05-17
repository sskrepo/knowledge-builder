---
queue_id: BUG-queue-938f0
source: user_report
tool: authorSkill
filed_at: 2026-05-13T06:22:10
status: open
---

# BUG-queue-938f0

**Tool**: `authorSkill` | **Filed**: 2026-05-13 | **Status**: open

REVIEW_SCHEMA auto-rich-description path appears to have regressed. Repro: start a fresh authorSkill…

<details>
<summary>Full details</summary>

**Description**:
REVIEW_SCHEMA auto-rich-description path appears to have regressed. Repro: start a fresh authorSkill session, at ANALYZE_ARTIFACT (step 2) provide a comma-separated list of 15 standard exec-review field names (week_id, project_name, overall_rag, executive_summary, key_accomplishments, upcoming_milestones, schedule_health, scope_health, resource_health, top_risks, blockers, dependencies, exec_asks, metrics_snapshot, workstream_status), confirm at REVIEW_FIELDS with 'ok'. Earlier today multiple sessions on this same kbf instance with the identical input produced rich, production-grade descriptions for each field (e.g., "Capture the executive summary narrative intended for leadership: 2–5 dense, exec-readable sentences..."). Affected sessions that worked: synth-tpm-87d3a9aa, synth-tpm-89134ad3, synth-tpm-dd1d4865. Now (synth-tpm-24609640) the same input returns stub descriptions ("Field X — refine description", "Identifier — week_id", "Multi-valued list — top_risks") with a banner warning: "⚠️ 15 field(s) were added after the artifact analysis — their descriptions were synthesised from context and may need more refinement than the rest." This warning fires even though no artifact was uploaded and the field list was the ONLY input at step 2. Likely cause: a recent change inverted the auto-description gating logic — either the "added after artifact analysis" flag is now set whenever ANY field-list input arrives at step 2 (not just genuine after-the-fact adds), or the auto-rich-description code path was removed/disabled. Impact: forces every skill author to either (a) refine 15+ descriptions individually (15+ extra round trips) or (b) accept stubs and ship a skill with low extraction quality. Recommend: (1) verify gating logic — auto-rich-descriptions should fire when fields arrive directly at step 2 input; (2) only show the "added after artifact analysis" warning when an artifact was actually analyzed and produced different fields than the final set; (3) add an integration test that confirms rich descriptions are produced for the standard 15-field input.

**Triggering input**:
```json
{
  "current_session": "synth-tpm-24609640",
  "input_at_step_2": "week_id, project_name, overall_rag, executive_summary, key_accomplishments, upcoming_milestones, schedule_health, scope_health, resource_health, top_risks, blockers, dependencies, exec_asks, metrics_snapshot, workstream_status",
  "no_artifact_uploaded": true,
  "result": "all 15 descriptions are stubs with the misleading 'added after artifact analysis' warning",
  "prior_working_sessions_same_input": [
    "synth-tpm-87d3a9aa",
    "synth-tpm-89134ad3",
    "synth-tpm-dd1d4865"
  ],
  "prior_working_example": "week_id was previously described as 'Extract the reporting week identifier exactly as shown (e.g., \\'FY26 W12\\', \\'2026-05-13 week\\', or \\'Week of May 13, 2026\\'). Preserve the original format and do not infer or reformat dates.' \u2014 now it comes back as 'Identifier \u2014 week_id'"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: REVIEW_SCHEMA-stubs-regression

</details>
