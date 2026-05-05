---
id: DECISION-002
title: Converged Autonomous DB vs spec §2.1 "polyglot, not unified"
status: decided
created: 2026-05-04
decided: 2026-05-04
owner: tpm
tags: [storage, principles, phase-0]
related: [DECISION-001]
---

# DECISION-002 — Converged Autonomous DB vs spec §2.1

## Context
Spec §2.1 ("polyglot, not unified") is a load-bearing principle: different data types live in different stores. DECISION-001 chose Oracle 23ai Autonomous Database as a converged store (vector + SQL + graph + JSON in one engine). Reconciliation is needed.

## Options considered
- **Reject §2.1 explicitly**: deploy everything in one Autonomous DB; treat §2.1 as obsolete.
- **Strict §2.1**: separate Autonomous DB instances per data type.
- **Interpretive split (recommended)**: keep §2.1's *principle* — each data type owns its access pattern (VECTOR for incidents, SQL for fleet, PG/Cypher for FA semantic, JSON for wiki metadata) — while collapsing *physical deployment* into one converged DB. Each data type gets its own schema; the §6.3 `Store` contract treats each as a logical store.

## Decision
**Interpretive split.** The polyglot principle survives at the *access-pattern* and *Store interface* layer. Physical deployment is a single converged Autonomous Database. ADR-002 will formalize the schema layout and the mapping from logical `Store` instances to physical schemas.

## Why this works
- Spec §2.1's intent ("each data type uses the storage and retrieval shape that fits its access pattern") is preserved — we don't force vector data into SQL or fleet rows into a graph.
- pgvector itself (the spec's named alternative) collapses vector + SQL into one engine. Oracle 23ai is the same idea, scaled.
- Operational complexity drops (one DB to manage, one set of credentials, one backup target).
- We retain the option to split later if scale forces it.

## Implications
- ADR-002 specifies one schema per data type: `kb_incidents`, `kb_fleet_views`, `kb_fa_semantic`, `kb_wiki_metadata`. Wiki *content* stays in git.
- The §6.3 `Store` contract is unchanged. Concrete classes (`VectorStore`, `GraphStore`, etc.) point at different schemas of the same DB.
- Cost telemetry must report per-data-type usage so a future split is data-driven.

## Revisit conditions
- If any single data type's read or write traffic crosses the Autonomous DB's tier ceiling without horizontal scaling options, file a new DECISION to split.
- If compliance or data-residency requires per-data-type isolation, file a new DECISION.
