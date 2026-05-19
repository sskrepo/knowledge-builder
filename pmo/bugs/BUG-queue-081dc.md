---
queue_id: BUG-queue-081dc
source: user_report
tool: authorSkill
filed_at: 2026-05-19T00:24:58
status: open
---

# BUG-queue-081dc

**Tool**: `authorSkill` | **Filed**: 2026-05-19 | **Status**: open

mcp_server lifespan gated executor Confluence-adapter construction on _any_promoted_skill_requires_e…

<details>
<summary>Full details</summary>

**Description**:
mcp_server lifespan gated executor Confluence-adapter construction on _any_promoted_skill_requires_ephemeral (promoted-only check) — first in-authoring ask_parameterized skill deterministically fails EVAL Path-A (chicken-and-egg): can't promote without EVAL passing; EVAL Path-A can't pass without the adapter; adapter only built if something is already promoted. Fix: build adapter unconditionally. build_confluence_adapter returns None safely when unconfigured. Reference session synth-tpm-afcacfc5. Fix commit: b1c48d5.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: bug-filing-081dc

</details>
