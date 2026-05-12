---
id: BUG-008
queue_id: BUG-queue-1a86a
request_id: req-9f15f8a2
synth_id: synth-tpm-ec2fad6d
filed: 2026-05-12
status: fixed
fixed_in: pending commit
---

# BUG-008 — ORA-03146: CLOB bind overflow at CONFIGURE_TRIGGERS

## Symptom

`authorSkill` crashes at `CONFIGURE_TRIGGERS` (or any state reached after
artifact analysis) with:

```
ORA-03146: invalid buffer length for TTC field
```

The session cannot be saved to ADB. The user loses the entire session
progress from that turn onward.

## Root cause

`AdbSessionStore.save()` builds a params dict and passes it to
`cur.execute(_SQL_UPSERT, params)` with `session_data` and `intent` as plain
Python strings. The `oracledb` thin driver defaults to VARCHAR2 binding for
Python strings. VARCHAR2 has an effective network buffer limit of ~32 KB in
the TTC protocol. Once artifact analysis content is embedded in `session_data`
(e.g. extracted PPT text, field descriptions, conversation history across 8+
turns) the serialised JSON easily exceeds 32 KB, causing `ORA-03146`.

Both `intent` and `session_data` are defined as `CLOB` in the DDL
(`kb_shim.author_skill_sessions`). The driver must be told explicitly to use
CLOB binding so it routes through the LOB protocol (which supports up to 4 GB).

## Fix

Added `cursor.setinputsizes()` before `cur.execute()` in `save()`:

```python
if _ORACLEDB_AVAILABLE:
    cur.setinputsizes(
        session_data=oracledb.DB_TYPE_CLOB,
        intent=oracledb.DB_TYPE_CLOB,
    )
cur.execute(_SQL_UPSERT, params)
```

`oracledb` imported at module level with a safe try/except guard (the import
already exists for the pool; the guard is for test environments without the
package).

## Files changed

- `framework/deploy/session/adb_store.py` — `save()` method

## Trigger condition

Reliably reproducible after the `CONFIGURE_TRIGGERS` turn in any session
where artifact analysis was performed (session JSON > 32 KB). Any long session
without artifacts could also hit this eventually (e.g. deep multi-turn
GATHER_INFO with large field descriptions).

## Reported by

User (anon-dev) via `reportBug` MCP tool, 2026-05-12T19:06:27Z.
Linked error: `req-9f15f8a2` / `synth-tpm-ec2fad6d`.
