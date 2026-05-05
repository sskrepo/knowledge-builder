---
id: DECISION-003
title: LLM + embeddings provider for ingestion and synthesis
status: decided
created: 2026-05-04
decided: 2026-05-04
owner: tpm
tags: [llm, embeddings, phase-0]
related: [DECISION-001]
---

# DECISION-003 — LLM + embeddings provider

## Context
The framework uses LLMs in two places (spec §2.2):
1. **Ingestion** — summarization, entity extraction, relationship inference for contextual content (incidents, design docs).
2. **Retrieval (final synthesis only)** — Context Builder assembles a context packet and synthesizes a cited answer.

Plus embeddings for vector search (§4.1, §6.4).

## Options considered
- **OCI Generative AI** (Cohere Command-R, Llama-family, embed-multilingual-v3) — tightest data locality.
- **OpenAI** (gpt-4o family + text-embedding-3-large) — Oracle-certified per user; strongest general capability.
- **Anthropic Claude** — strongest at long-context synthesis; no first-party Oracle integration named.
- **Mixed** (e.g., OCI for embeddings, OpenAI for synthesis).

## Decision
**OpenAI for both LLM and embeddings.** Justified by (a) Oracle certification covers it, so compliance posture is intact, (b) maturity of tooling (Ragas defaults work, LangGraph integration is first-class), (c) avoiding split-brain debugging across two providers in v1.

Specific defaults (Architect to confirm in ADR-001):
- Ingestion parser LLM: gpt-4o (or successor)
- Synthesis LLM: gpt-4o
- Embeddings: text-embedding-3-large (3072 dims) — pinned at the store level per spec §11
- Eval judge LLM: gpt-4o (Ragas default)

## Implications
- Embeddings dimension (3072) is part of `Store` schema. Changing the embedding model requires a re-index — versioned via `parser_version` / `schema_version` (spec §10).
- All API calls go through OCI Vault-managed keys; never hard-coded.
- Cost telemetry (spec §10) tracks tokens per provider; if costs spike, file a new DECISION to evaluate switching ingestion to OCI Gen AI (cheaper Cohere models) and reserve OpenAI for synthesis.

## Revisit conditions
- Phase 1 cost report shows per-retrieval cost > target.
- Compliance change forbids data egress to OpenAI (would force migration to OCI Gen AI).
- A specific use case demands long-context (>200k tokens) where Anthropic outperforms.
