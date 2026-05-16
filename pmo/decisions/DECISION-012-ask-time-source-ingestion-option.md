---
title: DECISION-012 — Runtime Ingestion Option for Ask-Parameterized Skills
status: open
created: 2026-05-16
owner: architect
deciders: user
tags: [decision, workflow-skills, ingestion, consumption]
related: [ADR-032, ADR-016, ADR-029]
---

# DECISION-012 — Runtime Ingestion Option for Ask-Parameterized Skills

## Status

**Open — awaiting user decision.**

---

## Context

ADR-032 (Proposed) documents a production failure where a TPM email-draft skill
silently drew from the wrong Confluence page when the consumer-supplied page was not
in the KB. The ADR defines three root causes (P1: design-contract gap, P2:
runtime-capability gap, P3: silent wrong-page substitution) and proposes three
implementation options for the runtime-ingestion mechanism.

P3 (silent substitution) will be fixed immediately as a standalone change, regardless
of this decision. This decision governs P1 + P2: how ask-parameterized skills
fetch the user-supplied source at consumption time.

---

## Options

### Option A — Synchronous Ingest-on-Demand Inside the Ask Path

When a skill is marked `source_binding.mode: ask_parameterized` and the requested
page is not in the KB, the WorkflowExecutor calls `ConfluenceWikiIngestor.ingest_page()`
synchronously inside the request handler, persists the content to the shared KB, then
retrieves from it.

**Pros:** seamless consumer UX; no retry; content is fresh.
**Cons:** adds 5-30s latency; the shared KB accumulates one-off pages that no other skill
uses; trust boundary is fully implicit (any consumer can trigger ingestion of any page
the service credential can reach); fights the spec §2 ingest/retrieve separation.
**Effort:** 3-5 days.

### Option B — Ask Triggers Ingestion Pipeline; Hard-Fail with Retry Instruction

When the requested page is not in the KB, the executor enqueues a background ingestion
job and returns a hard-fail with "retry in 30-60 seconds." The ingestion pipeline runs
asynchronously.

**Pros:** architectural separation preserved; KB stays curated (only async-ingested pages).
**Cons:** poor consumer UX (must retry manually); requires new IPC channel between MCP
server and ingestion worker (Redis queue or HTTP); trust boundary is deferred, not solved.
**Effort:** 5-8 days.

### Option C — Request-Scoped Ephemeral Ingestion (Architect's Recommendation)

When the requested page is not in the KB, the executor fetches the page content from
Confluence via the existing adapter, runs LLM extraction using the skill's authored
schema, uses the content for synthesis, and discards it — the content is never persisted
to the shared KB. A short in-process TTL cache (default 300s) prevents redundant
fetches within a session.

**Pros:** seamless consumer UX; no retry; no shared KB pollution; citations are real
Confluence URLs; adapter wiring is a focused non-breaking change; trust is an author-time
grant (the author who promotes the skill grants the `ingest_on_demand` permission).
**Cons:** adds 2-15s latency on cache miss (must be disclosed in API response); does not
build up a persistent KB for future use; requires Confluence adapter in the MCP server
lifespan; trust boundary still present but governed by author-time grant and space
allow-list.
**Effort:** 3-5 days.

---

## Architect's Recommendation

**Option C.** It is the only option that:
- Delivers the correct content without consumer retry.
- Does not pollute the shared KB with one-off pages.
- Keeps the trust boundary explicit (author-time grant, space allow-list, rate limiting).
- Is architecturally consistent with the spec: ephemeral schema-bounded extraction at
  ask time is not the same as unconstrained autonomous LLM extraction.

Option A fails primarily on the trust boundary (implicit, fully open) and secondarily
on spec §2 conflict. Option B fails on consumer UX and operational complexity.

---

## What needs to be decided

Reply with: **DECISION-012: option A**, **option B**, or **option C**.

If you have concerns about the trust boundary in Option C, specify which additional
mitigations you want alongside it:
- Space allow-list enforcement (restrict ephemeral fetch to spaces declared in skill)
- Per-consumer OAuth (use the consumer's Confluence credentials, not the service token)
- Disable feature entirely for skills deployed to production until OAuth is available

---

## Impact

| If you choose | What happens next |
|---|---|
| Option A | ADR-032 updated to Accepted. Backend Dev implements sync ingest-on-demand in WorkflowExecutor. Trust boundary mitigation is a separate story. |
| Option B | ADR-032 updated to Accepted. Dev team designs IPC channel + ingestion queue before implementation begins. Longer lead time. |
| Option C | ADR-032 updated to Accepted. Backend Dev wires Confluence adapter into mcp_server.py lifespan (conditional on any promoted skill declaring ask_parameterized). WorkflowExecutor gains ephemeral fetch path. |
| No decision yet | P3 hard-fail fix ships immediately (it is independent). P1+P2 deferred. New TPM email skills cannot use ask-parameterized mode. |

---

## References

- [ADR-032 — Ask-time source ingestion (Proposed)](../../docs/wiki/adr/ADR-032-ask-time-source-ingestion.md)
- [ADR-016 — Workflow skills](../../docs/wiki/adr/ADR-016-workflow-skills.md)
