---
title: ADR-024 — Dedicated Bug DB Connection
status: accepted
created: 2026-05-12
owner: architect
deciders: user, tpm
supersedes: ~
tags: [arch, data, ops]
---

## Context

DECISION-009 (2026-05-12) introduced a `bug_db` config section pointing at a separate
Oracle user (`KBF_BUGS`) for all bug-related writes. The decision separates bug storage
credentials from the main ADB credentials while keeping the same physical ADB for now.

Three write paths currently point at `KB_SHIM.KBF_BUG_REPORTS` and
`KB_SHIM.KBF_AUDIT_RUNS` using the main `adb_pool` (ADMIN-connected):

1. `AdbErrorStore.record_user_bug()` — `reportBug` MCP tool
2. `AdbErrorStore.record_error()` — `KB_SHIM.KBF_ERROR_LOG` (stays on main pool; not moved)
3. `_persist_audit_run()` in `mcp_tools.py` — `reviewSkillSession` → `KBF_AUDIT_RUNS`

The goal is to re-route paths 1 and 3 through a dedicated `bug_pool` (KBF_BUGS user),
expose that pool as `app.state.bug_pool`, and make the config change the only thing
required when the bug DB later moves to a separate ADB.

Note: `record_error()` writes to `KB_SHIM.KBF_ERROR_LOG`, not a bug table. That table
is used by `cmd_watch_bugs` for server-side error diagnostics, not bug triage. It stays
on the main pool. Only `KBF_BUG_REPORTS` and `KBF_AUDIT_RUNS` move to `bug_pool`.

---

## Decision

### 1. `_init_bug_pool` — config inheritance contract

A new function `_init_bug_pool(repo_root, kbf_env)` in `mcp_server.py` builds the
bug DB connection pool by merging `bug_db` overrides on top of `adb` base values.

**Overridable fields** — any of these may appear in `bug_db`; if absent, the
corresponding value from `adb` is used verbatim:

| Field in YAML | Source of truth when absent from `bug_db` |
|---|---|
| `dsn` / `service_name` | `adb.dsn` then `adb.service_name` |
| `wallet_path` | `adb.wallet_path` |
| `wallet_password_secret` | `adb.wallet_password_secret` |
| `user` | — must be present in `bug_db`; no sensible default |
| `password_secret` | — must be present in `bug_db`; no sensible default |

**Fields that are NEVER taken from `bug_db`** (always inherited from `adb` or top-level):

| Field | Reason |
|---|---|
| `bastion` (all sub-fields) | Bug DB uses the same tunnel as the main ADB (same host, same port). Separate bastion config is not supported. |
| `admin_user` / `admin_password_secret` | `bug_pool` never needs admin access; admin pool is only used by `setup-bug-user` and `migrate`. |
| `deployment_mode` | Comes from the top-level YAML key; env-level, not connection-level. |

**Merge algorithm** (pseudocode the dev must implement):

```python
def _build_bug_pool_config(raw: dict) -> dict:
    adb = raw.get("adb", {})
    bug = raw.get("bug_db", {})

    # Connectionstring: bug_db overrides adb
    service_name = (
        bug.get("dsn") or bug.get("service_name")
        or adb.get("dsn") or adb.get("service_name", "")
    )
    wallet_path = str(
        Path(bug.get("wallet_path") or adb.get("wallet_path", "")).expanduser()
    )
    wallet_password = _resolve_secret(
        bug.get("wallet_password_secret") or adb.get("wallet_password_secret", "")
    )

    # User + password: must come from bug_db
    user = bug.get("user", "")
    password = _resolve_secret(bug.get("password_secret", ""))

    if not user or not password:
        raise RuntimeError(
            "bug_db config must specify user and password_secret "
            "(inheritance from adb is not permitted for credentials)"
        )

    pool_config = {
        "deployment_mode": raw.get("deployment_mode", kbf_env),
        "adb": {
            "service_name":    service_name,
            "wallet_path":     wallet_path,
            "user":            user,
            "password":        password,
            "wallet_password": wallet_password,
        },
    }

    # Bastion config: always inherited from top-level; never from bug_db
    bastion_raw = raw.get("bastion", {})
    if bastion_raw:
        pool_config["bastion"] = { ... }  # same field mapping as _init_adb_pool

    return pool_config
```

`_init_bug_pool` raises `RuntimeError` on missing config or pool init failure. The
caller decides whether to treat this as fatal (see section 4 below).

---

### 2. Oracle user creation approach

Embedding a password in a SQL migration file is insecure. `KBF_BUGS` user creation
uses a CLI command that resolves the password at runtime.

**`kb-cli setup-bug-user --env <env>`** (new standalone command):

1. Loads the env config YAML.
2. Builds the **admin pool** using the `adb` section (same as `cmd_migrate`).
3. Resolves `bug_db.password_secret` via `_resolve_secret_cli()` to get the plaintext password.
4. Executes:
   ```sql
   CREATE USER KBF_BUGS IDENTIFIED BY "<resolved_password>"
   ```
   Suppresses ORA-01920 (user already exists) for idempotency.
5. Closes the admin pool.
6. Prints instructions to run `kb-cli migrate --schema kb_shim --env <env>` next,
   which applies migration-007 (GRANTs).

**Migration-007** (`framework/db/migrations/007_bug_user_grants.sql`) contains only
GRANT statements — no CREATE USER. Run after `setup-bug-user`. Idempotent (Oracle
ignores re-granting an existing privilege without error).

```sql
-- Migration 007: GRANT privileges on bug tables to KBF_BUGS user.
-- Prerequisites: kb-cli setup-bug-user must have run first.
-- Idempotent: re-granting an existing privilege is a no-op in Oracle.

GRANT INSERT, SELECT ON KB_SHIM.KBF_BUG_REPORTS TO KBF_BUGS;
GRANT INSERT, SELECT ON KB_SHIM.KBF_AUDIT_RUNS   TO KBF_BUGS;
```

**Why not embed in `migrate`?** The `migrate` command runs as ADMIN and executes DDL
SQL files. It is not designed to accept a password argument or build a second pool
mid-migration. Keeping `setup-bug-user` standalone avoids overloading `migrate` with
privilege management semantics, and makes the two-step process explicit:

```
Step 1:  kb-cli setup-bug-user --env laptop
Step 2:  kb-cli migrate --schema kb_shim --env laptop
         (picks up 007_bug_user_grants.sql automatically)
```

---

### 3. SQL table reference strategy

`error_store.py` uses qualified names `KB_SHIM.KBF_BUG_REPORTS` and
`KB_SHIM.KBF_AUDIT_RUNS`. When KBF_BUGS connects and issues:

```sql
INSERT INTO KB_SHIM.KBF_BUG_REPORTS ...
INSERT INTO KB_SHIM.KBF_AUDIT_RUNS   ...
```

Oracle resolves `KB_SHIM.` as an explicit schema qualifier. KBF_BUGS needs only
`INSERT` privilege on those tables — the schema-qualified INSERT works as long as
the GRANT in migration-007 has been applied. No schema name changes are needed.

When the bug DB later moves to a separate ADB, those tables will be created by the
same migration suite (`kb-cli migrate --schema kb_shim`) run against the new ADB.
At that point the tables will exist in whatever user owns the schema on that ADB
(likely ADMIN or a dedicated schema owner). The SQL in `error_store.py` and
`mcp_tools.py` does not need to change — only the `bug_db.dsn` and
`bug_db.wallet_path` in the config.

---

### 4. `mcp_server.py` startup wiring

**Startup order:**

1. `adb_pool` initialised first (existing behaviour; raises on failure — ADB-always policy).
2. `bug_pool` initialised second, immediately after.
3. If `bug_pool` init fails: **log a warning and set `app.state.bug_pool = None`**.
   `AdbErrorStore` and `_persist_audit_run` already handle `pool=None` by falling
   back to JSONL-only writes (see `AdbErrorStore.__init__` pool=None check and the
   `if self._pool is None: return` guard in both write methods; `_persist_audit_run`
   already wraps its DB write in `except Exception` with a warning log).

**Rationale for non-fatal bug_pool failure:** Bug writes are a diagnostic aid, not
a transaction path. A failed bug_pool init must not prevent the server from serving
`askKnowledgeBase` and `authorSkill` requests. The fallback — JSONL files under
`~/.kbf/store/` — is sufficient for `kb-cli watch-bugs` and `kb-cli export-bugs`
until the pool is restored. Bugs written to JSONL while bug_pool is down are visible
immediately via `watch-bugs`; they are never backfilled to ADB (acceptable loss).

**Exception:** If `bug_db` section is entirely absent from the config YAML,
`_init_bug_pool` should return `None` without raising (not a misconfiguration —
`bug_db` is optional until the user runs `setup-bug-user`). The warning should say:
`"bug_db section not found in config — bug writes will fall back to JSONL only"`.

**Wiring diff in `mcp_server.py`:**

```python
# After adb_pool is ready:
bug_pool = None
try:
    bug_pool = _init_bug_pool(REPO_ROOT, kbf_env)
    log.info("bug DB pool ready (env=%s)", kbf_env)
except RuntimeError as exc:
    log.warning("bug DB pool init failed — bug writes will use JSONL fallback: %s", exc)

app.state.bug_pool = bug_pool
app.state.error_store = AdbErrorStore(bug_pool, store_root)
```

**`_persist_audit_run` change in `mcp_tools.py`:**

The call site at line 624–636 currently reads `adb_pool`. It must be updated to read `bug_pool`:

```python
# Before (line 624):
pool = getattr(app.state, "adb_pool", None)

# After:
pool = getattr(app.state, "bug_pool", None)
```

The function signature `_persist_audit_run(pool, ...)` is unchanged; only the
call site changes.

---

### 5. `export-bugs` CLI update

`cmd_export_bugs` in `kb_cli.py` currently builds its pool from `cfg.get("adb", {})`.
It must be updated to use the `bug_db` merge strategy (same inheritance rules as
`_init_bug_pool`): resolve `bug_db` overrides on top of `adb` defaults.

This is the **only CLI change needed** for `export-bugs`. The SQL queries, output
format, and file writing logic are unaffected. The `--env` flag continues to select
which config file to load.

---

### 6. Config YAML shape — all three environments

#### laptop.yaml — add after the `adb:` block

```yaml
# ---- Bug DB (DECISION-009) — separate Oracle user, same ADB ----------
# Credentials for KBF_BUGS Oracle user.
# dsn, wallet_path, wallet_password_secret, and bastion are inherited from adb above.
# Run once per env: kb-cli setup-bug-user --env laptop
#                   kb-cli migrate --schema kb_shim --env laptop
bug_db:
  user: KBF_BUGS
  password_secret: env://KBF_BUGS_PASSWORD   # export KBF_BUGS_PASSWORD=<pw>
```

#### staging.yaml — add after the `adb:` block

```yaml
# ---- Bug DB (DECISION-009) — separate Oracle user, same ADB ----------
# dsn, wallet_path, wallet_password_secret, and bastion inherited from adb above.
bug_db:
  user: KBF_BUGS
  password_secret: vault://kb/kbf-bugs-password-staging
```

#### prod.yaml — add after the `adb:` block

```yaml
# ---- Bug DB (DECISION-009) — separate Oracle user, same ADB ----------
# For now: same ADB, different user.
# Future scale-out: add dsn + wallet_path here pointing at the dedicated bug ADB.
#   dsn: <bug_adb_service_name>
#   wallet_path: /opt/kb/wallet/bug-prod/
#   wallet_password_secret: vault://kb/bug-adb-wallet-password-prod
bug_db:
  user: KBF_BUGS
  password_secret: vault://kb/kbf-bugs-password-prod
```

**Future prod scale-out** — no code change required. Add `dsn`, `wallet_path`, and
`wallet_password_secret` to `bug_db` in `prod.yaml`. `_init_bug_pool` picks them up
on next server restart.

---

## Consequences

- **Positive:** Bug credentials are isolated from main ADB ADMIN credentials. Least-privilege principle applied. Future DB separation is a config-only change.
- **Positive:** `export-bugs` reads from the correct pool by default; no need to pass special flags.
- **Positive:** `record_error()` (server-side error log) stays on main pool and is unaffected by bug_pool outages.
- **Negative:** Two pool init calls on startup (minor overhead; both are lazy connection pools, so actual connections are not opened until first acquire).
- **Negative:** `setup-bug-user` is a manual per-environment step before the first use. The server starts and degrades gracefully if it has not been run, but bugs go to JSONL only until it is run.
- **Reversibility:** High. Reverting to main-pool bug writes is a two-line change in `mcp_server.py` and no config changes.

## Alternatives considered

- **Option A: Keep all writes on main pool, add KBF_BUGS as a schema-level read user only** — Rejected. Does not achieve credential isolation for writes. Future DB separation still requires code changes.
- **Option B: Move KBF_BUG_REPORTS/KBF_AUDIT_RUNS to a dedicated schema (KBF_BUGS schema, not KB_SHIM)** — Rejected for "for now" phase. Schema-qualified SQL in error_store.py and mcp_tools.py would need to change. DECISION-009 explicitly keeps tables in KB_SHIM to minimise changes.
- **Option C: Fatal error on bug_pool init failure** — Rejected. Bug writes are a diagnostic path; they must not bring down the API server. The JSONL fallback is sufficient for short outages.

## References

- DECISION-009: `/Users/sravansunkaranam/github/Knowledgebase/pmo/decisions/DECISION-009-bug-db-separation.md`
- ADR-023: kbf_ops persona and reviewSkillSession (introduces `_persist_audit_run`)
- ADR-019: Bastion auto-reconnect (bastion config structure)
- `framework/deploy/error_store.py` — `AdbErrorStore` pool=None fallback behaviour
- `framework/deploy/mcp_tools.py` — `_persist_audit_run` (lines 643–679)
- `framework/deploy/mcp_server.py` — `_init_adb_pool` (lines 372–459) — template for `_init_bug_pool`
- `framework/cli/kb_cli.py` — `cmd_export_bugs` (line 656), `cmd_migrate` (line 200)
- `framework/db/migrations/006_audit_runs_and_schema_constraint.sql` — migration style to follow for 007
