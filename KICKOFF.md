# Knowledgebase — Kickoff

Building the **Knowledge Builder Framework** with the [Dev Agent Team](/Users/sravansunkaranam/github/dev-agent-team).

The authoritative spec is `docs/raw/knowledge-builder-framework-spec.md`. This kickoff is the *operating manual* for the team; the spec is the *what*.

## Goal (one paragraph)

A polyglot knowledge layer for LLM consumption: ingest Confluence/Jira/code/fleet → store in purpose-fitted backends (vector, SQL, graph, git wiki) → expose via uniform MCP retrieval tools → orchestrated by a Context Builder agent. **Plus** a per-persona "Knowledge Builder agent" pattern (PM Knowledge Builder, TPM Knowledge Builder, …) where each persona team declares (a) what to extract, (b) which raw sources to pull from. The framework owns plumbing; persona-builder configs own schemas and source lists. See `docs/wiki/persona-knowledge-builder.md`.

## Your First Move

```bash
cd /Users/sravansunkaranam/github/Knowledgebase
claude
```

Then say:

```
TPM, give me a status briefing and start Phase 0. The PM should ingest the spec at
docs/raw/knowledge-builder-framework-spec.md and produce: (a) project-overview, (b) personas,
(c) one module page per data type from §4 of the spec, (d) MVP scope decision aligned to
spec §7 phases. The Architect should draft ADRs covering tech-stack choices from spec §11
and the §6 interfaces. Do NOT design schemas for individual personas — that's owned by the
persona teams themselves (see docs/wiki/persona-knowledge-builder.md).
```

The TPM will:
1. Read CLAUDE.md, the spec, the wiki, the dashboard, open decisions
2. Brief you on current state
3. Coordinate the PM to ingest the spec into wiki pages
4. File DECISION-001 (MVP scope — which phase 1 slice ships first)
5. Coordinate the Architect to draft Phase 0 ADRs

## Phase Plan (from spec §7 — TPM/PM may refine)

| Phase | Scope | Exit criterion |
|-------|-------|----------------|
| **0 — Setup** | Tech-stack ADRs, repo layout (`framework/` per spec §5), eval harness skeleton, persona-builder config schema | Architect's ADRs approved; one passing eval gold-set entry |
| **1 — Skeleton + incident KB** | `core/` interfaces (§6.1–6.3), Confluence+Jira adapters, LLM parser w/ incident schema, vector store, vector_search + get_incident_summary tools, minimal Context Builder | Matches/beats current Aira KB on a 25-question gold set per persona |
| **2 — Fleet + code wiki** | Read-through fleet adapter, `query_fleet` + `text_to_sql` tools, Som-style code wiki on commit, `read_code_page` + `find_symbol` tools | Context Builder answers mixed queries (e.g. "fleet state for tenants impacted by incident X") |
| **3 — PM/TPM wiki + Context Builder maturity** | Git-backed wiki store + frontmatter, Confluence→wiki ingestion (per-persona schema plug-in), shim index, intent classification, parallel tool calls. **Resolves spec §8.1, §8.3.** | Multi-source queries return cited, faithful answers within budget |
| **4 — Permissions, FA semantic graph, polish** | `persona_visibility` enforced at retrieval, FA graph store (Dave's POC), Jira roadmap approach decided, cost dashboards, eval CI | v2 scope decisions made; ops SLOs in place |

**Phase 1 acceptance criteria** (spec §12) — copy into the dashboard:
- New incident flows ingest→retrievable in <5 min
- `vector_search` top-5 with citations <500ms p95
- Context Builder answers ≥80% of incident gold set with grounded citations
- Re-ingesting same Jira ticket = zero rows changed (idempotent)
- Eval runs on every PR, blocks merge on regression
- All ContentItems carry `persona_visibility` and `classification`
- Cost report: tokens/ingest, tokens/retrieve, daily totals

## Open Problems (research, not implementation — spec §8)

These have **no agreed answer**. Each becomes a DECISION before Phase 3.

1. **§8.1** — LLM wiki storage/retrieval for remote agents (git+cached MCP vs TOC-on-demand vs BM25 vs graph-of-wikis vs hybrid). Spec recommends starting with #1+#2; do not jump to vector DB.
2. **§8.2** — Code accessibility for remote agents (VM-spinup vs central pre-built code wiki vs hybrid). Spec recommends hybrid.
3. **§8.3** — TPM/PM extraction schema. **Out of scope for the framework**; persona teams own this. Framework provides schema template + "test your schema" CLI + promotion path (v0 draft → v1 production).

## How to Use the Team

### Invoke an agent
```
PM, ingest docs/raw/knowledge-builder-framework-spec.md into project-overview + persona pages.
Architect, draft ADR-001 for the Python/pgvector tech-stack defaults.
Dev Manager, scaffold framework/core/interfaces.py per spec §6.1–6.3.
QA, write the gold-set seed (5 incident questions) we need for Phase 1 exit.
TPM, status please.
```

The TPM is your default contact for "what's happening?"

### Make a decision
Open files in `pmo/decisions/` with status `open`. Reply with:
```
DECISION-007: I pick option B.
```

### Give an agent feedback
Same protocol as in dev-agent-team — the agent will ask: (a) one-off, (b) project-only, or (c) permanent for all projects.

### Check status
| View | What it shows |
|------|--------------|
| `pmo/dashboard.md` | Live program board: stories, status, decisions, blockers |
| `docs/wiki/current-status.md` | Narrative paragraph (what's happening, what's next) |
| `docs/wiki/log.md` | Chronological log |

## Conversation Logs

Every conversation is automatically logged to:
```
~/Google Drive/AI Projects/Claude/Conversations/Knowledgebase/YYYY-MM-DD.md
```

Local `conversations/` symlink in this repo (gitignored).

## Help

- Spec: [docs/raw/knowledge-builder-framework-spec.md](docs/raw/knowledge-builder-framework-spec.md)
- Per-persona builder concept: [docs/wiki/persona-knowledge-builder.md](docs/wiki/persona-knowledge-builder.md)
- Agent prompts: `dev-agent-team/agents/*.md`
- Shared protocols: `dev-agent-team/shared/*.md`
