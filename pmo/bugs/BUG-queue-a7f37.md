---
queue_id: BUG-queue-a7f37
source: user_report
tool: authorSkill
filed_at: 2026-05-18T07:28:17
status: open
---

# BUG-queue-a7f37

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: open

DECISION-013 Phase A Bug: source_binding_mode stays ambiguous for fixed-source intent (display URL p…

<details>
<summary>Full details</summary>

**Description**:
DECISION-013 Phase A Bug: source_binding_mode stays ambiguous for fixed-source intent (display URL present). Root cause: _advance_to_capture_intent stores LLM output ambiguous without checking whether the intent text already contains a specific Confluence /display/ URL — a deterministic fixed-source signal. As a result, the source-binding CLARIFY question is raised unnecessarily and, if not explicitly answered with recognized text, the mode persists as ambiguous all the way to _synthesize_preview. _synthesize_preview had no ambiguous branch so neither derive_pinned_source nor derive_space_allow_list was called. The committed workflow_skill artifact carried source_binding=null and trigger.inputs=[{name:input,type:string}] (generic — not typed/pinned). The promoted tpm.faaas_kiwi_project_pptx skill in session synth-tpm-0de96bcc is in this hollow state. Severity: HIGH. Discovered by: architect. Fix: (1) _intent_contains_fixed_confluence_url helper detects display URL in intent text; (2) auto-resolve ambiguous to author_fixed at _advance_to_capture_intent when URL detected; (3) safety-net branch in _synthesize_preview resolves ambiguous to author_fixed and calls derive_pinned_source.

**Triggering input**:
_not recorded_

**User ID**: 218a5f843d6c3eee
**Request ID**: req-phase-a-bug-1

</details>
