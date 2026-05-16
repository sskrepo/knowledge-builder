---
title: DECISION-012 — Runtime Ingestion Option for Ask-Parameterized Skills
status: resolved
created: 2026-05-16
resolved: 2026-05-16
owner: architect
deciders: user
tags: [decision, workflow-skills, ingestion, consumption]
related: [ADR-032, ADR-016, ADR-029]
---

# DECISION-012 — Runtime Ingestion Option for Ask-Parameterized Skills

## Status

**RESOLVED — 2026-05-16. User chose Option C (ephemeral request-scoped ingestion).**

---

## Decision

**Option C — Request-Scoped Ephemeral Ingestion** is the accepted approach for
how ask-parameterized workflow skills obtain a user-supplied Confluence page at
consumption time.

Key terms of the decision as accepted:

- Content is **never persisted** to the shared KB / WikiMetadataStore. The
  ephemeral fetch is for the duration of a single request only.
- An **in-process TTL cache** of approximately 300 seconds (keyed by
  `page_id + content_hash`) prevents redundant fetches within a session window.
  The cache is request-process-scoped, thread-safe, and never written to disk.
- **Trust model:** the author-time grant. `ingest_on_demand: true` in a skill's
  `source_binding` block is the author's explicit declaration that "this skill
  may issue a live Confluence HTTP call on behalf of a consumer-supplied page
  reference." The consuming identity's authorization scope is governed by the
  `space_allow_list` declared in the skill's `source_binding` block, not by
  per-consumer Confluence ACLs.
- **Per-consumer OAuth (full Confluence ACL enforcement)** is explicitly v2 /
  out-of-scope for this build. The architecture for it exists (ADR-020
  codex_proxy / emcp_direct) but is deferred until the user decides to require
  it. The space allow-list + rate limiter + audit log are the v1 mitigations.
- **LLM extraction inside a retrieval request** is acceptable ONLY for
  `ask_parameterized` skills whose schema was authored, reviewed, and promoted
  by a persona team through the full `authorSkill` flow. Ephemeral extraction is
  schema-bounded against that authored schema — it is not unconstrained autonomous
  LLM extraction. This is the accepted caveat under spec §2 principle 2
  ("LLM-in-ingestion != LLM-in-retrieval"): the extraction is schema-bounded
  and the schema is author-time reviewed, making it materially different from
  ad-hoc LLM extraction.
- Extraction runs the **existing `_llm_extract_fields` method** with the skill's
  authored schema. No new extraction code path is introduced; the ephemeral
  path is a wrapper that substitutes the source text and skips the KB write.
- **Adapter availability gate:** a skill with `source_binding.mode:
  ask_parameterized` and `ingest_on_demand: true` MUST NOT be promoted to a
  deployment that has no Confluence adapter configured. The VALIDATE state
  (ADR-016 lifecycle) enforces this check before promotion.

---

## Rationale

Option C was chosen because it is the only option that simultaneously:

1. Delivers the correct content without consumer retry (unlike Option B).
2. Does not permanently pollute the shared KB with one-off consumer-specific
   pages (unlike Option A).
3. Does not require a new IPC channel between the MCP server and the ingestion
   worker (unlike Option B).
4. Makes the trust boundary explicit via the author-time grant model (better
   than Option A's fully implicit grant).

Options A and B remain documented in ADR-032 as alternatives considered.

---

## Spec §2 Caveat (accepted on record)

The user has accepted the following architectural caveat:

> Schema-bounded LLM extraction inside a retrieval request is acceptable ONLY
> for `ask_parameterized` skills whose schema was authored, reviewed, and
> promoted through the `authorSkill` flow. The skill author's act of promoting
> the skill with `ingest_on_demand: true` is the explicit architectural grant
> that this LLM extraction step may occur at retrieval time. This is categorically
> different from unconstrained autonomous LLM extraction: the schema is fixed
> and authored before the first consumer ever supplies a page.

This caveat is recorded in ADR-032 §F and in the implementation blueprint.

---

## Implementation

P3 (silent wrong-page substitution guard) shipped standalone in commit 8c947dc
before this decision was reached, using a regex heuristic on the input string.
That heuristic is explicitly temporary; it will be replaced by the schema field
`source_binding.input_param` when P1 ships, as described in ADR-032 §C.

P1 (author-time source-binding contract) and P2 (Option C ephemeral runtime
ingestion) will be implemented per the implementation blueprint at:
`docs/wiki/adr/ADR-032-impl-plan.md`

---

## References

- [ADR-032 — Ask-time source ingestion (Accepted)](../../docs/wiki/adr/ADR-032-ask-time-source-ingestion.md)
- [ADR-032 — Implementation Blueprint](../../docs/wiki/adr/ADR-032-impl-plan.md)
- [ADR-016 — Workflow skills schema](../../docs/wiki/adr/ADR-016-workflow-skills.md)
- Commit 8c947dc — P3 standalone guard (ConfluencePageNotInKBError + 19 tests)
