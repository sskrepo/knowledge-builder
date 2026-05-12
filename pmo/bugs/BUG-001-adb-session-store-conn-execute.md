---
title: BUG-001 — AdbSessionStore calls non-existent Connection.execute()/fetchone()/fetchall()
status: verified
created: 2026-05-11
verified: 2026-05-11
owner: qa
assigned-to: backend-dev
severity: blocker
related-story: V3-deployment-layer
component: framework/deploy/session/adb_store.py
discovered-by: claude (kind-noether-c0925c session)
fixed-in: d36d46b (feat(phase2+3): close all 9 implementation gaps found in architect audit)
---

## Steps to reproduce

1. Start the KB server in laptop mode with a healthy ADB pool:
   ```bash
   bash framework/scripts/kbf-start.sh --migrate
   ```
   Server log shows: `laptop mode: ADB pool ready — authorSkill sessions backed by ADB`.
2. POST a session-creating request:
   ```bash
   curl -sS -X POST \
     -H "Authorization: Bearer dev-only-token-replace-me" \
     -H "Content-Type: application/json" \
     -d '{"persona":"tpm","intentDescription":"Author a skill"}' \
     http://localhost:8080/api/v1/kb/authorSkill
   ```

## Expected

201/200 response with `{ synthId, lastTurn, ... }` envelope — a new session persisted to `kb_shim.author_skill_sessions` and the state machine advanced.

## Actual

HTTP 500. Server log:
```
File ".../framework/deploy/routes/author_skill.py", line 71, in author_skill_start_or_continue
    result = _start_or_continue_session(
File ".../framework/deploy/routes/author_skill.py", line 246, in _start_or_continue_session
    session_store.save(session_dict, user_id=user_id)
File ".../framework/deploy/session/adb_store.py", line 135, in save
    conn.execute(_SQL_UPSERT, params)
AttributeError: 'Connection' object has no attribute 'execute'
```

Same shape will fire on `load()`, `list_for_user()`, `abandon()`, `expire_stale()`. The whole `authorSkill` flow is blocked when ADB-backed.

## Evidence

- Server log: `/Users/sravansunkaranam/.kbf/kbf-server.log`
- Existing test suite: [framework/tests/test_session_store.py](framework/tests/test_session_store.py) only exercises **stub mode** (`pool=None`); there is **no test** covering the pool-attached code path. That is why this regression wasn't caught.

## Root cause

`framework/deploy/session/adb_store.py` calls `conn.execute(sql, params)` and `conn.fetchone(sql, params)` directly on the `oracledb.Connection` object. The `python-oracledb` API doesn't expose `execute`/`fetchone`/`fetchall` on `Connection` — those are **cursor** methods.

Affected call sites (all in `adb_store.py`):

| Line | Method | Bad call |
|---|---|---|
| 135 | `save` | `conn.execute(_SQL_UPSERT, params)` |
| 149 | `load` | `conn.fetchone(_SQL_LOAD, {...})` |
| 188 | `list_for_user` | `conn.fetchall(_SQL_LIST, {...})` |
| 212 | `abandon` | `conn.execute(_SQL_ABANDON, {...})` |
| 226 | `expire_stale` | `conn.execute(_SQL_EXPIRE_STALE)` then `cursor.rowcount` on the conn |

Additionally, `load()` and `list_for_user()` access result rows as `row["column_name"]` — `oracledb` returns tuples by default; a `rowfactory` must be set to enable dict-style access.

## Fix

Switch every call site to the proper cursor pattern, and install a dict rowfactory after `cursor.execute()` for SELECT statements:

```python
with self._pool.acquire() as conn:
    with conn.cursor() as cur:
        cur.execute(_SQL_LOAD, {"synth_id": synth_id, "user_id": user_id})
        cols = [d[0].lower() for d in cur.description]
        cur.rowfactory = lambda *vals: dict(zip(cols, vals))
        row = cur.fetchone()
```

For DML (`save`, `abandon`, `expire_stale`): no rowfactory needed, but `cur.rowcount` replaces `cursor.rowcount` on the returned object.

## Resolution

Patched [framework/deploy/session/adb_store.py](framework/deploy/session/adb_store.py) in commit `d36d46b`:

1. **Cursor pattern.** All five DML/SELECT sites now use `with conn.cursor() as cur: cur.execute(...)`.
2. **Dict rowfactory.** Added `_install_dict_rowfactory(cur)` helper installed after `cur.execute()` for SELECTs so `row["column_name"]` access works.
3. **TIMESTAMP binding.** Added `_to_dt()` helper that coerces ISO-8601 strings (and `datetime` passthrough) into `datetime` objects for binding — Oracle `oracledb` binds `datetime` directly to TIMESTAMP columns, but rejects ISO strings with ORA-01843 because it tries to parse them via NLS_DATE_FORMAT.
4. **Python 3.9 `Z` suffix.** `_to_dt()` rewrites trailing `Z` to `+00:00` because `datetime.fromisoformat()` on 3.9 doesn't accept `Z` (added in 3.11). Upstream conversation code emits `Z`.

### Verification

Server restarted from this worktree, then:

```
POST /api/v1/kb/authorSkill {"persona":"tpm","intentDescription":"Author a skill"}
→ 200 OK
  synthId: synth-tpm-c6df8cd3
  state:   ANALYZE_ARTIFACT
  progress: {step: 2, total: 15}
```

Session was created, persisted to ADB, and the state machine advanced to step 2 (`ANALYZE_ARTIFACT`).

## Test gap to close

Add at least one **integration test** that exercises `AdbSessionStore.save/load/list_for_user/abandon/expire_stale` against either:

- a real `oracledb` thin connection to a local Oracle Free container, OR
- a thin fake that mimics the cursor API surface (`conn.cursor()` → `cur.execute()`, `cur.fetchone()`, `cur.description`, `cur.rowcount`).

The current stub-only tests would have flagged `Connection.execute` if they exercised the live path with a minimal fake.
