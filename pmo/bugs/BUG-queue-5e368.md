---
queue_id: BUG-queue-5e368
source: user_report
tool: deleteSkill
filed_at: 2026-05-16T20:39:39
status: open
---

# BUG-queue-5e368

**Tool**: `deleteSkill` | **Filed**: 2026-05-16 | **Status**: open

Part 2 of BUG-queue-280f1 (Part 1 was NOT a defect — server was down during teardown). deleteSkill h…

<details>
<summary>Full details</summary>

**Description**:
Part 2 of BUG-queue-280f1 (Part 1 was NOT a defect — server was down during teardown). deleteSkill handler declared async but ran three synchronous blocking ADB I/O calls (delete, delete_persona_builder_kb, shim_kb.reload) directly on the asyncio event loop. Under bastion/ADB reconnect, these freeze the event loop and uvicorn kills the unresponsive worker. Same d3ec0-class latent blocking that 309db5d fixed for authorSkill but was missed for deleteSkill. Fix: offload via asyncio.to_thread.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-322d946

</details>
