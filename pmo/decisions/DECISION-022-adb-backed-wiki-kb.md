---
title: DECISION-022 — ADB-backed wiki/page KB store
status: Accepted
created: 2026-05-18
owner: architect
deciders: user
tags: [arch, data, adb, wiki, portability]
cross_refs:
  - ADR-023 (ADB-always principle)
  - DECISION-008 (bug storage ADB-only)
  - DECISION-020 §3 (canonical_ref contract at write + read)
  - ADR-039 (source-identity canonicalization)
  - host-local-KB defect (wiki/page KB filestore-only → promoted skills not portable)
---

# DECISION-022 — ADB-backed wiki/page KB store

## Status: Accepted

Accepted 2026-05-18. Implementation required immediately — host-local filestore KB
for wiki/page content is a confirmed portability blocker for promoted/consumable skills.

## Problem

`WikiMetadataStore` (and the wiki markdown files it indexes) is filestore-only:
`~/.kbf/store/wiki_metadata/{page_id}.json` + `~/.kbf/wiki/{space}/{page_id}.md`.

This means:

- A skill authored on laptop A and PROMOTED to production carries a `pinned_ref`
  that resolves to a Confluence page that was ingested into that laptop's local
  filestore — NOT into any shared, durable store.
- When the promoted skill is executed on a different host (production VM, laptop B,
  or any host that has not run the same ingest), `_retrieve_author_fixed_pinned`
  Strategy 1a and 1b both find nothing, hard-fail with `ConfluencePageNotInKBError`.
- The skill is silently broken after promotion — the portability guarantee that
  PROMOTE is supposed to provide does not hold.

This violates ADR-023 (ADB-always for promoted/consumable artifacts) and the
polyglot principle: the wiki store type is correct (wiki stays wiki, not collapsed
into another store), but its BACKING must be ADB so ingested pages are accessible
from any host that can reach the shared ADB.

## Decision

Wiki/page KB content MUST be ADB-backed (using the `KB_SHIM` schema, table
`KB_SHIM.KBF_WIKI_PAGES`) so that:

1. Promoted/consumable `author_fixed` skills can retrieve their pinned pages from
   any execution host — not just the laptop that ran INGEST.
2. The canonical_ref contract (DECISION-020 §3) is preserved end-to-end: ingest
   stamps `canonical_ref` into the ADB row; retriever returns it in passage metadata;
   executor `_passage_matches_canonical()` matches canonical==canonical from ADB.
3. Idempotency is enforced by `content_hash` — re-ingesting the same page version
   is a no-op.

### Table: `KB_SHIM.KBF_WIKI_PAGES`

Columns: `page_id` (PK), `canonical_ref` (CLOB/JSON), `title`, `space`, `persona`,
`kb_scope` (freeform scope tag, e.g. persona name), `content` (CLOB, markdown body),
`content_hash`, `citation_url`, `source_url`, `tags` (CLOB/JSON array), `last_modified`,
`ingested_at`, `extraction_version`, `schema_version`.

### ADB-backed store: `AdbWikiMetadataStore`

- Same interface as `WikiMetadataStore` (`upsert_page`, `get_page`, `list_pages`,
  `search_pages`, `delete_page`).
- Additionally stores the full markdown content in the `content` CLOB column (so
  retrievers do not need a filesystem path on the consuming host).
- CLOB columns (`content`, `canonical_ref`, `tags`) use `setinputsizes` (mirror
  `AdbErrorStore` / `AdbSkillStore` CLOB pattern).
- Pool required — no stub-mode fallback (mirrors `AdbSkillStore` contract).

### Factory: `build_wiki_store(pool, env)`

- `pool is not None` → returns `AdbWikiMetadataStore(pool)`.
  Logs: `wiki_store: ADB-backed (KB_SHIM.KBF_WIKI_PAGES)`.
- `pool is None` (explicit no-ADB path only, e.g. unit test isolation) → returns
  `WikiMetadataStore()` (filestore). Logs explicitly:
  `wiki_store: FILESTORE FALLBACK — not suitable for promoted skills`.
  This path is NEVER silent; logging is mandatory so operators know portability
  is compromised. It is NOT the default for any path that serves a promoted skill.

### Filestore retained as explicit laptop/no-ADB fallback only

`WikiMetadataStore` (filestore) is kept and used when:
- ADB is explicitly unavailable (pool=None, e.g. pure unit test isolation).
- Logged at WARNING level: this path must NEVER be silent.

Filestore is NOT the path for any ingest that could feed a PROMOTED skill.

### Migration stance

Re-ingest on demand; NO backfill of existing host-local pages. Any page not yet
in ADB will fail at EVAL/retrieval time with an actionable error (consistent with
prior session decisions on migration-from-filestore).

## Consequences

- Positive: Promoted skills are portable across hosts. The ADB-always guarantee holds.
- Positive: Wiki content is durable — laptop wipe does not destroy ingested knowledge.
- Positive: canonical_ref round-trip (INGEST → ADB → retriever → executor) is
  fully observable and auditable.
- Negative: First INGEST after this change requires ADB connectivity (no silent
  filestore fallback for the promoted-skill path). This is intentional.
- Reversibility: Reversible — filestore is preserved as an explicit fallback.
  Migrating back is config-change only (return filestore from factory).

## Alternatives considered

- **Keep filestore, document the limitation** — rejected. The limitation is not
  cosmetic; it means PROMOTED skills silently break on any host other than the
  authoring laptop. The PROMOTE gate is supposed to mean "portable and consumable".
- **Git-backed wiki (spec default)** — rejected for this use case. Git-backed wiki
  is appropriate for human-readable, diff/PR/blame artefacts. Skill KB pages are
  machine-serving retrieval artefacts; they need a shared, queryable backing store
  that all hosts can read without git checkout. ADB serves this role.

## References

- ADR-023 — ADB-always principle
- DECISION-008 — bug storage ADB-only (pattern precedent)
- DECISION-020 §3 — canonical_ref contract at write + read
- ADR-039 — source-identity canonicalization (canonical==canonical matching)
- Issue-1a (commit 1c95d3d) — executor Strategy 1b + wiki_store wiring
- BUG-queue-43ac1 — bind-side canonicalization gap (DECISION-013)
