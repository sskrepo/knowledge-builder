---
queue_id: BUG-queue-44116
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-17T06:25:46
status: open
---

# BUG-queue-44116

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-17 | **Status**: open

ADR-033: promoted skill silently unreachable — tier-4 no_answer. shim_workflows built card bodies fr…

<details>
<summary>Full details</summary>

**Description**:
ADR-033: promoted skill silently unreachable — tier-4 no_answer. shim_workflows built card bodies from disk YAML (could be absent/stale/missing source_binding) while gating promotion on ADB. Promoted ask_parameterized skill tpm.project_tracking_weekly_stakeholder_meeting_email (session synth-tpm-5b3e690f, JSON-RPC id 34) returned tier_used=4 no_answer because disk byproduct was absent. Fix: ADR-033 — resolve card body from ADB read_artifact, not disk. Mirrors ADR-015 Option B (shim_kb).

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: None

</details>
