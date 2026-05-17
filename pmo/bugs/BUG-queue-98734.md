---
queue_id: BUG-queue-98734
source: user_report
tool: reviewSkillSession
filed_at: 2026-05-16T18:35:49
status: open
---

# BUG-queue-98734

**Tool**: `reviewSkillSession` | **Filed**: 2026-05-16 | **Status**: open

_check_kb_references_resolve iterated top-level dict KEYS of the persona_builder_delta artifact (nam…

<details>
<summary>Full details</summary>

**Description**:
_check_kb_references_resolve iterated top-level dict KEYS of the persona_builder_delta artifact (name/kind/extraction_schema/...) instead of the knowledge_bases[].name VALUE. The known-KB set was always wrong, causing every correctly-authored skill to file a spurious 'major: hallucinated KB reference' finding (false-positive). Found investigating reported synth-tpm-fe0f9e9f (review score 9.1 with 3 false major findings).

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: agent-rca-0f0214f

</details>
