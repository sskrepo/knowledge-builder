---
id: DECISION-001
title: Oracle tech-stack commitment level
status: decided
created: 2026-05-04
decided: 2026-05-04
owner: tpm
tags: [tech-stack, oracle, phase-0]
---

# DECISION-001 — Oracle tech-stack commitment level

## Context
Spec §11 lists technology recommendations (pgvector, Oracle Property Graph, Git wikis, FastAPI, etc.) that are "recommendations, not prescriptions." The user is at an Oracle-shop and explicitly requested Oracle technologies where applicable.

## Options considered
- **A — Oracle-where-it-replaces-cleanly**: pgvector → Oracle 23ai Vector Search; Oracle Property Graph; FastAPI on OCI Compute. Everything else generic.
- **B — Converged DB + OCI runtime**: Oracle Autonomous Database for vector + SQL + graph + JSON; OCI Object Storage; OCI Vault.
- **C — Full Oracle stack**: B plus OCI Generative AI for embeddings/LLM, OCI Streaming for events, Oracle Coherence cache, OCI Functions for ingestion workers, OCI Gen AI Agents for orchestration.

## Decision
**C with two carve-outs**:
1. **LLM + Embeddings → OpenAI** (Oracle-certified per user). See DECISION-003.
2. **Orchestration → LangGraph on OCI** (not OCI Gen AI Agents). User preference for control + flexibility.

## Resolved stack
| Layer | Choice |
|---|---|
| Language | Python |
| Converged data store | Oracle 23ai Autonomous Database |
| Vector | Oracle 23ai AI Vector Search (in Autonomous DB) |
| Graph | Oracle Property Graph (in Autonomous DB) |
| Relational / fleet | Existing UDAP / Sentinel (read-through) |
| Wiki storage | Git (markdown + frontmatter) |
| Wiki serving | FastAPI on OCI Compute / Container Instances |
| Embeddings + LLM | OpenAI (Oracle-certified) |
| Orchestration | LangGraph on OCI |
| Eval | Custom recall/latency/cost runner + Ragas for faithfulness |
| Reranker (deferred) | Cohere / Voyage |
| Object storage | OCI Object Storage |
| Secrets | OCI Vault |
| Change events | OCI Streaming |
| Cache | Oracle Coherence (deferred to v2) |
| Ingestion workers | OCI Functions |

## Implications
- Architect's ADR-001 codifies this baseline.
- ADR-002 handles the implication on §2.1 (polyglot principle) — see DECISION-002.
- ADR-005 confirms Ragas + custom runner.
- All persona-builder configs (per persona-knowledge-builder.md) reference these stores.
