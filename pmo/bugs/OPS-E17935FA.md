---
queue_id: OPS-E17935FA
source: user_report
tool: reviewSkillSession
filed_at: 2026-05-13T01:13:44
status: fixed
fixed_at: 2026-05-13
fix_commit: pending
---

# OPS-E17935FA

**Tool**: `authorSkill` (CONFIGURE_SOURCES) | **Filed**: 2026-05-13 | **Status**: fixed

No conversation history beyond the intent; the server did not elicit essential parameters (bug tracker source, scope, definition of 'open', output format). This blocks correct automation design.

<details>
<summary>Full details</summary>

**Description**:
No conversation history beyond the intent; the server did not elicit essential parameters (bug tracker source, scope, definition of 'open', output format). This blocks correct automation design.

Root causes identified (2026-05-13):
1. **No minimum-source validation** — CONFIGURE_SOURCES allowed `done` with zero sources, silently injecting a `REPLACE_ME` Confluence placeholder. A skill with no real source produces empty extractions and fails at EVAL.
2. **No persona-aware source hints** — `kbf_ops` sources are ADB tables, but the prompt only showed Confluence/Jira options. User had no guidance that ADB is the right source type.
3. **No ADB source parsing** — `_parse_source_descriptor` had no `adb` branch, so even if a user typed "adb table KB_SHIM.X" it would fall to `{"kind": "unknown"}`.

**Fix** (`framework/skill_builder/conversation.py`):
- Added `_PERSONA_SOURCE_HINTS` — per-persona examples and option chips for `kbf_ops`, `tpm`, `pm`; generic fallback for others.
- `_advance_to_configure_sources()` now uses persona-specific hints.
- `_handle_configure_sources_response("done")` with empty sources now blocks with a clear error + persona-specific guidance (instead of silently adding `REPLACE_ME`).
- `_parse_source_descriptor()` now handles `adb table SCHEMA.TABLE` → `{"kind": "adb", "table": "..."}`.
- 11 regression tests added.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: ops-rev-aa0c6882b710-3162e5

</details>
