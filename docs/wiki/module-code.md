---
title: Module — Code knowledge (spec §4.3)
source: docs/raw/knowledge-builder-framework-spec.md (§4.3, §8.2)
compiled_at: 2026-05-04T00:00:00Z
created: 2026-05-04
owner: pm
tags: [module, code, phase-2]
status: current
---

# Module — Code knowledge

> **Status: structure-indexed, NOT vectorized.** Per spec §4.3, raw code embeddings underperform structural lookup for navigation. Phase 2 ships this.

## Sources
- **Git repos** — APIs, workflows, OpenAPI specs

## Ingestion (Som-style code wiki — rule-driven, not LLM-summarized at the symbol level)
On each commit:
1. Adapter walks repo tree
2. Per-module summary generation (LLM-assisted but bounded — fixed schema; spec §2.3)
3. Per-symbol entries (rule-based: AST-derived signature, location, imports/exports)
4. OpenAPI specs go into a separate **typed structured index** (queryable as SQL, not as prose)

## Storage
- **`kb_code` schema** in Autonomous DB:
  - `code_pages` — markdown bodies for module summaries + path index
  - `symbols` — name, kind, path, line ranges, signature
  - `symbol_refs` — call/import graph if the language adapter produces one
- **No vector embeddings** on code itself.

## Retrieval
- Browsing / agent navigation: `read_code_page(path)`, `list_code_pages(repo, path_prefix?)`
- Exact symbol lookup: `find_symbol(name, kind?)` — returns paths + line numbers
- For OpenAPI specs: a typed query tool (Phase 2 deliverable; lightweight wrapper over the OpenAPI structured index)
- For full-text fallback: `ripgrep` / AST search tool when symbol lookup misses

## Open problem (spec §8.2) — remote-agent code access
A remote agent like Aira can't drag the whole codebase into its context. Three candidates:
1. **VM-spinup** (Rajeev's proposal) — agent clones repo into a VM, regenerates the code wiki via the skill, operates locally, opens a PR. Heavy but isolated.
2. **Pre-built code wiki served centrally** — CI builds the code wiki on every commit, publishes to a read-only MCP service. Cheap reads; no write/PR ability without #1.
3. **Hybrid (recommended)** — #2 for read-only / Q&A workloads; #1 only when the agent needs to actually modify code.

Decision deferred to **DECISION-005** (to be filed by Architect at Phase 2 kickoff).

## Sample queries
- "Where is the auth flow implemented?"
- "Which services consume the customer-events Kafka topic?"
- "Show me the OpenAPI definition for /v1/customers"

## Acceptance criteria (Phase 2)
- Code wiki regenerates on commit within configured SLA (target: <60s for typical change)
- `find_symbol` returns hits with line-level citations
- Code wiki ingestion is idempotent on `git_sha` match

## Open items
- **Multi-repo strategy** — for the FA codebase (many repos), do we one-repo-per-config or one super-config with multiple repos? Architect to file ADR in Phase 2.
- **Generated-code handling** — autogen'd files inflate the symbol index; rule-based exclusion list.
