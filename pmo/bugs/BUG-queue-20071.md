---
queue_id: BUG-queue-20071
source: user_report
tool: authorSkill
filed_at: 2026-05-18T23:19:42
status: open
---

# BUG-queue-20071

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: open

emcp_direct.canonical_identity passed session=None to resolve_to_numeric_id, so /display/SPACE/Title…

<details>
<summary>Full details</summary>

**Description**:
emcp_direct.canonical_identity passed session=None to resolve_to_numeric_id, so /display/SPACE/Title URLs were STRUCTURALLY Unresolvable in laptop mode even though EmcpRuntime (the working keychain-backed channel used by CONFIGURE_SOURCES) could resolve them. Fix: added _resolve_via_emcp() helper in emcp_direct.py that uses self.runtime.call_tool_for_text('fetch', ...) to obtain the numeric page ID. Fix commit: 292f1a0.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: bug-remediate-20071

</details>
