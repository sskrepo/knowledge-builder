---
queue_id: BUG-queue-885bc
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T18:44:14
status: open
---

# BUG-queue-885bc

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

D1 Priority-1 (ask-time ingestion) was structurally dead for all MCP consumers. _make_ask_handler in…

<details>
<summary>Full details</summary>

**Description**:
D1 Priority-1 (ask-time ingestion) was structurally dead for all MCP consumers. _make_ask_handler in mcp_tools.py called maybe_render_artifact with no body= kwarg; the D1 Priority-1 branch was unreachable. MCP callers had no structured page_id parameter and relied exclusively on Priority-2 question-string regex. Surfaced while investigating req-7d351fb1 — that session itself was NOT a live defect (killed stub + stale pre-fix artifact); the structural dead-branch was the real finding. Fix: ask_handler gains page_id param; body= threaded into maybe_render_artifact.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-fd18916

</details>
