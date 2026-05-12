---
title: BUG-002 — authorSkill continuation fails: Oracle 23ai auto-deserialises JSON CLOB to dict, json.loads(dict) raises TypeError
status: verified
created: 2026-05-12
verified: 2026-05-12
owner: qa
assigned-to: backend-dev
severity: blocker
related-story: V3-deployment-layer
component: framework/deploy/session/adb_store.py
discovered-by: user (live MCP session via /mcp tools/call)
fixed-in: 7cee283 (fix(session): handle Oracle 23ai auto-deserialising JSON CLOB to dict)
---

## Steps to reproduce

1. Start the KB server in laptop mode:
   ```bash
   bash framework/scripts/kbf-start.sh --skip-migrate
   ```

2. Start a new `authorSkill` session (this succeeds — only writes to DB):
   ```bash
   curl -sS -X POST http://localhost:8080/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
          "params":{"name":"authorSkill","arguments":{"input":"start"}}}'
   ```
   Note the returned `synth_id` (e.g. `synth-new-8205a2c3`).

3. Attempt to continue the session (this fails — loads from DB):
   ```bash
   curl -sS -X POST http://localhost:8080/mcp \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
          "params":{"name":"authorSkill","arguments":{
            "synthId":"synth-new-8205a2c3",
            "input":"tpm - create weekly exec review ppt"}}}'
   ```

## Expected

Session advances to the next state (e.g. `COLLECT_SOURCES`), returning the next
conversational turn with `state`, `message`, `progress`, `done=false`.

## Actual

```json
{
  "result": {
    "content": [{"type": "text", "text": "Invalid arguments: the JSON object must be str, bytes or bytearray, not dict"}],
    "isError": true
  }
}
```

The start call (step 2) always works because it only **writes** (`save()`) — no
`json.loads()` involved.  The continuation call (step 3) always fails because it
**reads** (`load()`) → `json.loads(row["session_data"])` where `row["session_data"]`
is already a Python dict.

## Root cause

`framework/stores/sql/kb_shim.sql` declares `session_data` as:

```sql
session_data CLOB  CHECK (session_data IS JSON),
```

Oracle 23ai's `python-oracledb` driver automatically deserialises CLOB columns
that carry an `IS JSON` check constraint into Python `dict` objects on fetch.
`AdbSessionStore.load()` then called `json.loads()` on the already-deserialised
dict:

```python
# Line 187 — before fix
session: dict = json.loads(row["session_data"])  # TypeError: not str, bytes, bytearray
```

The `TypeError` was caught by `_dispatch_tool_call`'s `except TypeError` clause
and surfaced as an `isError=true` content item — hiding the real stacktrace.

Same issue exists in `list_for_user()` line 237 for the `progress_json` column
(returned by `JSON_VALUE(session_data, '$.progress')`, also auto-deserialised).

## Why it wasn't caught by existing tests

`test_session_store.py` only exercises the **stub path** (`pool=None`).  The
`AdbSessionStore` pool-attached path (`pool != None`) has **zero unit-test
coverage**.  `save()` → `load()` round-trip was never tested against a real or
fake cursor, so the auto-deserialisation behaviour was invisible until a live run.

This is the same root cause as BUG-001's test gap (noted in dashboard).

## Fix

Two one-line guards in `adb_store.py` to handle both the old (string) and new
(dict, Oracle 23ai auto-parse) return shapes:

```python
# load() — line 186-188
raw = row["session_data"]
session: dict = raw if isinstance(raw, dict) else json.loads(raw)

# list_for_user() — line 237
"progress": (row["progress_json"] if isinstance(row["progress_json"], dict)
             else json.loads(row["progress_json"])) if row["progress_json"] else {},
```

The guard is forward-compatible: works on older `oracledb` drivers that return
strings, and on Oracle 23ai drivers that return dicts.

## Verification

After server restart on `7cee283`, the continuation call returns:

```json
{
  "result": {
    "content": [{"type": "text", "text": "{\"synth_id\": \"synth-new-8205a2c3\", \"state\": \"COLLECT_SOURCES\", ...}"}],
    "isError": false
  }
}
```

Session advanced from `IDENTIFY_PERSONA` → `COLLECT_SOURCES`.

## Test gap to close

Add a `TestAdbSessionStorePoolPath` suite (or integrate into existing
`test_session_store.py`) that covers the pool-attached path using a **thin
oracledb cursor fake** that:

1. Returns `session_data` as a Python `dict` (Oracle 23ai auto-parse simulation)
2. Returns `session_data` as a JSON `str` (older driver simulation)
3. Returns `progress_json` as a `dict` and as a `str`
4. Exercises `save → load` round-trip and asserts `load()` output matches `save()` input

Both return shapes must be exercised so the isinstance guard is always verified
in CI, not just in production.
