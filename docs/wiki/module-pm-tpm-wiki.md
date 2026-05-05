---
title: Module — PM & TPM wiki (spec §4.4)
source: docs/raw/knowledge-builder-framework-spec.md (§4.4, §8.1, §8.3)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [module, wiki, pm, tpm, phase-3]
status: current
---

# Module — PM & TPM wiki

> **Status: storage open, schema deferred to persona teams.** Phase 3 ships this. Resolves spec §8.1 (storage/retrieval) at Phase 3 entry; resolves §8.3 (extraction schema) via [ADR-004](adr/ADR-004-persona-builder-config.md) (persona teams own their schemas).

## Sources
- **Confluence** — product defs, TPM wikis, design docs, weekly ops summaries, ECARs, compliance docs
- **Optionally** — git-backed design-doc repos (per persona-builder config)

## Ingestion (LLM-driven)
- LLM parser produces summary + entity extraction + edges (links to services/owners)
- Should be **auto-maintained**: TPM agents producing weekly summaries naturally fit the wiki page format

**Per-persona extraction schemas** (PM, TPM) are owned by the respective persona teams per [ADR-004](adr/ADR-004-persona-builder-config.md). The framework provides:
- `framework/parsers/schemas/_template.json` — JSON Schema starter
- `framework/persona_builders/_template.yaml` — persona-builder YAML starter
- `kb-cli` — validate / dry-run / eval / promote (Phase 3 fully implements; Phase 0 ships skeleton)

## Storage (resolves spec §8.1 at Phase 3 entry)
**Default plan** (recommended starting point per spec §8.1):
- **Wiki bodies in git** (markdown + YAML frontmatter), single canonical repo `kb-wiki/{persona}/...`
- **Wiki metadata in `kb_wiki_meta`** (Autonomous DB) — frontmatter, path, git SHA, links
- **Always-loaded TOC + page summaries** in the Context Builder prompt (cheap, debuggable)
- **`read_wiki_page(path)`** for on-demand fetch
- **`search_wiki(query)`** — start with simple title/heading match, add BM25 / hybrid only if eval shows recall gaps

**Candidates rejected for v1**: vector DB as default (overkill for curated wiki; spec rules out as default), full-on graph-of-wikis (interesting v2).

## Retrieval
- `search_wiki(query, persona?, max_results?)` — hybrid (text first; vector fallback if behind threshold)
- `read_wiki_page(path)` — fetch full page body (DB metadata → git blob → cached HTML)
- `vector_search(corpus="pm" | "tpm", query)` — semantic recall when search_wiki underperforms

## Sample queries
- "What's the rollout plan for feature X in 25.01?" (PM)
- "Who owns the workflow engine and what's its current SLO?" (TPM/Architect cross)
- "Summarize the last 4 weeks of ops issues for the customer-events service" (TPM)
- "What ECAR exceptions are open for tenant-99?" (TPM compliance)

## Acceptance criteria (Phase 3)
- PM and TPM Knowledge Builder configs ship in `framework/persona_builders/`
- Each persona's gold set lives in `eval/gold_sets/{persona}.jsonl`; recall@5 ≥ 80%
- Wiki content survives a re-ingest with zero changed rows when source unchanged (idempotency)
- Cross-source query (e.g., TPM ops summary + fleet roll-up) returns cited answer in <2s p95

## Open items
- **Confluence webhook plumbing** — incremental update path; vendor docs pending review.
- **Cross-persona dedup** — per ADR-004 we accept duplication; revisit if storage costs surprise.
- **Wiki PR review owner** — who approves changes to `kb-wiki/pm/...`? Default: PM persona team owners; CODEOWNERS file in the wiki repo.
