---
queue_id: BUG-queue-20071
source: user_report
tool: authorSkill
filed_at: 2026-05-18T23:19:42
status: fixed
severity: HIGH
session: synth-tpm-fbaafad2
prior_bug_ref: BUG-queue-98ca0
fix_branch: fix/emcp-direct-canonical-via-runtime
---

# BUG-queue-20071 — DECISION-013: emcp_direct canonical_identity structural gap

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: fixed | **Severity**: HIGH

`emcp_direct.canonical_identity` passed `session=None` to `resolve_to_numeric_id`, so
`/display/SPACE/Title` URLs were STRUCTURALLY Unresolvable in laptop mode even though
`EmcpRuntime` (the working keychain-backed channel used by CONFIGURE_SOURCES) could resolve them.
The prior keychain-retry RCA (same session synth-tpm-fbaafad2) was a mis-diagnosis.

## Root cause

`canonical_identity()` called `resolve_to_numeric_id(reference, resource_type, session=None, base_url="")`.
With `session=None`, `shared.py` cannot do the display-by-title REST title lookup (Step 2 requires a
live session) and always returns `Unresolvable(TRANSIENT, retryable=True)` for `/display/SPACE/Title` URLs.

Meanwhile `self.runtime` (`EmcpRuntime`) was already working — CONFIGURE_SOURCES/sampler used
the same channel to fetch the same page successfully. The gap was that `canonical_identity` never
used it.

## Fix

Added `_resolve_via_emcp()` private helper in `emcp_direct.py`:
- Calls `self.runtime.call_tool_for_text("fetch", {"id": reference})` — same mechanism as `fetch()`.
- Reads numeric page `id` from `results.metadata.id` in the payload.
- Returns `CanonicalRef(connector_id="confluence", resource_type=..., canonical_id=<numeric>, display_hint=<title>)`.

`canonical_identity()` updated:
- Fast-path (numeric ref): delegates to `resolve_to_numeric_id(session=None)` → returns `CanonicalRef` immediately, NO eMCP round-trip.
- Display-by-title / other non-numeric non-invalid refs: calls `_resolve_via_emcp()`.
- `INVALID_REF` from shared: returned unchanged (not a recognized Confluence ref form).

Error mapping in `_resolve_via_emcp()`:
- `EmcpAuthError` → `Unresolvable(NO_ACCESS, retryable=False)`
- `EmcpError` / transient → `Unresolvable(TRANSIENT, retryable=True)`
- `not_found` in payload / empty id → `Unresolvable(NOT_FOUND, retryable=False)`

## Scope hygiene confirmed

- `shared.py` unchanged (stays pure; native/mcp still call it WITH a real session).
- `synthesize_workflow.py`, `conversation.py`, upper layers: byte-unchanged.
- No keychain-retry added (mis-diagnosis — out of scope).
- No prod/native transient-HTTP retry (valid but explicitly deferred).

## Tests

15 new unit tests in `framework/tests/unit/test_emcp_direct_canonical_via_runtime.py`.
All pass. Full unit suite: exactly 8 pre-existing failures, 0 new.

Note: live laptop eMCP behavior NOT unit-testable — user validates via live re-authoring run.
