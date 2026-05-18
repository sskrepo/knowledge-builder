# DECISION-016: Connector Model Scope — Read-Only Registry Now, Write/Orchestration Phased

**Status**: DECIDED
**Date**: 2026-05-17
**Decided by**: User (directive given in connector-model scoping session)
**Relates to**: spec §5 (component map), §6 (interfaces), §8 (open problems)
**Amends**: none — additive scope clarification to spec §5/§6 adapter contract
**Implemented in**: ADR-036 (connector registry, read), ADR-037 (write/orchestration roadmap)
**Concurrent context**: DECISION-015 / ADR-035 (CONFIGURE/INSPECT gate access-verify) — see seam note below

---

## Context

The framework's `framework/adapters/` layer has grown as ad-hoc one-off integrations:
`confluence/`, `jira/`, `git_adapter.py`, `udap_adapter.py`, `code_wiki_builder.py`,
`_base.py`. There is no unified connector registry. The `source_type` field in
`_SessionData` is a loosely-validated free string with a single non-empty check at
`framework/skill_builder/conversation.py:178-182`; there is no enforcement against a
known, declared set of supported connectors.

This means the authoring gate (CONFIGURE_SOURCES / INSPECT_SOURCES) cannot honestly
reject an unsupported connector (e.g. a user naming "Lumberjack" as a source). The
system silently accepts any `source_type` string, proceeds through skill design, and
only fails at runtime — a capability-dishonesty failure the system ethos explicitly
prohibits (fail-fast, no silent degradation).

Separately, a driving use-case surfaced by the user illustrates a future need that goes
materially beyond read ingestion: read a Jira issue, extract an OCID, query Lumberjack
logs by that timestamp window, write the resulting RCA back into a database and/or Jira.
This pattern requires write-capable connectors, cross-connector data flow (a connector
graph), and an execution/orchestration runtime — none of which the framework currently
has, and none of which the spec §5/§6 adapter contract currently specifies.

The question put to the user: scope this work for now to a connector registry
(read/ingestion path), or design write + orchestration in the same change?

---

## Options Considered

### Option A — Registry-gated read sources only (no write, no orchestration)

Build a first-class Connector Registry that catalogs supported read-only connectors and
their capabilities. Gate CONFIGURE_SOURCES against it. All write/orchestration work is
explicitly deferred indefinitely with no roadmap.

**Pros**: Minimal scope. Directly fixes the capability-dishonesty defect. Stays within
the current spec §5/§6 adapter model. Low risk.

**Cons**: Does not address the driving Jira → Lumberjack → RCA-write use case at all,
even conceptually. Teams cannot plan for it.

### Option B — Full action/write connectors + multi-hop orchestration now

Design write-capable connector manifests, a permission/authz model for write actions,
a connector-graph data-flow contract, and an orchestration/execution runtime all in the
same phase.

**Pros**: Addresses the end-to-end driving scenario immediately.

**Cons**: Materially riskier than read. Write actions have authz requirements, blast
radius, and irreversibility properties that read connectors do not. Requires a connector
graph + execution runtime the framework does not have. Ties to a system safety model
(prohibited/explicit-permission action discipline) that has not been designed. Risks
blocking the urgent capability-honesty fix behind a large, poorly-scoped design effort.
Contradicts the spec's build-thin-slice-end-to-end-first principle (§7).

### Option C — Registry now + phased roadmap for write/orchestration (CHOSEN)

Ship the Connector Registry for read sources immediately (fixes capability-dishonesty
today). Produce an explicit, sequenced roadmap ADR (ADR-037) covering write-capable
manifest extensions, permission/authz, connector-graph data-flow, and orchestration
runtime — as a research track anchored in spec §8 (open problems). Mark ADR-037
as Proposed/Roadmap, not Accepted; no implementation until the phases are explicitly
approved.

**Pros**: Decouples the urgent fix (capability-honesty at authoring time) from the
large, riskier write/authz expansion. Teams can plan against the roadmap. The driving
Jira → Lumberjack → RCA-write scenario is documented and sequenced, not ignored.
Consistent with spec §7 phased build discipline and §8 open-problem classification.

**Cons**: The driving end-to-end scenario is not addressed in the current phase.
Mitigated: the roadmap ADR makes the path explicit and prevents the scenario from being
forgotten or re-litigated.

---

## Decision

**Option C: Ship a first-class READ-ONLY Connector Registry + honest fail-fast
capability gating now (ADR-036). Treat action/write connectors, connector-graph
data-flow, and multi-hop orchestration as a deliberate FUTURE PHASED roadmap
(ADR-037), explicitly not bolted on.**

### Rationale

The capability-dishonesty failure at authoring time (accepting any `source_type` string,
failing silently at runtime) is an immediate correctness defect that must be fixed
independently of the larger write/orchestration expansion. Bundling the two would delay
the fix behind a research track that has unresolved open questions (authz model, blast
radius guardrails, execution runtime design).

This decision is a **vision/scope decision touching spec §5/§6 load-bearing sections**.
Any future work that modifies the adapter contract beyond read-only ingestion requires
revisiting this decision and filing a follow-on DECISION or amending ADR-037 phases.

### Principles established (standing practice going forward)

1. **Connector type support is declared, not inferred.** The Connector Registry is the
   authoritative catalog of what the framework can do. An unsupported connector fails
   hard at CONFIGURE_SOURCES — never silently proceeds.

2. **Read-only ingestion is the current contract.** Spec §5/§6 adapter protocol
   covers read-only adapters. Any write-capable connector is out of scope until
   ADR-037 Phase 1 is explicitly approved.

3. **Write actions require explicit permission gating.** No write connector ships
   without a permission/authz model and guardrails (ADR-037 Phase 1 prerequisite).

4. **Open problems (spec §8) are research tracks, not implementation tasks.** ADR-037
   is filed as a roadmap under spec §8 discipline — sequenced, not guessed past.

---

## Seam with DECISION-015 / ADR-035

DECISION-015 and ADR-035 (concurrent, filed in the same session) specify that
CONFIGURE_SOURCES / INSPECT_SOURCES must **access-verify** declared source + reference +
output instances before DESIGN begins (instance-level: "can I reach this specific
Confluence space?").

The Connector Registry (ADR-036) operates at a strictly **earlier, type-level check**:
"is this connector TYPE supported at all?" A valid type is a prerequisite before
instance access-verify is meaningful. The sequencing within the CONFIGURE/INSPECT gate
is: **registry type-check (ADR-036) → instance access-verify (ADR-035)**. ADR-036 is
the catalog that ADR-035's access-verify code will consult. These are complementary;
neither replaces the other.

---

## Consequences

- `framework/adapters/` connectors acquire declarative capability manifests as a
  migration task (see ADR-036 §Migration). The adapters themselves are not rewritten.
- CONFIGURE_SOURCES gains a registry type-check gate before proceeding (ADR-036).
- ADR-037 phases are defined but carry no implementation commitment until explicitly
  approved by the user.
- Spec §5/§6 interface contract remains read-only ingestion for this phase.
- Any new connector proposed for implementation must be registered in the catalog before
  code is written; there is no path to an unregistered connector.

---

## Related

- ADR-036 (implementation: connector registry, read-only)
- ADR-037 (roadmap: write connectors + multi-hop orchestration)
- ADR-035 (implementation: CONFIGURE/INSPECT instance access-verify — concurrent)
- DECISION-015 (principle: CONFIGURE/INSPECT gate access-verify — concurrent)
- spec §5 (component map — adapter layer)
- spec §6.2 (parser contract), §6.3 (store contract)
- spec §8 (open problems — research track discipline)

---

*See also ADR-036 (connector registry design) and ADR-037 (write/orchestration roadmap).*

---

## Amendment note — 2026-05-17

ADR-036 was amended (same date) to fold in three decided items: (1) a **New Connector
Request demand-capture mechanism** — when the CONFIGURE_SOURCES gate hard-stops on an
unsupported connector, the system also logs a `CONNECTOR-REQ-…` record into
`KBF_BUG_REPORTS` (reusing DECISION-008/DECISION-013 ADB infrastructure; discriminated
by `record_kind: "connector_request"` in `extra_json`; exported separately via
`kb-cli export-bugs --kind connector_request`; grouped by connector identifier for
demand signal); (2) a **formal adapter ABC and connector conformance test harness**
folded into ADR-036 scope as a prerequisite for the registry's "supported" guarantee —
existing adapters are retrofitted to the ABC and must pass the harness before their
manifests are registered; (3) a **guided authorConnector codegen skill was considered
and deliberately dropped** — connector/adapter authoring is done by KBF developers
using Claude Code directly (LLM-auto-committed credential-handling network code
contradicts the project's safety discipline); the framework's role is to gate
unsupported connectors honestly and capture the demand backlog, not to auto-generate
adapters. No ADR-039.

## Amendment — 2026-05-17: UDAP deferred — NOT registered in the connector registry

UDAP is intentionally **not registered** in the Connector Registry until its production
JDBC path is implemented. `framework/adapters/udap_adapter.py` raises `NotImplementedError`
for all production `list`/`fetch`/`discover` calls; it only works in filestore/dev mode
against `_dev_fixtures/fleet/*.json`. Registering an unimplemented connector would violate
the capability-honesty principle this decision establishes — the very defect ADR-036 was
created to eliminate. UDAP is effectively another ADB database connector to be implemented
when its production JDBC path is ready. Tracked as future work alongside UDAP/ADB
implementation. The registry exposes exactly THREE connectors: Confluence, Jira, Git. Any
`source_type: "udap"` or `source_type: "fleet"` request at CONFIGURE_SOURCES will
hard-stop with the standard unsupported-connector message and log a `CONNECTOR-REQ-…`
demand record per ADR-036 Amendment 1 (L.3).
