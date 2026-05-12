---
title: BUG-003 — authorSkill continuation fails: Oracle plain TIMESTAMP returns naive datetime; comparison with datetime.now(tz=utc) raises TypeError
status: verified
created: 2026-05-12
verified: 2026-05-12
owner: qa
assigned-to: backend-dev
severity: blocker
related-story: V3-deployment-layer
component: framework/deploy/session/adb_store.py
discovered-by: user (live MCP session via /mcp tools/call)
fixed-in: (pending commit — see fix section)
---

## Steps to reproduce

1. Start the KB server in laptop mode:
   ```bash
   bash framework/scripts/kbf-start.sh --skip-migrate
   ```

2. Start a new `authorSkill` session (this succeeds):
   ```bash
   curl -sS -X POST http://localhost:8080/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
          "params":{"name":"authorSkill","arguments":{"input":"start"}}}'
   ```
   Note the returned `synth_id` (e.g. `synth-new-08219169`).

3. Attempt to continue the session (fails on expiry check):
   ```bash
   curl -sS -X POST http://localhost:8080/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
          "params":{"name":"authorSkill","arguments":{
            "synthId":"synth-new-08219169",
            "input":"tpm - create weekly exec review ppt"}}}'
   ```

## Expected

Session advances to the next state (e.g. `COLLECT_SOURCES`), returning the next
conversational turn with `state`, `message`, `progress`, `done=false`.

## Actual

```json
{
  "result": {
    "content": [{"type": "text", "text": "Invalid arguments: can't compare offset-naive and offset-aware datetimes"}],
    "isError": true
  }
}
```

The start call (step 2) always works because it only **writes** (`save()`) — no
datetime comparison involved. The continuation call (step 3) always fails because
it **reads** (`load()`) and then compares the Oracle-returned `expires_at` datetime
against `datetime.now(tz=timezone.utc)`.

## Root cause

`framework/stores/sql/kb_shim.sql` declares `expires_at` as:

```sql
expires_at  TIMESTAMP,
```

Oracle plain `TIMESTAMP` (not `TIMESTAMP WITH TIME ZONE`) **strips timezone on write**
and returns a timezone-naive `datetime` object on read via the `python-oracledb` driver.

`AdbSessionStore.load()` then compared the naive datetime against
`datetime.now(tz=timezone.utc)` (which is UTC-aware):

```python
# Lines 220-226 — before fix
expires_at_val = row["expires_at"]
if expires_at_val and session.get("status") == "in_progress":
    if isinstance(expires_at_val, str):
        expires_dt = datetime.fromisoformat(expires_at_val)
    else:
        # oracledb may return a datetime object
        expires_dt = expires_at_val   # ← timezone-naive datetime from Oracle
    if expires_dt < datetime.now(tz=timezone.utc):  # ← TypeError here
```

Python raises `TypeError: can't compare offset-naive and offset-aware datetimes`
because `expires_dt.tzinfo` is `None` while `datetime.now(tz=timezone.utc)` is
UTC-aware.

The `TypeError` propagates up through `_dispatch_tool_call`'s `except Exception`
handler and surfaces as an `isError=true` content item.

This bug was invisible until BUG-002 was fixed: before BUG-002, the continuation
path always raised `json.loads(dict)` first, so the expiry check was never reached.

## Why it wasn't caught by existing tests

Same root cause as BUG-001 and BUG-002: `test_session_store.py` only exercises the
**stub path** (`pool=None`). The `AdbSessionStore` pool-attached path has zero unit
test coverage. A fake Oracle cursor would have revealed this immediately.

## Fix

Replace the brittle `isinstance` chain with a call to the `_as_utc()` static method
(added as part of this fix). `_as_utc()` normalises both ISO strings and
timezone-naive datetimes to UTC-aware, so the comparison always succeeds:

```python
# load() — after fix
expires_at_val = row["expires_at"]
if expires_at_val and session.get("status") == "in_progress":
    expires_dt = self._as_utc(expires_at_val)   # always UTC-aware
    if expires_dt < datetime.now(tz=timezone.utc):
```

`_as_utc()` handles all three shapes that oracledb may return:
- `None` → returns `None`
- ISO string ending in `Z` → normalise to `+00:00`, parse, return aware
- ISO string with offset → parse, return aware
- timezone-naive `datetime` → attach `timezone.utc`, return aware
- timezone-aware `datetime` → return unchanged

## Verification

After server restart with this fix, the continuation call returns:

```json
{
  "result": {
    "content": [{"type": "text", "text": "{\"synth_id\": \"synth-new-08219169\", \"state\": \"COLLECT_SOURCES\", ...}"}],
    "isError": false
  }
}
```

Session advanced from `IDENTIFY_PERSONA` → `COLLECT_SOURCES`.

## Test gap to close

Add a `TestAdbSessionStorePoolPath` suite (alongside BUG-001/002 gaps) that:

1. Creates a thin `oracledb` cursor fake returning `expires_at` as:
   - A timezone-naive `datetime` (Oracle plain TIMESTAMP simulation)
   - A timezone-aware `datetime` (UTC)
   - An ISO string with `Z` suffix
   - An ISO string with `+00:00` offset
2. Verifies that none of these shapes raise `TypeError` in `load()`
3. Verifies that a session whose `expires_at` is in the past returns `None`
4. Verifies that a session whose `expires_at` is in the future is returned normally
