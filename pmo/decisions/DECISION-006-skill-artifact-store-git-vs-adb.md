---
id: DECISION-006
title: Durable storage for committed skill artifacts — ADB vs git-sync
status: decided
created: 2026-05-12
decided: 2026-05-12
owner: architect
tags: [adb, storage, skill-builder, git, durability]
related: [ADR-010, ADR-015, ADR-021, DECISION-005]
---

# DECISION-006 — Durable storage for committed skill artifacts — ADB vs git-sync

## Context

When `authorSkill` reaches COMMITTED, `_write_artifacts()` in
`framework/skill_builder/conversation.py` writes four files to the **local
filesystem** under `REPO_ROOT`:

```
framework/workflow_skills/{persona}/{skill_name}.yaml
framework/persona_builders/{persona}.yaml.new_kb
eval/gold_sets/{persona}-{skill_name}-extraction.jsonl
eval/gold_sets/{persona}-{skill_name}-workflow.jsonl
```

This is plain `Path.write_text()` — no DB writes, no git operations.  On a
developer's laptop `REPO_ROOT` is their working tree; they commit manually.
On an OCI VM the files live on the VM's boot volume.  If that volume is lost
or corrupted all committed skills are gone, while sessions (ADB), errors, and
user bugs (the subject of this decision's companion ADR-022) are also at risk
but addressed separately.

Additionally, `_run_validate()` reads `wf_path` from the filesystem
**immediately after** `_write_artifacts()` wrote it.  On a fresh VM restart
(before any `git pull` or manual restore) the validate step would crash even
if the skill was previously committed.

### What the team has already decided

- Oracle ADB is the durable store for **sessions** (`AdbSessionStore`).
- Oracle ADB will also store **errors**, **user bugs**, and **cost telemetry**
  (ADR-022, separate migration).
- The question here is narrowly: **what do we do with the four skill artifact
  files?**

---

## What git actually provides for skill artifacts

Before choosing, it is worth being honest about which git benefits are real
vs. theoretical for this specific data type.

| Git benefit | Real for skill artifacts? | Notes |
|---|---|---|
| Human-readable diffs | Marginal | YAML diffs are readable but nobody reviews them today. PR review of skill commits is a future workflow, not a v1 requirement. |
| Commit history / blame | Useful | "Who authored skill X, when, from which session?" — but ADB can answer this too via `authored_by` + `authored_at` columns with no extra work. |
| PR review before promotion | Potentially valuable | Useful if skills need sign-off before running in production. Not a v1 requirement. |
| CI eval harness trigger | Valuable | CI could run the eval gold set on every new skill commit. However, a scheduled poll or promotion webhook satisfies the same requirement without git-sync. |
| Portability / DR export | Low urgency | `kb-cli export-skills` (SQL SELECT + yaml.dump) provides the same thing on demand; no ongoing job needed. |
| Off-site backup independent of ADB | Real | If ADB and its backups somehow fail simultaneously, git is a second copy. This is a genuine tail-risk argument. |

**Summary:** The PR-review and CI-trigger benefits are real but are v2
concerns.  The off-site backup argument is the strongest case for git-sync,
but it is weak compared to enabling ADB's own Autonomous Backup (7-day PITR
is on by default).

---

## Options

### Option A — ADB only, no git-sync

`KBF_SKILL_ARTIFACTS` table in ADB stores the four artifact blobs as CLOBs.
`_write_artifacts()` executes an ADB MERGE (upsert) in the same DB connection
already open for session writes.  `_run_validate()` loads the workflow YAML
CLOB from ADB (or writes a temp file and passes it to the validator).

The git repo's `framework/workflow_skills/` and `eval/gold_sets/` are
populated **only** when a developer runs `kb-cli export-skills` (a read-only
SQL → filesystem dump command). The server never touches git.

**Pros:**
- ADB pool already open — no new infrastructure
- Single ADB transaction for `_write_artifacts()` + session status update
  eliminates the partial-write race (if server crashes between files 2 and 3,
  the session is not corrupt — the transaction rolled back)
- Server needs no git credentials, no deploy key, no network path to GitHub
- `VALIDATE` reads from ADB — works on a fresh VM restart, no git pull needed
- SQL queries across skills: `SELECT * FROM KBF_SKILL_ARTIFACTS WHERE persona='tpm'`
- ADB Autonomous Backup provides 7-day PITR at no extra cost

**Cons:**
- CLOBs in ADB are less human-browsable than files in a repo
- `kb-cli export-skills` must be run manually for DR drills or audit requests
- CI cannot trigger on a git push event (needs a promotion webhook or scheduled job instead)

---

### Option B — ADB primary + async git-sync

ADB is source of truth. A scheduled job (`kb-cli git-sync`, e.g. every
15 min or triggered via promotion webhook) reads rows with
`sync_status = 'pending'` from `KBF_SKILL_ARTIFACTS`, writes the files,
runs `git add / commit / push` to a `skills-export` branch, and marks the
rows `synced`.  Human or CI reviews and merges.

**Pros:**
- All Option A durability benefits, plus
- Skills appear in git history → PR review workflow possible
- CI push-event trigger possible
- Off-site backup via git remote

**Cons:**
- New moving part: the sync job must be deployed, monitored, and alerted on failure
- Sync job holds git write credentials (deploy key) — a credential that expires or rotates breaks the loop silently
- Merge conflicts on the export branch are possible (e.g. if a developer committed a hand-edited skill directly)
- Ongoing reconciliation complexity: what if ADB and git disagree?
- The 15-min lag means git is eventually consistent — not immediately useful for PR review

---

### Option C — Git primary, ADB as lightweight index

Server writes to git synchronously (`git add / commit / push`) as part of
`_handle_commit()`.  ADB stores a lightweight index (skill name, persona,
path, synth_id, authored_at) but not the full content.

**Pros:**
- Skills immediately in git history
- Full PR review and CI trigger possible from day one

**Cons:**
- Server must hold a git deploy key with push access
- `git push` failure blocks the user's COMMITTED turn — UX breaks on network
  hiccups, GitHub rate limits, or key rotation
- Two concurrent `authorSkill` sessions for the same persona cause a push
  race (commit A and commit B both branch from HEAD; second push fails)
- CLAUDE.md §Rules explicitly states git writes are owned by human agents
  (including this framework's Claude agents), not the server process.
  A server `git push` violates that principle.

**Rejected.** The synchronous push failure UX and the CLAUDE.md principle
conflict alone disqualify this option.

---

## Recommendation

**Option A — ADB only, no git-sync.**

### Rationale

1. **No new infrastructure.** The ADB pool is open. Adding `KBF_SKILL_ARTIFACTS`
   is one DDL + one method rewrite. Option B needs a deployed, monitored,
   credentialed sync job for benefits that are v2 concerns.

2. **Eliminates partial-write corruption.** Currently if the server crashes
   between writing file 3 and file 4, `committed_paths` in the session shows
   partial progress and `_run_validate()` reads a corrupt state.  A single ADB
   MERGE is atomic.

3. **Server does not own git writes.** This is both a CLAUDE.md principle and
   a practical operations concern. The human agent (or the developer) owns the
   `git push` that makes skills "official." The server's job is to author and
   validate; promotion to the canonical repo is a deliberate human action.

4. **ADB Autonomous Backup is on by default.** Oracle ADB in the team's OCI
   tenancy has 7-day PITR enabled automatically. The "off-site backup via git"
   argument for Option B is addressed by ADB's own backup, which does not
   require a sync job.

5. **CI eval is satisfiable without git-sync.** The v1 requirement is that
   the eval harness runs.  A post-PROMOTE webhook to a CI system, or a
   scheduled `kb-cli run-eval`, satisfies this without git-sync.

6. **`kb-cli export-skills` covers portability.** If the team ever wants to
   review skills in the repo, a single command exports all promoted skills to
   the working tree for commit.  This is a deliberate, auditable action —
   better than a background sync whose last run time you have to check.

### When to revisit (→ ADR-023: git-sync for skill promotion)

**Decided 2026-05-12: Option B deferred.** The PR-review benefit only materialises when author ≠ approver (separation of duties). Today one user does the full authoring flow and PROMOTE is already the review gate. The KB framework owner has no domain knowledge to review skill YAML. ADB `authored_by + authored_at + synth_id` covers the audit-trail requirement without a sync job.

File **ADR-023 — git-sync for skill promotion** when **any of** the following become true:
- The team introduces a role-separated approval model (author ≠ approver)
- CI requires a git push-event trigger that cannot be satisfied by a post-PROMOTE webhook
- ADB is being decommissioned

Until then, `kb-cli export-skills` provides on-demand git export for any manual PR or audit request.

---

## Proposed schema

### `KBF_SKILL_ARTIFACTS` — committed skill artifact blobs

```sql
CREATE TABLE KB_SHIM.KBF_SKILL_ARTIFACTS (
    artifact_id    VARCHAR2(128)  NOT NULL,   -- "{persona}.{skill_name}.{artifact_type}"
    persona        VARCHAR2(64)   NOT NULL,
    skill_name     VARCHAR2(128)  NOT NULL,
    artifact_type  VARCHAR2(64)   NOT NULL,
    -- workflow_skill | persona_builder_delta | eval_extraction | eval_workflow
    rel_path       VARCHAR2(512)  NOT NULL,   -- original REPO_ROOT-relative path
    content        CLOB           NOT NULL,
    format         VARCHAR2(16)   DEFAULT 'yaml',   -- yaml | json | jsonl
    status         VARCHAR2(32)   DEFAULT 'committed',
    -- committed | promoted | archived
    synth_id       VARCHAR2(64),
    authored_by    VARCHAR2(128),
    authored_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP,
    promoted_at    TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_skill_artifacts PRIMARY KEY (artifact_id),
    CONSTRAINT chk_artifact_type CHECK (
        artifact_type IN ('workflow_skill','persona_builder_delta',
                          'eval_extraction','eval_workflow')
    ),
    CONSTRAINT chk_artifact_status CHECK (
        status IN ('committed','promoted','archived')
    )
);

CREATE INDEX idx_skill_art_persona ON KB_SHIM.KBF_SKILL_ARTIFACTS (persona, skill_name);
CREATE INDEX idx_skill_art_synth   ON KB_SHIM.KBF_SKILL_ARTIFACTS (synth_id);
```

---

## Companion: ADR-022 — ADB for errors, user bugs, cost telemetry

The following three tables replace the three local JSONL files.  They follow
the same pattern as `AdbSessionStore` and should be implemented in the same
migration as `KBF_SKILL_ARTIFACTS`.

### `KBF_ERROR_LOG`

```sql
CREATE TABLE KB_SHIM.KBF_ERROR_LOG (
    request_id    VARCHAR2(32)   NOT NULL,
    recorded_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    tool          VARCHAR2(64),
    synth_id      VARCHAR2(64),
    user_id       VARCHAR2(128),
    error_type    VARCHAR2(128),
    message       CLOB,
    traceback     CLOB,
    input_snapshot CLOB IS JSON,
    CONSTRAINT pk_error_log PRIMARY KEY (request_id)
);
CREATE INDEX idx_error_synth  ON KB_SHIM.KBF_ERROR_LOG (synth_id);
CREATE INDEX idx_error_time   ON KB_SHIM.KBF_ERROR_LOG (recorded_at);
```

### `KBF_BUG_REPORTS`

```sql
CREATE TABLE KB_SHIM.KBF_BUG_REPORTS (
    queue_id      VARCHAR2(32)   NOT NULL,
    request_id    VARCHAR2(32),
    reported_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    tool          VARCHAR2(64),
    description   CLOB,
    input_snapshot CLOB IS JSON,
    user_id       VARCHAR2(128),
    status        VARCHAR2(32)   DEFAULT 'open',
    -- open | investigating | resolved | duplicate
    resolved_at   TIMESTAMP WITH TIME ZONE,
    resolution    CLOB,
    CONSTRAINT pk_bug_reports PRIMARY KEY (queue_id),
    CONSTRAINT chk_bug_status CHECK (
        status IN ('open','investigating','resolved','duplicate')
    )
);
CREATE INDEX idx_bug_request ON KB_SHIM.KBF_BUG_REPORTS (request_id);
CREATE INDEX idx_bug_time    ON KB_SHIM.KBF_BUG_REPORTS (reported_at);
```

### `KBF_COST_LOG`

```sql
CREATE TABLE KB_SHIM.KBF_COST_LOG (
    log_id        VARCHAR2(64)   NOT NULL,   -- UUID
    recorded_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    tool          VARCHAR2(64),
    synth_id      VARCHAR2(64),
    user_id       VARCHAR2(128),
    model         VARCHAR2(128),
    tokens_in     NUMBER(10),
    tokens_out    NUMBER(10),
    cost_usd      NUMBER(12,6),
    operation     VARCHAR2(128),
    CONSTRAINT pk_cost_log PRIMARY KEY (log_id)
);
CREATE INDEX idx_cost_time  ON KB_SHIM.KBF_COST_LOG (recorded_at);
CREATE INDEX idx_cost_user  ON KB_SHIM.KBF_COST_LOG (user_id, recorded_at);
CREATE INDEX idx_cost_synth ON KB_SHIM.KBF_COST_LOG (synth_id);
```

---

## Required code changes (if Option A chosen)

### `framework/skill_builder/conversation.py`

| Method | Current | Change |
|---|---|---|
| `_write_artifacts()` | `Path.write_text()` × 4 | `ADB MERGE INTO KBF_SKILL_ARTIFACTS` × 4, in one transaction. Keep filesystem write as laptop-mode fallback when `pool=None`. |
| `_run_validate()` | Reads `wf_path = REPO_ROOT / ...` | Load `content` CLOB from ADB where `artifact_id = '{persona}.{skill_name}.workflow_skill'`; write to tempfile; pass to `validate_workflow_links()`. Fall back to filesystem in laptop mode. |
| `_run_eval()` | Reads gold sets from filesystem | Load extraction + workflow gold CLOBs from ADB. Fall back to filesystem. |
| `_run_promote()` | No-op (stub) | `UPDATE KBF_SKILL_ARTIFACTS SET status='promoted', promoted_at=SYSTIMESTAMP WHERE skill_name=? AND persona=?` |

The `artifact_store` pattern from ADR-021 is a good model: constructor-inject
an `ArtifactStore`-like `SkillStore` that has filestore and ADB backends,
selected by `KBF_ENV`.

### `framework/deploy/error_store.py`

Replace `ErrorStore` with `AdbErrorStore` (in prod) that writes to
`KBF_ERROR_LOG` and `KBF_BUG_REPORTS` in addition to (or instead of) the
local JSONL files.  Same dual-write pattern: JSONL for `watch-bugs` CLI hot
reads, ADB for durability.

### `framework/deploy/cost_store.py`

Same pattern: `AdbCostStore` writes to `KBF_COST_LOG`. Local JSONL kept as
cache for the `/api/v1/metrics/cost` endpoint.

---

## Done when

The following can be verified after implementation:

1. Complete an `authorSkill` session to PROMOTE.
2. Delete `framework/workflow_skills/{persona}/{skill_name}.yaml` from disk.
3. Start a new session and resume with the same `synth_id`.
4. `_run_validate()` passes — skill loaded from ADB, not filesystem.
5. `SELECT COUNT(*) FROM KBF_SKILL_ARTIFACTS WHERE synth_id = ?` returns 4.
6. `SELECT COUNT(*) FROM KBF_ERROR_LOG` > 0 after any tool error.
7. `SELECT COUNT(*) FROM KBF_BUG_REPORTS` > 0 after calling `reportBug`.
8. `SELECT COUNT(*) FROM KBF_COST_LOG` > 0 after any LLM call.

---

## Decision

**Option A — ADB only, no git-sync. Decided 2026-05-12.**

Future git-sync work tracked as ADR-023 (deferred — see "When to revisit" above).

Backend Dev to implement:
1. Oracle DDL migration — 4 tables (`KBF_SKILL_ARTIFACTS`, `KBF_ERROR_LOG`, `KBF_BUG_REPORTS`, `KBF_COST_LOG`).
2. `AdbSkillStore` — dual-write: ADB in prod, filesystem fallback in laptop mode.
3. Update `_write_artifacts()`, `_run_validate()`, `_run_eval()`, `_run_promote()`.
4. Replace `ErrorStore` + `CostStore` with ADB-backed versions (ADR-022).
5. `kb-cli export-skills` — on-demand SQL → filesystem dump for manual PR or audit.
