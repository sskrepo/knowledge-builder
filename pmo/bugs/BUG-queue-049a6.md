---
queue_id: BUG-queue-049a6
source: user_report
tool: authorSkill
filed_at: 2026-05-16T13:28:36
status: open
---

# BUG-queue-049a6

**Tool**: `authorSkill` | **Filed**: 2026-05-16 | **Status**: open

Architect-RCA companion to user report BUG-queue-44364. Arbitrary app-layer content caps silently tr…

<details>
<summary>Full details</summary>

**Description**:
Architect-RCA companion to user report BUG-queue-44364. Arbitrary app-layer content caps silently truncated extraction output: max_tokens ceilings (e.g. 4096) on extraction/design prompts caused 20–32 field RODS/26ai schemas to render as blank placeholders. Additionally: arbitrary maxLength in synthesized schema defaults and char-level source-text caps clipped LLM input before extraction. All were silent truncations — ADB/CLOB storage is unbounded; the caps were vestigial app-layer constraints. Fix cluster: bf6dfab (max_tokens raised), f3baf51 (maxLength removed), e6b0a65 (source-text caps raised), ec84c0d (LLM-JSON parse guards).

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-bf6dfab

</details>
