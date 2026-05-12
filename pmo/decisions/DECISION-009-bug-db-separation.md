# DECISION-009: Dedicated Bug DB — Separate Connection Config

**Status**: DECIDED  
**Date**: 2026-05-12  
**Decided by**: User  
**Informed by**: DECISION-008 (ADB-only bug storage), bug bash design session

---

## Problem

Bug records come from two distinct origins that will eventually need to be in separate databases:

1. **Dev-created bugs** — filed by Claude agents (Claude Code, dev CI) running on developer machines in a dev environment. Currently unrouted — no write path exists for these yet.
2. **User-reported bugs** — filed via `reportBug` MCP tool, received through the prod API → `AdbErrorStore.record_user_bug()` → `KB_SHIM.KBF_BUG_REPORTS`
3. **Critic-found bugs** — filed by `reviewSkillSession` → `_persist_audit_run()` → `KB_SHIM.KBF_AUDIT_RUNS`

Sources 2 and 3 write to the main ADB (the same database as sessions, skills, artifacts). In production these will be in separate infrastructure. But today, ALL bugs — from all sources — need to land in **one place** with enough context to triage and validate them.

---

## Decision

Introduce a **`bug_db` config section** in every environment config file. It specifies a separate Oracle user (and optionally separate ADB instance) for bug storage. Any field not set in `bug_db` is inherited from the main `adb` section.

**For now (laptop + staging + current prod)**: same ADB instance, different Oracle user (`KBF_BUGS`). One bug DB, same ADB, just isolated credentials.

**Future (prod scale-out)**: override `dsn`, `wallet_path`, `wallet_password_secret` in `bug_db` to point at a completely separate ADB dedicated to bugs. No code change needed — only a config change.

### Config structure

```yaml
adb:
  dsn: ...
  wallet_path: ...
  admin_user: ADMIN
  admin_password_secret: vault://kb/adb-admin
  # ... (main app DB — sessions, skills, artifacts, vectors)

bug_db:
  # Overrides for bug storage connection.
  # Unspecified fields (dsn, wallet_path, bastion, wallet_password_secret)
  # are INHERITED from the adb section above.
  #
  # For now: same ADB, different user.
  # For prod scale-out: add dsn/wallet_path here to point at a dedicated ADB.
  user: KBF_BUGS
  password_secret: vault://kb/kbf-bugs-password
```

### Wire-up

- `_init_bug_pool(repo_root, kbf_env)` — new function; merges `adb` base + `bug_db` overrides; returns an `oracledb` pool connected as `KBF_BUGS`
- `app.state.bug_pool` — exposed on app state (alongside `app.state.adb_pool`)
- `AdbErrorStore(bug_pool, store_root)` — uses bug pool (not main pool)
- `_persist_audit_run(bug_pool, ...)` — uses bug pool
- `kb-cli export-bugs` — reads from `bug_db` config
- `kb-cli setup-bug-user` — creates `KBF_BUGS` Oracle user (admin pool) + runs GRANTs; run once per environment

### Schema / GRANTs

Tables stay in `KB_SHIM` for now. `KBF_BUGS` user gets:
```sql
GRANT INSERT, SELECT ON KB_SHIM.KBF_BUG_REPORTS TO KBF_BUGS;
GRANT INSERT, SELECT ON KB_SHIM.KBF_AUDIT_RUNS   TO KBF_BUGS;
```

When the future prod bug DB is a separate ADB, it will have its own `KBF_BUG_REPORTS` / `KBF_AUDIT_RUNS` tables created by the same migration suite run against that ADB.

---

## Consequences

- Dev-side bugs (Claude Code on dev machines) can now be routed to the same bug DB — they connect using the `bug_db` credentials in the dev/laptop config
- Prod API bugs (`reportBug`, `reviewSkillSession`) go through `bug_pool` → same bug DB
- `export-bugs` always reads from the bug DB regardless of environment
- One SQL change when prod separates: update `dsn`/`wallet_path` in `prod.yaml`'s `bug_db` section — no code changes
