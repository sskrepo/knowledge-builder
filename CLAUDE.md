# Knowledgebase — Claude Rules

This project builds the **Knowledge Builder Framework** — a polyglot knowledge layer for LLM consumption that ingests Confluence, Jira, code, and fleet data into purpose-fitted stores (vector, SQL, graph, git-backed wiki) and exposes them as MCP retrieval tools.

The authoritative spec is `docs/raw/knowledge-builder-framework-spec.md`. **Read it before making any structural changes.** Sections **§5 (Component map)** and **§6 (Interfaces)** are load-bearing.

This project uses the **Dev Agent Team** at `/Users/sravansunkaranam/github/dev-agent-team/`. Agent prompts in `.claude/agents/` are symlinks to that canonical source. Updates there propagate to this project.

## What we are building (one paragraph)

A *framework* (infrastructure: ingestion → storage → retrieval → orchestration), **not** a content definition. Each persona team (PM, TPM, Architect, Dev Manager, DevOps, Exec) gets a **persona-specific Knowledge Builder agent** that declaratively specifies (a) what to extract, (b) which raw sources to pull from (Confluence space, Jira filter, code repo, fleet DB) and produces a knowledge base that downstream **use-case agents** (e.g. Aira, internal portals) can query through the framework's uniform MCP retrieval surface. The framework owns the plumbing; persona-builder configs own the schemas and source lists. See `docs/wiki/persona-knowledge-builder.md`.

## Knowledge System (Wiki Pattern)

This project uses the [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern:

- **Raw sources** → `docs/raw/` (immutable inputs — the spec, meeting notes, future research)
- **Wiki** → `docs/wiki/` and `pmo/` (LLM-compiled living knowledge)
- **Schema** → this file

## Session Protocol (mandatory)

### Session Start
Every agent reads (in order):
1. This file (`CLAUDE.md`)
2. The relevant `dev-agent-team/shared/*-protocol.md` files
3. `docs/wiki/index.md` — what wiki pages exist
4. `docs/wiki/current-status.md` — where we are
5. `docs/wiki/log.md` — recent session history
6. `pmo/dashboard.md` — current phase, blockers, decisions
7. `docs/raw/knowledge-builder-framework-spec.md` — the spec (skim §1–4, deep read on whichever section your task touches)
8. Topic-specific wiki pages relevant to the current task (don't read everything)

### During Work
- New decision → update relevant wiki page + file `pmo/decisions/DECISION-NNN-*.md` if user input needed
- New raw doc → register in `manifests/raw_sources.csv`
- Code changes → if they diverge from wiki, update wiki in same session

### Session End
- Append to `docs/wiki/log.md`: `## [YYYY-MM-DD] {agent} | {what changed}`
- Update `docs/wiki/current-status.md`
- Update `pmo/dashboard.md` rows you touched

### Conversation Logging
Hooks in `.claude/settings.json` automatically write every prompt/response to:
`~/Google Drive/AI Projects/Claude/Conversations/Knowledgebase/YYYY-MM-DD.md`

You don't need to do anything — it happens automatically. The `conversations/` symlink in this repo points to that folder for local access (gitignored).

## Project Structure

```
Knowledgebase/
├── CLAUDE.md                    # This file — session protocol + framework rules
├── KICKOFF.md                   # How to start
├── .claude/
│   ├── settings.json            # Hooks for conversation logging
│   ├── scripts/                 # Hook scripts
│   └── agents/                  # Symlinks to dev-agent-team/agents/
├── conversations/ → ~/Google Drive/...  # Symlink, gitignored
├── docs/
│   ├── raw/                     # Immutable raw sources (spec, meeting notes)
│   └── wiki/                    # LLM-compiled knowledge
│       ├── index.md             # Catalog of wiki pages
│       ├── log.md               # Session log
│       ├── current-status.md    # Where we are
│       ├── project-overview.md  # PM-owned: vision, personas
│       ├── persona-knowledge-builder.md  # Per-persona builder agent concept
│       ├── architecture.md      # Architect-owned: framework shape (mirrors spec §3)
│       ├── data-model.md        # Architect-owned: ContentItem/Chunk/Edge (spec §6.1)
│       ├── api-design.md        # Architect-owned: MCP retrieval tool surface (spec §6.4)
│       ├── engineering/         # Dev Manager-owned: conventions
│       ├── adr/                 # Architecture Decision Records
│       └── module-*.md          # One page per data-type module (incidents, fleet, code, pm-tpm-wiki, fa-graph)
├── pmo/
│   ├── dashboard.md             # TPM-owned: live program view
│   ├── phases.md                # Phase scope (mirrors spec §7)
│   ├── stories/                 # User stories (PM writes after PDD approved)
│   ├── decisions/               # Decisions awaiting/recorded — incl. open problems §8
│   ├── handoffs/                # Cross-agent handoff records
│   ├── uat/                     # QA UAT scenarios (gold-set eval queries)
│   └── bugs/                    # QA bug reports
├── manifests/
│   └── raw_sources.csv          # Index of every file in docs/raw/
└── framework/                   # Implementation root (created when Phase 1 starts)
    └── ...                      # See spec §5 component map: core/ adapters/ parsers/
                                 # stores/ retrievers/ orchestrator/ ingestion/ eval/ deploy/
```

> The default `init-project.sh` created `api/`, `server/`, `web/` folders. We do **not** use those — this is not a web app. The Architect should create `framework/` per spec §5 when Phase 1 starts and may delete the unused stubs.

## Tech Stack (defaults from spec §11 — Architect to confirm in Phase 0 ADRs)

These are *recommendations from the spec, not prescriptions*. Architect files an ADR per choice.

| Layer | Default | Why |
|---|---|---|
| Language | Python (ingestion, parsers, orchestrator) | Best LLM/embedding tooling |
| Vector store | pgvector (escalate to Qdrant if scale demands) | Boring/correct; metadata filtering |
| Graph | Oracle Property Graph (already in stack) | Native vertex tables on relational data |
| Wiki storage | Git repo (markdown + YAML frontmatter) | Diff/PR/CI/blame for free |
| Wiki serving | FastAPI service with content cache, exposed as MCP | Decouples wiki source from agent runtime |
| Fleet | Existing UDAP / Sentinel (read-through, no re-ingest) | No reason to move it |
| Code structural index | Som-style LLM wiki, regenerated on commit | Outperforms code embeddings for navigation |
| Orchestration | LangGraph or thin custom layer over MCP | Team preference; defer until Phase 3 |
| Eval | Custom harness + Ragas for faithfulness | Eval discipline is non-negotiable |

**No Node/Next/Clerk/Twilio.** The default sports-app stack from the dev-agent-team templates does not apply here.

## Framework Discipline (mandatory — from spec §2 core principles)

These are load-bearing assumptions. **Every PR must respect them.** If a change requires breaking one, file a DECISION first.

1. **Polyglot, not unified.** Different data types live in different stores; framework abstracts over them.
2. **LLM-in-ingestion ≠ LLM-in-retrieval — decide separately.** Reserve LLM in retrieval for *final synthesis* and *orchestration*, not traversal.
3. **Deterministic extraction rules over autonomous LLM extraction.** Provide schemas up front (`parsers/schemas/*.json`).
4. **Storage is consequence of retrieval pattern**, not a starting choice.
5. **Every content creation flows through the parser.** Bypassed content goes stale.
6. **Don't LLM-parse data with no summary value.** Schema-defined data → relational store, full stop.
7. **Framework provides infrastructure, not content definition.** Persona teams own their extraction schemas.
8. **Permissions/ACLs are v2.** But carry `persona_visibility`, `owner`, `classification` on every ContentItem from day one.

## Cross-cutting requirements (spec §10)

Build these in from day one — non-negotiable v1 requirements:

- **Citations** — every retriever returns source URLs/paths. No citation = bug.
- **Idempotent ingestion** — content-hash IDs. Re-running is a no-op.
- **Incremental updates** — webhooks/git-push triggers; full re-index only on schema/model change.
- **Versioning** — every chunk carries `source_sha`, `parser_version`, `schema_version`.
- **Cost telemetry** — log tokens-per-ingest and tokens-per-retrieve.
- **Eval harness** — gold question sets per persona; recall@k, faithfulness, latency, cost. Runs in CI.
- **ACL placeholders** — `persona_visibility` and `classification` on every ContentItem.

## Project-Specific Agent Rules

- **Architect**: produce ADRs that map directly to spec §6 interfaces. Do not invent abstractions the spec doesn't ask for.
- **PM**: persona model is fixed by the spec (PMs, TPMs, Architects, Dev Mgrs, Devs, DevOps, Execs, Aira). Don't re-derive personas; extend with use-case-agent personas as they appear. Each persona gets its own **Knowledge Builder config** — see `docs/wiki/persona-knowledge-builder.md`.
- **Dev Manager**: framework code lives in `framework/`. Module folders match spec §5 names (`core/`, `adapters/`, `parsers/`, `stores/`, `retrievers/`, `orchestrator/`, `ingestion/`, `eval/`, `deploy/`).
- **QA**: "tests" here means the **eval harness** (recall@k, faithfulness, latency, cost). Story acceptance includes a gold-set delta. Spec §12 lists v1 acceptance criteria.
- **Open problems (spec §8)** are *research tasks*, not implementation tasks. Don't let any agent guess past them — file a DECISION instead.

## Rules

- **Compile-first**: don't just answer, write conclusions into wiki pages
- **Writeback is mandatory**: durable knowledge → wiki
- **Wiki and code MUST agree**: if they don't, that's incomplete work
- **Raw files are immutable**: never edit `docs/raw/`. Compile into wiki instead.
- **Decisions are first-class**: file `pmo/decisions/DECISION-NNN-*.md` for anything user needs to weigh in on
- **Bring options, not problems**: every decision has 2-3 concrete options with pros/cons
- **One agent owns each artifact**: see [dev-agent-team/agents/](../dev-agent-team/agents/) for ownership
- **Eval + wiki land in same change as code**: never ship parser/store/retriever changes without both
