---
queue_id: BUG-queue-d1bda
source: user_report
tool: authorSkill
filed_at: 2026-05-17T22:42:48
status: open
---

# BUG-queue-d1bda

**Tool**: `authorSkill` | **Filed**: 2026-05-17 | **Status**: open

ADR-028 S4 regression: persona-fragment injection check (test_persona_fragments_injected_in_run_desi…

<details>
<summary>Full details</summary>

**Description**:
ADR-028 S4 regression: persona-fragment injection check (test_persona_fragments_injected_in_run_design_skill) failed after 78307d1 added _generate_design_skill_card() as a second LLM call after the design_skill call. call_args pointed at the card-writer prompt (no persona overlay) instead of the design_skill prompt. Introduced by 78307d1, caught by trust-but-verify failure-count check (9 failures vs 8 pre-existing baseline). Fixed by reordering: card generation now precedes the design_skill LLM call.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: req-82956d0a

</details>
