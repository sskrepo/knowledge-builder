---
queue_id: BUG-queue-a0a9a
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T20:02:09
status: open
---

# BUG-queue-a0a9a

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

For ask_parameterized/ephemeral skills, ContextBuilder tier-1 synthesis ran with NO passages (the pa…

<details>
<summary>Full details</summary>

**Description**:
For ask_parameterized/ephemeral skills, ContextBuilder tier-1 synthesis ran with NO passages (the page is fetched separately in the executor chain). Synthesizer emitted the '(no relevant context found)' sentinel even though the executor produced a complete correct artifact. Response lied: answer='(no relevant context found)' + citations=[] alongside a valid artifact_path. maybe_render_artifact never backfilled answer/citations. Traced from user-pasted response (req-7d351fb1-class confusion).

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: agent-rca-0995189

</details>
