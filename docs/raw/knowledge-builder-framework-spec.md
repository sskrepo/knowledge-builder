# Knowledge Builder Framework — Implementation Brief

> **Audience:** Claude Code (and engineers driving it).
> **Source:** synthesized from architecture discussions and two design meetings (May 4, 2026 — "LLM Wiki vs KB Approaches", Meetings 1 & 2).
> **Status:** Draft v1 — ready to start skeleton implementation. Settled decisions and open problems are clearly separated.
> **Non-goal:** this brief defines the *framework* (infrastructure for ingestion, storage, retrieval, orchestration). It does NOT define what knowledge each domain extracts — that is owned by the data-type teams.

---

## 1. Context

We run a cloud platform for Fusion Applications, scaled from 0 → 20K+ customer instances over four years. AI adoption is happening across all personas (PMs, TPMs, Architects, Dev Mgrs, Devs, DevOps, Execs). We need a **central knowledge layer for LLM consumption** so each persona/agent does not re-ingest context from scratch.

Knowledge currently lives in:

- **Confluence** — product defs, TPM wikis, design docs, weekly ops, ECARs, compliance
- **Jira** — project tickets, incident management
- **Code** — APIs, workflows, OpenAPI specs
- **Fleet data** — UDAP-style unified store (instances, metadata, ops data)

We rejected a single unified store. The framework is **polyglot by design**: each data type uses the storage and retrieval shape that fits its access pattern. LLM is used in ingestion where it adds value, and avoided in retrieval where cheaper deterministic mechanisms exist.

---

## 2. Core principles (settled)

These were debated and agreed across both meetings. Encode them as load-bearing assumptions in the framework.

1. **Polyglot, not unified.** Different data types live in different stores. The framework abstracts over them; it does not force them into one shape.

2. **LLM-in-ingestion ≠ LLM-in-retrieval — decide separately.**
   - LLM in ingestion is valuable when summarization, context extraction, or relationship inference adds reusable signal (operational incident data, design docs).
   - LLM in retrieval is wasteful when cheaper mechanisms (vector search, SQL, graph traversal, file read) already produce the answer. Reserve LLM for *final synthesis* and for *orchestration decisions*, not for traversal.

3. **Deterministic extraction rules over autonomous LLM extraction.** Provide the LLM with business rules / ontology up front. Don't let the LLM decide what's important field-by-field.

4. **Storage format is a *consequence* of the retrieval pattern, not a starting choice.** Pick storage by how the data will be retrieved, not by where it came from.

5. **Every content creation must flow through the parser.** Bypassed content goes stale. The parser is either an LLM (for contextual content) or a deterministic adapter (for structured content).

6. **Don't LLM-parse data that has no summary value.** 20K pods × 20 properties is a SQL table, not a wiki. Schema-defined data with instances → relational store, full stop.

7. **The framework provides infrastructure, not content definition.** What to extract from a TPM doc or a design doc is owned by that domain team. The framework provides the ingestion contract, the store, the retrieval surface, and the eval harness.

8. **Permissions and access control are a v2 layer.** Recognized as critical, but not in the v1 scope. Design store metadata so ACLs can be added later without a rewrite (carry `persona_visibility`, `owner`, `classification` from day one).

---

## 3. Architecture

Four layers, top to bottom:

```
┌──────────────────────────────────────────────────────────────────┐
│  Personas / Agents (PMs, TPMs, Devs, DevOps, Execs, Aira)       │
└──────────────────────────────────────────────────────────────────┘
                              ↓ queries
┌──────────────────────────────────────────────────────────────────┐
│  Context Builder Agent (orchestration)                           │
│  - reads "shim layer" of LLM wikis to learn what exists          │
│  - picks data sources based on query intent                      │
│  - calls retrieval tools, assembles context packet               │
└──────────────────────────────────────────────────────────────────┘
                              ↓ tool calls (MCP)
┌──────────────────────────────────────────────────────────────────┐
│  Retrieval Tools (one per store, uniform MCP surface)            │
│  search_wiki · vector_search · sql_query · graph_traverse ·      │
│  read_code_page · get_incident_summary · ...                     │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  Stores (polyglot)                                               │
│  ┌──────────┬───────────┬──────────┬──────────┬──────────────┐  │
│  │ LLM Wiki │ Vector DB │ SQL/UDAP │ Graph DB │ Code Wiki    │  │
│  │ (git)    │ (incidents)│(fleet)  │(semantic)│ (Som-style)  │  │
│  └──────────┴───────────┴──────────┴──────────┴──────────────┘  │
└──────────────────────────────────────────────────────────────────┘
                              ↑ writes
┌──────────────────────────────────────────────────────────────────┐
│  Ingestion Pipelines (parser + adapter per source)               │
│  - LLM parser: Confluence pages, design docs, incident bodies    │
│  - Rule parser: Jira fields, fleet rows, code AST/structure      │
└──────────────────────────────────────────────────────────────────┘
                              ↑
┌──────────────────────────────────────────────────────────────────┐
│  Raw sources: Confluence · Jira · Code repos · UDAP/Sentinel     │
└──────────────────────────────────────────────────────────────────┘
```

Two flows:

- **Ingestion (write path):** source change event → adapter → parser (LLM or rule) → store(s). Idempotent, incremental, content-hashed.
- **Retrieval (read path):** persona/agent query → Context Builder → tool selection → store retrieval → synthesizer with citations.

---

## 4. Data type catalog

Each data type is a first-class entry in the framework. New data types follow the same template.

### 4.1 Operational incident data — **proven (Aira)**

- **Source:** Jira incident tickets + log files + service/pod metadata
- **Ingestion:** LLM-driven. Summarize each incident (root cause, logs excerpt, impact, resources affected). Index raw data alongside summaries.
- **Store:** Vector DB (chunked summaries + raw fields as metadata). Graph edges for `incident → service → owner → tenant`.
- **Retrieval:** Vector search + metadata filter. NOT LLM-driven traversal — vector outperformed lexical and LLM in our Aira evals.
- **Sample queries:**
  - "What incidents touched service X in the last 30 days?"
  - "What's the blast radius if auth-service goes down?" (combine historical incidents + dependency graph)
  - "Show resolutions for ORA-XXXX errors on tenant Y"

### 4.2 Fleet data — **straightforward (no debate)**

- **Source:** UDAP / Sentinel (structured rows: instances, pods, properties, ops events)
- **Ingestion:** None via LLM. Native ingest into the existing store.
- **Store:** SQL (Sentinel or equivalent). Stays in place.
- **Retrieval:** Tool-based. Two MCP tools:
  - `query_fleet(filters, projection)` — typed query for known shapes
  - `text_to_sql(nl_query)` — constrained schema, with view-level guardrails
- **Optional:** materialize hot rollups (counts by patch level, top-N noisy tenants) and promote summaries into the wiki layer.
- **Sample queries:**
  - "How many instances are on patch 24.05.1?"
  - "Which pods owned by team Z had restart spikes this week?"

### 4.3 Code — **structure-indexed, not vectorized**

- **Source:** Git repos (APIs, workflows, OpenAPI specs)
- **Ingestion:** Som's LLM-wiki-for-code approach. Build a structural index (folder tree + per-module summary + per-symbol entry) — NOT raw vector embeddings of code.
- **Store:** LLM wiki (markdown in git) describing code structure. Skill/script that builds it on commit.
- **Retrieval:**
  - For human/agent browsing: `list_code_pages`, `read_code_page(path)`
  - For exact symbol lookup: ripgrep / AST-aware search tool
  - OpenAPI specs: separate structured index (typed, queryable)
- **Open problem:** how remote agents (Aira) access this. Current proposal: agent spins up a VM, clones repo, regenerates the code wiki via the skill, then operates locally. See §8.
- **Sample queries:**
  - "Where is the auth flow implemented?"
  - "Which services consume the customer-events topic?"

### 4.4 Product management & TPM documentation — **open on storage**

- **Source:** Confluence (product defs, TPM wikis, design docs, weekly ops, ECAR)
- **Ingestion:** LLM-driven (summary, entity extraction, link to services/owners). Should be auto-maintained going forward — TPM agents producing weekly summaries naturally fit the wiki page format.
- **Store:** LLM wiki (markdown). Likely git-backed for revision control, update tracking, and PR-style review hooks. **See §8 — storage/retrieval for remote agent access is the open problem.**
- **Retrieval:** TBD pending §8 outcome. Default plan:
  - TOC + page summaries always loaded (cheap, fits in context)
  - `read_wiki_page(path)` and `search_wiki(query)` tools
  - Add BM25 / hybrid retrieval if recall becomes a problem
- **Sample queries:**
  - "What's the rollout plan for feature X in 25.01?"
  - "Who owns the workflow engine and what's its current SLO?"

### 4.5 FA semantic data — **graph-based (Dave's POC)**

- **Source:** FA schema definitions, object relationships, business rules
- **Ingestion:** Rule-driven extraction into property graph (nodes = objects, edges = relationships, rules = constraints).
- **Store:** Graph DB (Oracle property graph natively, or Neo4j). Vertex tables + edge indexes as metadata layer on relational data.
- **Retrieval:** Graph queries (PG/Cypher). Use vector embeddings to *find* starting nodes, then traverse deterministically.
- **Sample queries:**
  - "If I add object X with rule Y, which existing constraints are violated?"
  - "What downstream objects depend on table T?"

### 4.6 Jira roadmap data — **open**

- Service-specific roadmaps may be the right unit of aggregation. Approach TBD; treat as a v2 candidate.

---

## 5. Component map (what to build)

Build these modules. Names are suggestions; the structure matters more than the labels.

```
knowledge-builder/
├── core/
│   ├── interfaces.py          # ABCs for Source, Parser, Store, Retriever
│   ├── content.py             # ContentItem, Chunk, Metadata schemas
│   └── events.py              # ingestion events, change detection
├── adapters/                  # one per source
│   ├── confluence_adapter.py
│   ├── jira_adapter.py
│   ├── code_adapter.py
│   └── fleet_adapter.py
├── parsers/
│   ├── llm_parser.py          # summarize, extract entities, extract relationships
│   ├── rule_parser.py         # deterministic field-mapping
│   └── code_wiki_parser.py    # Som-style code structure builder
├── stores/
│   ├── vector_store.py        # incident KB
│   ├── sql_store.py           # fleet wrapper (read-only)
│   ├── graph_store.py         # FA semantic + cross-cutting entities
│   ├── wiki_store.py          # git-backed LLM wikis (PM/TPM/code)
│   └── code_store.py          # code wiki + AST/symbol lookup
├── retrievers/                # MCP tool implementations
│   ├── tools.py               # MCP server wiring
│   ├── search_wiki.py
│   ├── vector_search.py
│   ├── query_fleet.py
│   ├── text_to_sql.py
│   ├── graph_traverse.py
│   └── read_code_page.py
├── orchestrator/
│   ├── context_builder.py     # the agent that picks tools
│   ├── shim_index.py          # the wiki-of-wikis: what exists where
│   └── synthesizer.py         # final answer assembly with citations
├── ingestion/
│   ├── pipeline.py            # source → parser → store, idempotent
│   ├── change_detection.py    # webhooks, git diff, CDC
│   └── scheduler.py           # backfill, incremental, repair
├── eval/
│   ├── gold_sets/             # per-persona question sets
│   ├── runners.py             # recall, faithfulness, latency, cost
│   └── reports.py
└── deploy/
    ├── mcp_server.py          # exposes retrievers as MCP tools
    └── config/                # store endpoints, model IDs, ACL stubs
```

### 5.1 Key responsibilities

- **Adapters** know how to read a source (auth, pagination, change events). Output: stream of `RawItem`s. No interpretation.
- **Parsers** turn `RawItem` into `ContentItem(s) + Chunk(s) + Edge(s)`. LLM parser carries the *prompt template* and *extraction schema* per data type. Rule parser carries field maps.
- **Stores** persist `ContentItem`/`Chunk`/`Edge` and serve queries. Each store implements a small uniform interface (`upsert`, `delete`, `query`) plus its specialized methods.
- **Retrievers** are thin tools over stores, exposed via MCP. One concept per tool. Predictable inputs/outputs. Always returns citations.
- **Context Builder Agent** is the orchestration brain. Given a query, it (a) inspects the shim index to understand which sources matter, (b) calls retrieval tools, (c) hands a context packet to the synthesizer.
- **Ingestion pipeline** is incremental and idempotent. Content-hash chunk IDs so re-ingesting the same content is a no-op.
- **Eval** is mandatory from v1. No store/parser change ships without a gold-set delta.

---

## 6. Interfaces (concrete enough to implement)

### 6.1 Content model

```python
@dataclass
class ContentItem:
    id: str                       # stable, content-hash + source path
    source: str                   # "confluence" | "jira" | "code" | "fleet" | ...
    source_id: str                # e.g. confluence page id
    path: str                     # human-readable canonical path
    title: str
    body: str                     # raw or summarized
    metadata: dict                # owner, persona_visibility, classification,
                                  # last_reviewed, links, sha, timestamps
    chunks: list["Chunk"]
    edges: list["Edge"]           # relationships extracted

@dataclass
class Chunk:
    id: str                       # f"{content_id}#chunk_{i}"
    text: str
    heading_path: list[str]
    embedding: list[float] | None
    metadata: dict                # inherits + chunk-specific (page_no, span)

@dataclass
class Edge:
    src: str                      # entity URN
    dst: str
    rel: str                      # "owns" | "depends_on" | "references" | ...
    metadata: dict
```

### 6.2 Parser contract

```python
class Parser(Protocol):
    name: str
    input_kinds: set[str]         # which RawItem kinds it handles
    def parse(self, raw: RawItem, ctx: ParseContext) -> ContentItem: ...
```

LLM parser implementations get a **fixed extraction schema** per data type (the "deterministic business rules" requirement). Schemas live in `parsers/schemas/*.json` and are versioned.

### 6.3 Store contract

```python
class Store(Protocol):
    kind: str                     # "vector" | "sql" | "graph" | "wiki" | "code"
    def upsert(self, items: list[ContentItem]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def query(self, q: Query) -> list[Result]: ...
```

Store-specific extensions (vector knn, graph traverse, sql exec) live as additional methods on the concrete classes.

### 6.4 Retrieval tools (MCP surface)

Each tool follows a uniform shape:

```
tool: search_wiki
input: { query: string, persona?: string, max_results?: int }
output: { results: [{ path, title, snippet, score, citation_url }], ... }
```

Standard tool set for v1:

- `search_wiki(query, persona?)` — hybrid search over LLM wikis
- `read_wiki_page(path)` — fetch a full wiki page
- `vector_search(corpus, query, filters?)` — semantic recall over a named corpus
- `query_fleet(filters, projection)` — typed fleet read
- `text_to_sql(nl_query)` — constrained NL→SQL with allowlisted views
- `graph_traverse(start_entity, edge_types, depth)` — semantic graph
- `read_code_page(path)` — code wiki page
- `find_symbol(name, kind?)` — code symbol lookup
- `get_incident_summary(incident_id)` — short, structured incident view
- `list_sources()` — what's available; used by the Context Builder

### 6.5 Context Builder

```python
class ContextBuilder:
    def build(self, query: str, persona: Persona) -> ContextPacket:
        # 1. read shim index (cheap, mostly cached)
        # 2. classify query intent → choose tools
        # 3. call tools (parallel where possible), with budget
        # 4. dedupe, rerank, attach citations
        # 5. return ContextPacket(passages, citations, used_tools, cost)
```

The Context Builder is the only LLM-heavy component on the read path. Everything else stays deterministic.

### 6.6 Shim index (the "wiki of wikis")

A small registry — itself an LLM wiki — describing every store, every wiki tree, and a one-line summary of what's there. Loaded into the Context Builder's prompt. Acts as the mental map.

```yaml
# shim_index.yaml
sources:
  - name: pm_wiki
    kind: wiki
    root_path: kb-wiki/pm/
    summary: "Product definitions, feature briefs, release plans for ADP/FA"
    persona_visibility: [pm, tpm, architect, dev_mgr]
    retrieval_tools: [search_wiki, read_wiki_page]

  - name: incident_kb
    kind: vector
    summary: "Summarized incidents 2022→present, ~50K tickets, with service/owner edges"
    retrieval_tools: [vector_search, get_incident_summary]

  - name: fleet
    kind: sql
    summary: "Live fleet state: 20K+ instances, pod inventory, ops events. UDAP/Sentinel."
    retrieval_tools: [query_fleet, text_to_sql]

  # ... etc
```

---

## 7. Phased build plan

Ship a thin slice end-to-end before broadening. Each phase has an exit criterion that maps to the eval harness.

### Phase 1 — Skeleton + incident KB end-to-end (proven path)
- `core/` interfaces + content model
- Confluence + Jira adapters (read-only)
- LLM parser with incident extraction schema (root cause, impact, resources)
- Vector store + edges in graph
- `vector_search`, `get_incident_summary` tools
- Minimal Context Builder (fixed routing)
- Eval gold set: 25 incident-related questions per persona
- **Exit:** matches or beats current Aira KB on the gold set

### Phase 2 — Fleet via tools + code wiki
- Fleet adapter is read-through (no ingestion); wrap as MCP tool
- `query_fleet`, `text_to_sql` with allowlisted views
- Code adapter: build Som-style structural wiki on commit
- `read_code_page`, `find_symbol` tools
- **Exit:** Context Builder can answer mixed queries ("show fleet state for tenants impacted by incident X")

### Phase 3 — PM/TPM wiki + Context Builder maturity
- Git-backed wiki store with frontmatter schema
- Confluence → wiki ingestion (LLM parser with PM/TPM extraction schema, when defined)
- Hybrid retrieval (BM25 + vector) only if the gold set shows recall gaps
- Context Builder reads shim index, classifies intent, parallelizes tools
- **Exit:** non-trivial multi-source queries return cited, faithful answers within budget

### Phase 4 — Permissions, FA semantic graph, roadmap, polish
- `persona_visibility` enforced at the retrieval layer (not just UI)
- FA semantic graph store (Dave's POC integrated)
- Jira roadmap approach (service-specific or otherwise) decided
- Ops: cost dashboards, retrieval latency SLOs, eval CI

Each phase ships its own slice through ingestion → store → retrieval → context builder → eval. No "build the whole framework first."

---

## 8. Open problems (need investigation before Phase 3)

These were explicitly called out as unsolved in the meetings. They are *the* research priorities.

### 8.1 LLM wiki storage and retrieval for remote agents

**The problem:** if PM/TPM/code wikis live as markdown in git for revision control, how do remote agents (Aira, Context Builder running outside the dev environment) read them efficiently? Vector DB is the wrong default for a curated wiki — discussed and ruled out.

**Candidates to evaluate:**
1. **Git-backed wiki + cached HTTP/MCP server.** Wikis live in git. A read-only service serves them by path with content-addressed caching. Agents call `read_wiki_page(path)` or `search_wiki(query)`.
2. **TOC + on-demand fetch.** Always inject TOC + page summaries into the agent prompt. Agent calls `read_wiki_page` for the few pages it actually needs. Cheap, debuggable, scales to ~1000s of pages.
3. **BM25 / Postgres FTS over the same git-backed corpus.** When precision-term retrieval matters (service names, error codes).
4. **Graph-of-wikis.** Treat each wiki page as a node, links as edges. Lightweight property graph (or Postgres with edge tables) over wiki metadata for "find related pages" queries.
5. **Hybrid (BM25 + vector + reranker).** Only if (1)+(2) prove insufficient on real eval data.

**Recommended starting point:** (1) + (2). Add (3) when the wiki crosses ~1000 pages or recall is measured-low. Avoid jumping to vector DB until justified by eval.

**Decision criteria:**
- Recall on the persona-specific eval set
- p95 latency for a single retrieval
- Cost per retrieval (tokens + infra)
- Operational complexity (one more service to run, vs. lib in the agent)

### 8.2 Code accessibility for remote agents

**The problem:** code (with Som's wiki layer) lives in source repos. How does a remote agent like Aira do useful work against it without dragging the whole codebase into the agent context?

**Candidates:**
1. **VM-spinup pattern (Rajeev's proposal):** agent spins up a VM, SCM-clones the repo, regenerates the code wiki via the skill, operates locally, opens a PR. Heavy but isolated and reproducible.
2. **Pre-built code wiki served centrally:** CI builds the code wiki on every commit, publishes to a read-only service. Agent fetches `read_code_page` and `find_symbol` remotely. Cheap reads but no write/PR ability without (1).
3. **Hybrid:** (2) for read-only / Q&A workloads; (1) only when the agent needs to actually modify code.

**Recommended starting point:** (3). Most queries are read-only and (2) is much cheaper. Reserve (1) for actual change workflows.

### 8.3 What to extract for TPM and product management content

**The problem:** unlike incidents (where extraction schema is well understood: root cause, logs, impact, resources), TPM/PM extraction is unsolved. Each individual currently does it ad-hoc.

**Out of scope for the framework itself.** The framework ships a parser interface and a schema-versioning system. The PM and TPM teams own defining their schemas. Provide them with:
- A schema template (`parsers/schemas/_template.json`)
- A "test your schema" CLI: run an LLM parser with the proposed schema across a sample of pages, eval the output
- A promotion path: schema v0 (draft) → v1 (in production with monitoring)

---

## 9. Out of scope (v1)

- A user-facing UI. The framework is plumbing; UIs are downstream.
- Per-persona LLM personas / prompt templates. Each agent (Aira, Codex-style assistants, internal portals) brings its own.
- Real-time collaborative editing of the wiki. Git PR workflow is good enough.
- A custom embedding model. Use a hosted/standard model. Pin its name + dimension at the store level.
- Cross-region replication. Single-region v1.

---

## 10. Cross-cutting concerns (build in from day one)

- **Citations:** every retriever returns source URLs/paths. Every synthesized answer carries them. No citation = bug.
- **Idempotent ingestion:** content-hash IDs. Re-running ingestion is a no-op when nothing changed.
- **Incremental updates:** webhooks (Confluence/Jira), git push triggers (code, wiki), scheduled snapshots (fleet rollups). Full re-index only on schema/model changes.
- **Versioning:** every chunk carries `source_sha`, `parser_version`, `schema_version`. Mismatched versions are surfaced to the eval system.
- **Cost telemetry:** log tokens-per-ingest and tokens-per-retrieve. Alarm on outliers.
- **Eval harness:** gold question sets per persona. Recall@k, faithfulness (LLM-judged), latency, cost. Runs in CI on parser/store changes.
- **ACL placeholders:** carry `persona_visibility` and `classification` on every ContentItem from v1, even if not enforced yet. Filter at retrieval is the v2 enforcement point.

---

## 11. Tech stack — recommendations (not prescriptions)

| Layer | Recommended | Why |
|---|---|---|
| Language | Python (ingestion, parsers, orchestrator); Go or Python for services | Python is best supported for LLM/embedding tooling; Go if perf matters for the MCP server |
| Vector store | pgvector (if Postgres available) → Qdrant if scale demands it | Boring/correct; metadata filtering is excellent; no extra infra |
| Graph | Oracle property graph (already in stack, per Harish) | Native vertex tables + edge indexes on relational data |
| Wiki storage | Git repo (markdown + YAML frontmatter) | Diff/PR/CI/blame for free; LLM-readable; no DB lock-in |
| Wiki serving | Lightweight FastAPI (or Go) service with content cache, exposed as MCP | Decouples wiki source from agent runtime |
| Fleet | Existing UDAP / Sentinel | No reason to move it |
| Code structural index | Som's LLM wiki skill, regenerated on commit | Aligns with existing direction; outperforms code embeddings for navigation |
| Orchestration | LangGraph or a thin custom layer over MCP | LangGraph for agentic flows; thin custom if the team prefers control |
| Eval | Custom harness + Ragas (or similar) for faithfulness | Eval discipline is non-negotiable; tooling choice is flexible |
| Reranker | Cohere Rerank or Voyage rerank-2 (managed) — only if hybrid retrieval is needed | Defer until measured |

---

## 12. Acceptance criteria for v1 (Phase 1 exit)

- [ ] A new operational incident from Jira flows through ingestion within 5 minutes
- [ ] `vector_search` over the incident KB returns top-5 with citations in <500ms p95
- [ ] Context Builder answers 80%+ of the incident gold set with grounded citations
- [ ] Re-ingesting the same Jira ticket changes zero rows in the store (idempotency)
- [ ] Eval harness runs on every PR; merge blocked on regression
- [ ] All ContentItems carry `persona_visibility` and `classification` (even if unused)
- [ ] Cost report: tokens per ingestion, tokens per retrieval, daily totals

---

## 13. Glossary

- **LLM Wiki** — hand- and AI-curated markdown corpus optimized for LLM consumption. Karpathy-style. Small, dense, high-signal.
- **Shim layer / Shim index** — a meta-wiki describing what other stores/wikis exist and what's in them. Loaded into the Context Builder's prompt so it knows where to look.
- **Context Builder** — the orchestration agent that picks retrieval tools based on a query and assembles a context packet.
- **Aira** — our remote agent that operates against the platform (incidents, code).
- **UDAP** — unified data store (fleet metadata, ops data).
- **Sentinel** — SQL-based fleet/ops query system.
- **ECAR** — internal compliance/risk doc.
- **P2T** — internal incident/issue category.
- **Blast radius** — set of services/tenants impacted (or potentially impacted) by an incident or change.
- **PG (Property Graph)** — Oracle's native graph storage on top of relational tables.
- **Codex** — OpenAI's coding agent (referenced in the meetings as a comparison point).

---

## 14. How to use this brief with Claude Code

1. Place this file at the repo root as `KNOWLEDGE_BUILDER_SPEC.md`.
2. Add a thin `CLAUDE.md` pointing to it: *"This repo implements the Knowledge Builder Framework. Read `KNOWLEDGE_BUILDER_SPEC.md` before making any structural changes. Sections 5 (Component map) and 6 (Interfaces) are load-bearing."*
3. Start Claude Code with: *"Implement Phase 1 (§7) end-to-end. Begin by scaffolding `core/interfaces.py` and `core/content.py` exactly as specified in §6.1–6.3, then write the incident extraction schema and the LLM parser test harness. Stop after the schema + 5 example incidents are passing the parser and ask for review before touching stores."*
4. For each subsequent phase, point to its exit criterion in §7 and let Claude Code drive.

This brief is intentionally opinionated where the meetings settled, and intentionally underspecified where they didn't (§8). Treat the open problems as research tasks, not implementation tasks — don't let Claude Code guess past them.
