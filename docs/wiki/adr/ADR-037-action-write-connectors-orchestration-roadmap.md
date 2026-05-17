---
title: ADR-037 — Action/Write Connectors & Multi-hop Orchestration (ROADMAP)
status: proposed-roadmap
created: 2026-05-17
owner: architect
deciders: user (phases require explicit approval before implementation)
tags: [adr, roadmap, write-connectors, orchestration, open-problem, spec-s8]
related: [ADR-036, ADR-035, DECISION-016, DECISION-015]
supersedes: ~
---

# ADR-037 — Action/Write Connectors & Multi-hop Orchestration (ROADMAP — NOT BUILT)

## Status

**Proposed / Roadmap — 2026-05-17.**

THIS ADR DESCRIBES FUTURE WORK. NO IMPLEMENTATION IS APPROVED OR IN PROGRESS.

This document is a deliberate, sequenced research track filed under spec §8 (open
problems). Each phase below requires an explicit, separate user approval before any
implementation begins. Filing this ADR now ensures the driving scenario is documented
and sequenced rather than forgotten or re-litigated. It does NOT commit any agent or
developer to implementation.

See DECISION-016 for why this is a separate track from the read-only Connector Registry
(ADR-036).

---

## A. Driving Scenario

The following scenario was given verbatim by the user as the motivation for this
roadmap:

> Read a Jira issue → extract an OCID → query Lumberjack logs by that timestamp
> window → write the resulting RCA back into a database and/or Jira.

This scenario requires capabilities the framework does not currently have:

1. **Write-capable connectors** — the framework's current adapter contract (spec §5/§6)
   is explicitly read-only. Writing an RCA back to Jira or a database is a write action.

2. **Cross-connector data flow** — the OCID extracted from the Jira issue must be
   passed as a parameter to the Lumberjack query. This is a connector-graph pattern:
   one connector's output becomes another connector's input. The framework has no
   data-flow contract between connectors.

3. **Orchestration/execution runtime** — the sequence (read Jira → query Lumberjack →
   write RCA) must be orchestrated: step ordering, error handling, retry/rollback,
   partial-failure semantics. The framework has no execution runtime for multi-step
   connector workflows.

4. **Authorization and blast-radius control** — write actions must be gated by explicit
   permission grants. A misconfigured or compromised write connector can cause
   irreversible damage (overwrite production Jira tickets, corrupt a database). The
   framework has no authz model for action connectors.

---

## B. Why This Is a Separate Track

### B.1 Write is materially riskier than read

Read connectors fail safe: an adapter that cannot reach Confluence returns an error
and the skill produces no content. Write connectors fail unsafely: an adapter that
sends a partial or malformed write to Jira may corrupt live data before it fails.

The risk profile differences:

| Property | Read connector | Write/action connector |
|---|---|---|
| Failure mode | Returns no data (safe) | May corrupt data (unsafe) |
| Blast radius | Zero (read-only) | Bounded to target system |
| Irreversibility | N/A | High (overwrites, posts) |
| Authz requirement | Credential to read | Permission grant per action type |
| Rollback | N/A | Required for production targets |

### B.2 The system safety model is not yet designed

The framework's current safety model (spec §2) covers deterministic extraction and
capability-honesty for read sources. It does not specify:
- A prohibited-action list for write connectors.
- An explicit-permission model (which skill authors can grant which write operations
  to which connectors).
- Guardrails against LLM-driven write actions (the LLM must not autonomously decide
  to write to production systems).
- Audit logging for write operations.

Building write connectors before this safety model is designed would be a safety debt.

### B.3 The connector graph and execution runtime are undesigned infrastructure

A multi-hop workflow (Jira → Lumberjack → write) requires:
- A data-flow contract: how connector outputs are typed and passed as inputs.
- An execution plan: the DAG of connector calls, with dependency edges.
- An execution runtime: step sequencing, error propagation, partial-failure semantics.

None of these exist in the framework. Building them is not an extension of the existing
adapter model — it is a new infrastructure layer.

### B.4 Spec §8 classification

This scenario maps to spec §8 (open problems — research tracks). The spec is explicit:
open problems are "the research priorities" that need investigation before implementation.
Guessing past them produces the wrong infrastructure. This ADR is filed in that spirit.

---

## C. Phased Roadmap

Each phase is a discrete, approachable unit of work. No phase starts without explicit
approval. Phases are sequenced — Phase 2 requires Phase 1; Phase 3 requires Phase 2.

### Phase 1 — Write-Capable Manifest Extensions + Permission/Authz Model + Guardrails

**Goals**

- Extend the Connector Registry manifest schema (ADR-036) to support write/action
  operations: add `"write"`, `"create"`, `"update"`, `"delete"` to
  `supported_operations`. Each write operation carries additional fields:
  `requires_permission_grant` (bool), `blast_radius_classification`
  (`low | medium | high`), `rollback_supported` (bool).
- Design the permission/authz model: which personas/roles can grant which write
  operations to which connectors. Minimum: an explicit opt-in requirement (no write
  operation is allowed by default; a named human must grant it per connector per skill).
- Define guardrails: LLM-generated content MUST NOT be submitted to a write connector
  without a deterministic validation step. Specify what validation is required per
  operation type (schema check, diff preview, human-in-the-loop confirmation threshold).
- Audit logging: every write operation is logged with: connector_id, operation, target
  reference, actor (skill_id + author_id), timestamp, outcome.

**Open questions (must be resolved before Phase 1 begins)**

- Who grants write permissions? Skill author? Framework admin? Per-run approval?
- What is the minimum confirmation UX for a write action? Silent (just log)?
  Preview-and-confirm? Always human-in-the-loop?
- How does rollback work for connectors that do not support it natively
  (e.g. a database INSERT with no transaction)?
- Does the permission model live in the framework config, in the skill definition,
  or in an external IAM/RBAC system?

**Spec amendments implied**

- §5 component map: adapters section gains write-capable subtype.
- §6.2 parser contract: write operations do not produce `ContentItem`s; new contract
  needed for write results (e.g. `WriteResult(connector_id, target_ref, status, audit_ref)`).
- §6.3 store contract: write connectors are NOT stores; they are action targets.
  A new `ActionConnector` protocol is implied, distinct from `Store`.

**Risks**

- Permission model design may require external stakeholder input (security, platform
  owners of target systems).
- Blast-radius classification requires per-connector expert review (not an LLM task).
- Guardrail design for LLM-generated write payloads is a novel safety engineering
  problem with no prior art in this codebase.

---

### Phase 2 — Connector Graph + Data-Flow Contracts

**Goals**

- Define a connector-graph model: a DAG where nodes are connector invocations and
  edges carry typed data (the output of one connector step is the input of the next).
- Specify the data-flow contract: how connector outputs are typed (schema), how
  field mapping between connectors is declared (e.g. Jira issue `custom_field_ocid`
  → Lumberjack query parameter `ocid`), and how type mismatches are surfaced at
  authoring time (not runtime).
- Extend CONFIGURE_SOURCES to validate the declared connector graph: all connector
  types registered, all edge field mappings type-compatible, all required permissions
  granted for write connectors in the graph.
- Handle branching and conditional edges: e.g. "if the Jira issue has no OCID field,
  skip the Lumberjack step."

**Open questions (must be resolved before Phase 2 begins)**

- What is the schema language for connector output types? JSON Schema? A custom DSL?
  How are optional vs. required fields handled in cross-connector mappings?
- How deep can connector graphs be? Is there a maximum depth enforced by the framework?
- What happens when a graph has a cycle (should be rejected; how is it detected)?
- How does the graph interact with the eval harness (spec §6)? Can gold-set queries
  exercise multi-hop paths?

**Spec amendments implied**

- §5: new component `connector_graph/` in the component map — graph definition store,
  graph validator, field-mapping engine.
- §6: new interface section for connector graph (analogous to §6.2–§6.4 for existing
  components).

**Risks**

- Data-flow contracts between connectors are a new abstraction layer with no prior
  implementation reference in the codebase. Complexity risk is high.
- Type-compatibility validation at authoring time may require per-connector output
  schemas that are costly to maintain as connectors evolve.

---

### Phase 3 — Orchestration/Execution Runtime

**Goals**

- Build the execution runtime that interprets and executes a validated connector graph:
  step sequencing per DAG topology, parallel execution where edges allow, error
  propagation (fail-fast or continue-on-error per edge policy), partial-failure
  semantics (which prior steps' writes to roll back or flag on downstream failure).
- Define execution state: a persistent record of in-progress and completed runs, with
  enough state to support resumption and audit replay.
- Integrate with the background job queue (spec §2, framework ethos): long-running
  multi-hop workflows run as background jobs (BullMQ/Redis equivalent), not in
  request-handler async.
- Observability: every step emits a structured event (step_id, connector_id, operation,
  input_hash, output_hash, latency_ms, cost_tokens). Execution traces are queryable.

**Open questions (must be resolved before Phase 3 begins)**

- What execution runtime technology? LangGraph (spec §11 option for orchestration),
  a thin custom layer, or something else? This is a team-preference decision that
  requires an ADR at Phase 3 design time.
- What are the failure semantics for a graph where Step 3 (write) fails after
  Step 2 (read + extract) has already run? Is the read result cached? Is the
  partial state surfaced to the skill author?
- How does the execution runtime interact with the skill's cost telemetry requirement
  (spec §10)? Multi-hop runs may span multiple connectors with different token costs.
- What is the latency SLO for a multi-hop execution? Is it user-facing (synchronous)
  or always async?

**Spec amendments implied**

- §5: `orchestrator/` component (already named in spec §5 component map) gains concrete
  responsibility: execution runtime for connector-graph workflows. Spec §5 currently
  defers orchestration to Phase 3 — this aligns.
- §6: new §6.X for execution runtime interface (ExecutionPlan, ExecutionState,
  ExecutionResult).
- §7: Phase 3 exit criterion updated to include multi-hop execution capability.

**Risks**

- Execution runtime is the most complex component in this roadmap. Building it before
  Phase 1 (authz) and Phase 2 (graph contracts) are solid will produce an unsafe,
  untyped runtime.
- Integration with background job queue may require a different technology choice than
  current framework stack (Python + BullMQ is a cross-language bridge).
- Observability for multi-step executions is substantially more complex than single
  retrieval tracing.

---

## D. Relationship to Spec §8 Open Problems

This entire ADR is filed as a spec §8 open-problem research track. The spec states
(§8): these are "the research priorities" that need investigation before implementation.

The three phases above map to three distinct research questions:

| Phase | Research question |
|---|---|
| Phase 1 | What is the minimum authz + guardrail model that makes write connectors safe enough to ship? |
| Phase 2 | What data-flow contract and graph model is expressive enough for the driving scenario without becoming a general-purpose workflow engine? |
| Phase 3 | What execution runtime is operationally tractable given the framework's technology constraints and the team's expertise? |

None of these questions have obvious answers today. Guessing past them produces
incorrect infrastructure. Each phase begins with a focused investigation spike, not a
feature sprint.

---

## E. Consequences

### When ADR-037 Phase 1 is approved and begins

- ADR-036 manifest schema is extended (write operation fields). This is a backward-
  compatible extension — existing read-only manifests remain valid.
- DECISION-016 is amended to reflect Phase 1 approval.
- A new ADR is filed for the specific authz/permission model design (Phase 1 is not
  a single ADR — it is multiple design decisions).

### When ADR-037 Phase 2 is approved and begins

- Spec §5 component map is amended to add `connector_graph/`.
- Spec §6 gains a new interface section.
- A new ADR is filed for the connector-graph and field-mapping contract.

### When ADR-037 Phase 3 is approved and begins

- Spec §5 `orchestrator/` component is concretized.
- Technology choice for execution runtime is made in a new ADR.

### Until any phase is approved

- No write connector code is written.
- No connector-graph infrastructure is built.
- No orchestration runtime is designed.
- The read-only Connector Registry (ADR-036) is the complete and correct connector
  model for the framework.

---

## F. Non-Goals for This Document

- This ADR does NOT specify implementation details for any phase. Those come from
  phase-specific ADRs filed when phases are approved.
- This ADR does NOT approve any implementation. It documents the roadmap only.
- This ADR does NOT design the authz model (Phase 1 does).
- This ADR does NOT select the execution runtime technology (Phase 3 does).
- This ADR does NOT modify the current spec §5/§6 read-only adapter contract.
  That contract is owned by ADR-036 for this phase.

---

## G. Related ADRs

- **ADR-036** — Connector Registry (read-only). The foundation this roadmap builds on.
  Phase 1 extends ADR-036's manifest schema; Phase 2 adds graph validation to
  ADR-036's authoring gate; Phase 3 adds the execution runtime.
- **ADR-035** — CONFIGURE/INSPECT instance access-verify (concurrent). ADR-037's
  phases inherit the access-verify pattern and must extend it for write-operation
  instance verification (e.g. verify write permission to the specific Jira project,
  not just connectivity).
- **DECISION-016** — Scope decision: registry now + phased roadmap. ADR-037 is the
  roadmap artifact that decision called for.
- **DECISION-015** — Principle: CONFIGURE/INSPECT gate access-verify.

---

## H. References

- spec §5 (component map — adapter layer, orchestrator component)
- spec §6.2 (parser contract — `RawItem`, `ContentItem`)
- spec §6.5 (Context Builder — orchestration reference point)
- spec §7 (phased build plan — Phase 3 orchestration)
- spec §8 (open problems — research track discipline)
- spec §10 (cross-cutting: citations, idempotency, versioning, cost telemetry)
- DECISION-016 — connector model scope decision
- ADR-036 — Connector Registry (read) — foundation for this roadmap
- ADR-035 — CONFIGURE/INSPECT instance access-verify (concurrent)
