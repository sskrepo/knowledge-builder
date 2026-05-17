---
queue_id: BUG-queue-e1463
source: user_report
tool: authorSkill
filed_at: 2026-05-16T21:30:35
status: open
---

# BUG-queue-e1463

**Tool**: `authorSkill` | **Filed**: 2026-05-16 | **Status**: open

Architect-RCA companion to reported synth-tpm-5b3e690f VALIDATE failure. synthesize_workflow_skill()…

<details>
<summary>Full details</summary>

**Description**:
Architect-RCA companion to reported synth-tpm-5b3e690f VALIDATE failure. synthesize_workflow_skill() never emitted a source_binding block. Every newly authored ask_parameterized skill committed with author_fixed defaults and immediately failed _validate_source_binding_contract at VALIDATE. The ADR-032 core use case (conversational authoring -> PROMOTE) was completely unreachable via the normal skill authoring flow.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-47ec90d

</details>
