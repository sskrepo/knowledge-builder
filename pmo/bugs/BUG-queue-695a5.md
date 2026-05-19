---
queue_id: BUG-queue-695a5
source: user_report
tool: authorSkill
filed_at: 2026-05-19T01:18:25
status: open
---

# BUG-queue-695a5

**Tool**: `authorSkill` | **Filed**: 2026-05-19 | **Status**: open

conversation.py _run_eval constructed WorkflowExecutor without confluence_adapter (3rd/separate exec…

<details>
<summary>Full details</summary>

**Description**:
conversation.py _run_eval constructed WorkflowExecutor without confluence_adapter (3rd/separate executor construction site missed by the mcp_server lifespan fix BUG-queue-081dc) -> EVAL Path-A deterministically hard-fails for ask_parameterized skills with ConfluencePageNotInKBError. Fix: build+pass adapter mirroring conversation.py:4528/6401. References BUG-queue-081dc, session synth-tpm-8cb2adf7 (and earlier synth-tpm-afcacfc5). fix_commit=1647d4b merged 059e673.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: fix-session-run-eval-3rd-site

</details>
