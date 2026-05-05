---
title: ADR-001 — Tech-stack baseline
status: accepted
created: 2026-05-04
owner: architect
tags: [adr, tech-stack, phase-0]
supersedes: []
related: [DECISION-001, DECISION-003]
---

# ADR-001 — Tech-stack baseline

## Status
Accepted (2026-05-04). Source decision: [DECISION-001](../../../pmo/decisions/DECISION-001-oracle-tech-stack.md).

## Context
The framework spec (§11) presented technology recommendations as non-prescriptive. The user is at an Oracle-shop and has explicitly chosen the full Oracle stack with two carve-outs (OpenAI for LLM/embeddings, LangGraph for orchestration). This ADR records the baseline so every downstream design defers to it.

## Decision
The baseline tech stack for the framework is:

### Compute & language
- **Language**: Python 3.12+ for ingestion, parsers, retrievers, orchestrator. Type hints required (`mypy --strict` on `framework/core/`).
- **Runtime hosts**:
  - Long-running services (FastAPI MCP server, scheduler, eval harness API): **OCI Compute** (Container Instances acceptable for v1).
  - Event-driven workers (per-source ingestion handlers): **OCI Functions**.
  - Local development: macOS / Linux with Docker.

### Data plane
- **Converged store**: **Oracle 23ai Autonomous Database** — one instance hosting all framework-managed schemas. See [ADR-002](ADR-002-storage-shape.md) for schema layout and the §6.3 `Store` mapping.
- **Vector**: Oracle 23ai AI Vector Search inside Autonomous DB. Default index: HNSW. Default similarity: cosine.
- **Graph**: Oracle Property Graph inside Autonomous DB. Access via PG/Cypher.
- **JSON**: Native JSON columns in Autonomous DB for wiki metadata, ContentItem `metadata` field, persona-builder config snapshots.
- **Fleet (read-through, not ingested)**: existing **UDAP / Sentinel**. Wrapped via the `query_fleet` and `text_to_sql` MCP tools (spec §4.2). No data movement.
- **Wiki content**: Git repository (`kb-wiki/`) of markdown + YAML frontmatter. Branch protection on `main`. PRs reviewed by the persona team that owns the directory.
- **Object storage**: **OCI Object Storage** — raw source dumps (Confluence HTML snapshots, Jira ticket JSON), parser audit artifacts, model snapshots, eval run artifacts.
- **Secrets**: **OCI Vault** — every credential (Confluence token, Jira token, OpenAI key, DB password) lives here. No secret in env files committed to git.
- **Change events**: **OCI Streaming** (Kafka-compatible) for ingestion fan-out and webhook normalization.

### LLM plane
- **LLM provider**: **OpenAI** (Oracle-certified). See [DECISION-003](../../../pmo/decisions/DECISION-003-llm-provider.md).
  - Ingestion parser model: `gpt-4o` (or successor when validated).
  - Synthesis model (Context Builder): `gpt-4o`.
  - Eval judge model: `gpt-4o`.
- **Embeddings**: OpenAI `text-embedding-3-large`. Vector dim: **3072**, pinned at the schema level. Re-embedding requires `schema_version` bump and a backfill plan.
- **Reranker** (deferred until measured-need): Cohere Rerank or Voyage rerank-2.
- **Provider abstraction**: A thin `LLMClient` shim in `framework/core/llm.py` that swaps OpenAI for OCI Generative AI without touching call sites. Required because cost or compliance may force a swap later (see DECISION-003 revisit conditions).

### Orchestration plane
- **Orchestration**: **LangGraph on OCI Compute**. Hosts the Context Builder graph (intent classification → tool selection → parallel retrieval → rerank → synthesis).
- **MCP server**: FastAPI app exposing the §6.4 tool surface. Hosted on OCI Compute / Container Instances. Stateless; horizontal scale by load balancer.
- **MCP transport**: HTTP/SSE per the MCP spec.

### Eval plane
- **Eval harness**: Custom Python runner for **recall@k**, **latency p50/p95**, **token/cost telemetry**. Plus **Ragas** for faithfulness, answer relevancy, context precision/recall (LLM-as-judge via the same OpenAI deployment).
- **Storage**: gold sets in `eval/gold_sets/{persona}.jsonl` (committed). Run artifacts in OCI Object Storage (large blobs not in git).
- **CI**: every PR that touches `framework/parsers/`, `framework/stores/`, `framework/retrievers/`, or `framework/orchestrator/` runs the eval harness; merge blocked on regression beyond a configured tolerance. See [ADR-005](ADR-005-eval-harness.md).

### Tooling
- **Package manager**: `uv` or `poetry` (Dev Manager to choose in Phase 0).
- **Lint/format**: `ruff` + `black`.
- **Type-check**: `mypy --strict` on `framework/core/` and `framework/parsers/` schemas.
- **Tests**: `pytest`. Integration tests use a dedicated Autonomous DB schema seeded by fixtures.
- **CI**: GitHub Actions or OCI DevOps (Dev Manager to choose).

## Considered alternatives
- **pgvector + Postgres**: simpler, OSS, but does not match the user's Oracle commitment. Rejected per DECISION-001.
- **Qdrant** for vector: superior at scale, but adds a separate store outside the converged DB. Rejected for v1.
- **OCI Generative AI** for LLM: data-locality win, but Oracle certification of OpenAI removes that benefit and OpenAI's tooling maturity is higher. Rejected per DECISION-003 (revisit if cost demands).
- **OCI Gen AI Agents** for orchestration: viable but newer; LangGraph offers more control and better debuggability for v1. Rejected per DECISION-001.

## Consequences
**Positive**
- One DB to operate. Backups, IAM, networking are unified.
- Provider choices are first-class in OCI Vault → secret rotation is straightforward.
- LangGraph + OpenAI tooling is mature; lowest dev friction.

**Negative / risks**
- Vendor lock-in to Oracle Cloud is now structural. Mitigated by keeping framework code provider-agnostic at the `Store` and `LLMClient` interface seams (see ADR-003).
- Single converged DB is a single point of failure. Mitigated by Autonomous DB's HA defaults (Data Guard) and eval-driven monitoring.
- OpenAI is an external dependency. Mitigated by `LLMClient` shim and explicit DECISION-003 revisit conditions.

## Compliance with spec principles
- §2.1 *polyglot, not unified*: preserved at access-pattern + interface layer; physical deployment converged. See [ADR-002](ADR-002-storage-shape.md) and DECISION-002.
- §2.2 *LLM-in-ingestion ≠ LLM-in-retrieval*: enforced by separate code paths (`framework/parsers/llm_parser.py` for ingestion; `framework/orchestrator/synthesizer.py` for retrieval-time synthesis only).
- §2.4 *storage is consequence of retrieval pattern*: each data type chooses its index/schema in ADR-002.
- §10 *cross-cutting*: citations, idempotency, versioning, cost telemetry, eval, ACL placeholders — all enumerated in subsequent ADRs.
