# DECISION-008: Single Source of Truth for Bugs — ADB Only

**Status**: DECIDED  
**Date**: 2026-05-12  
**Decided by**: User  
**Informed by**: bug bash design session, BUG-009 fix, analysis of ADB vs filesystem read efficiency

---

## Context

The framework accumulates bugs from three sources:
1. **User-reported** — filed via `reportBug` MCP tool → `AdbErrorStore.record_user_bug()` → `KBF_BUG_REPORTS` ADB table (+ local JSONL dual-write)
2. **Critic-found** — filed by `reviewSkillSession` → `KBF_AUDIT_RUNS` ADB table
3. **Formally filed** — hand-written `pmo/bugs/BUG-NNN-*.md` files in the git repo

This split creates ambiguity: which store is authoritative? Does a `pmo/bugs/` file mean the bug is known/fixed, or just that someone noticed it? The `watch-bugs` CLI was already reading from both JSONL and `pmo/bugs/*.md` to deduplicate — an unmaintainable hybrid.

The question surfaced when designing `kbf_ops.bugBash`: should bug bash read from `pmo/bugs/*.md`, ADB, or both?

---

## Options Considered

### Option A — ADB only (CHOSEN)
- `KBF_BUG_REPORTS` + `KBF_AUDIT_RUNS` are the single source of truth
- `pmo/bugs/*.md` files become **generated export artifacts** — produced on demand by `kb-cli export-bugs`, not hand-written and not queried
- `bugBash` MCP tool reads from ADB only

**Efficiency analysis**:

| | Filesystem `pmo/bugs/*.md` | ADB `KBF_BUG_REPORTS` |
|---|---|---|
| 10 bugs | ~5 ms (10 file reads) | ~15 ms (1 SQL round-trip) |
| 100 bugs | ~30 ms | ~20 ms (ADB wins) |
| 1000 bugs | 100+ ms | ~30 ms |
| Pre-filter by status/severity | Python loop after full read | `WHERE`/`ORDER BY` before bytes cross the wire |
| Content shape for Claude | Prose markdown — Claude must parse structure | Structured JSON fields — direct triage |

ADB wins at any non-trivial count because a single SQL query with `WHERE status != 'resolved' ORDER BY severity DESC` does all filtering server-side. Markdown requires read-all → parse → filter in Python.

More importantly: structured fields (`queue_id`, `tool`, `description`, `severity`, `timestamp`) are cheaper for Claude to triage than parsing prose markdown. The LLM spends tokens parsing headers instead of reasoning about bugs.

### Option B — Filesystem only
- `pmo/bugs/*.md` remains the source of truth
- ADB stores remain write-only (or dual-write with filesystem as primary)
- Incompatible with ADB-always policy (ADR-023); filesystem is not queryable

### Option C — Hybrid (rejected)
- Both stores query at bug bash time
- Dedup logic required across sources
- Creates exactly the maintenance problem we're trying to eliminate

---

## Decision

**Option A: ADB is the single source of truth for all bug records.**

- `KBF_BUG_REPORTS` owns user-reported bugs
- `KBF_AUDIT_RUNS` owns critic-found audit findings
- `pmo/bugs/BUG-NNN-*.md` files already written are historical; no new ones are hand-written
- Bug bash MCP tool (`kbf_ops.bugBash`) reads from ADB only
- `kb-cli export-bugs` generates markdown from ADB on demand — these are read-only snapshots, not primary records
- `kb-cli watch-bugs` dedup logic updated: "known" = has `queue_id` in `user_bugs.jsonl` (local ADB cache); filesystem `pmo/bugs/*.md` dedup removed

---

## Consequences

- Developers wanting a markdown view of open bugs run `kb-cli export-bugs --out-dir pmo/bugs/`
- Generated files have YAML frontmatter + `<details>` expandable sections — readable in GitHub/IDE
- An `INDEX.md` is also generated listing all open bugs in a table
- The JSONL files (`user_bugs.jsonl`, `errors.jsonl`) remain as local hot-read caches for `watch-bugs` (ADB dual-write keeps them in sync)
- `pmo/bugs/` is now effectively a `.gitignore`-able generated directory (team may choose to commit exports for audit trail, but they are not editable)

---

*Amended by **DECISION-013** (2026-05-16): adds a third discovery channel — agent/architect-proactively-discovered defects — filed into `KBF_BUG_REPORTS` via `record_user_bug` with a `discovered_by` discriminator. ADB remains the single source of truth; `pmo/bugs/` remains generated.*
