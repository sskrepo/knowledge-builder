---
title: BUG-006 — PROMOTE/DONE save raises ORA-02290: status='committed' violates CHK_ASS_STATUS constraint
status: verified
created: 2026-05-12
verified: 2026-05-12
owner: qa
assigned-to: backend-dev
severity: blocker
related-story: V3-deployment-layer
component: framework/deploy/routes/author_skill.py
discovered-by: user (live MCP session via /mcp tools/call)
fixed-in: (this commit)
---

## Steps to reproduce

Complete a full `authorSkill` session through VALIDATE → INGEST → EVAL and then
respond to the promote prompt with either "yes, promote" or "no, keep as draft".

## Expected

Session is saved with `status='completed'` and the turn is returned with `done=true`.

## Actual

```
ORA-02290: check constraint (KB_SHIM.CHK_ASS_STATUS) violated
```

Both "yes, promote" and "no, keep as draft" fail identically because both paths
set `done=True` on the returned `ConversationTurn`.

## Root cause

`framework/deploy/routes/author_skill.py` line 238:

```python
# Before fix
status = "committed" if turn.done else "in_progress"
```

The DB constraint `CHK_ASS_STATUS` (defined in `framework/stores/sql/kb_shim.sql`)
allows only: `'in_progress'`, `'completed'`, `'abandoned'`, `'expired'`.

`"committed"` is not in that list. Whenever `turn.done=True` (PROMOTE/DONE state),
`adb_store.save()` binds `status='committed'` to the UPSERT — Oracle raises ORA-02290.

The value `"committed"` was chosen to describe a session that reached the commit step,
but the intended lifecycle value is `"completed"` (the session finished successfully).

## Fix

```python
# After fix
status = "completed" if turn.done else "in_progress"
```

## Verification

After server restart, the PROMOTE/DONE transition saves cleanly and returns
`done=true` to the MCP client.

## Test gap to close

`test_routes_author_skill.py` should assert that when a session with `done=True`
is returned from `_start_or_continue_session()`, the `session_store.save()` call
receives a dict with `status='completed'` (not `'committed'` or any other value
not in the DB constraint).
