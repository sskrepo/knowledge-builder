---
title: ADR-036 — Connector Registry (Read-Only) — Capability-Honest Authoring Gate
status: accepted
created: 2026-05-17
accepted: 2026-05-17
amended: 2026-05-17
owner: architect
deciders: user, architect
tags: [adr, connector, registry, capability, authoring, configure-sources, fail-fast, adapter-abc, connector-request]
related: [ADR-035, ADR-037, DECISION-016, DECISION-015, DECISION-008, DECISION-013]
supersedes: ~
---

# ADR-036 — Connector Registry (Read-Only) — Capability-Honest Authoring Gate

## Status

**Accepted — 2026-05-17. Amended — 2026-05-17** (three amendments folded in):
(1) New Connector Request demand-capture mechanism added at the CONFIGURE_SOURCES
gate hard-stop point; (2) formal adapter ABC + connector conformance test harness
folded into ADR-036 scope as a prerequisite for the registry's "supported" guarantee;
(3) authorConnector codegen skill considered and deliberately dropped.

Original acceptance: fixes the capability-dishonesty defect where CONFIGURE_SOURCES
silently accepts any `source_type` string and only fails at runtime when the
connector does not exist, violating the framework's fail-fast, no-silent-degradation
ethos. See DECISION-016 for the scoping decision that established this as the
immediate deliverable (read-only registry now; write/orchestration as a phased
roadmap in ADR-037).

---

## A. Context — The Problem

### Observed Failure

The current authoring gate at CONFIGURE_SOURCES performs only a non-empty check
on `source_type` (`framework/skill_builder/conversation.py:178-182`):

```
source_type must be present and non-empty … e.g. confluence_page, jira_filter, git_ref
```

This is not a contract against a declared catalog of what the framework can actually do.
If a skill author names `"lumberjack_logs"` as a source, the system:

1. Accepts the `source_type` string without error.
2. Proceeds through DESIGN_SKILL, producing a fully designed skill.
3. Fails at runtime when the ingestion pipeline tries to instantiate an adapter that
   does not exist.

The skill author receives no honest signal at authoring time. This is the same
capability-dishonesty failure family as BUG-queue-2ad9a (silent under-delivery),
applied at the authoring/configuration layer rather than the retrieval layer.

### Root Cause — No Connector Type Catalog

`framework/adapters/` contains ad-hoc, one-off adapters registered nowhere:
- `confluence/` — Confluence read adapter
- `jira/` — Jira read adapter
- `git_adapter.py` — Git ref adapter
- `udap_adapter.py` — UDAP/Sentinel fleet read-through
- `code_wiki_builder.py` — Som-style structural code wiki builder
- `_base.py` — abstract base

There is no unified Connector Registry that declares what connector types are supported,
what operations they support, and how to verify access to an instance of that connector.
`source_type` is a free string with no enforcement against this implicit set.

### Why This Must Be Fixed at Authoring Time

Fixing this at runtime (ingestion pipeline) is insufficient: skill design is the
expensive, user-facing step. A skill author who discovers their named source is
unsupported only after completing DESIGN has wasted the design effort and received a
misleading guarantee. The framework's ethos is fail-fast: surface capability gaps
before the user invests.

The Connector Registry is also a prerequisite for ADR-035's instance access-verify:
ADR-035 checks "can I reach this specific instance?" — but that question is only
meaningful after confirming "is this connector type supported at all?"

---

## B. Decision

**Create a Connector Registry — a declarative catalog of supported connector types
and their capabilities — that CONFIGURE_SOURCES consults before proceeding to
instance access-verify (ADR-035) or DESIGN_SKILL.**

The registry is read-only in this phase. Write-capable connector extensions are a
separate, later, approved track (ADR-037).

---

## C. Connector Registry Design

### C.1 Capability Manifest Schema

Each connector publishes a **capability manifest** — a declarative, static
description of what that connector type can do. The manifest is the unit of
registration.

| Field | Type | Description |
|---|---|---|
| `connector_id` | `str` | Unique stable identifier. Matches the `source_type` string the skill author uses. Example: `"confluence"`, `"jira"`, `"git"`, `"udap"`. |
| `display_name` | `str` | Human-readable name for error messages and UI. Example: `"Confluence"`. |
| `description` | `str` | One-sentence summary of what this connector provides. |
| `resource_types` | `list[str]` | Entity/content types this connector exposes. Example: `["page", "space", "attachment"]` for Confluence. |
| `supported_operations` | `list[str]` | Operations the connector supports. In this phase: read-only subset. Values: `"read"`, `"query"`, `"list"`, `"search"`. |
| `auth_model` | `str` | Authentication pattern required. Example: `"api_key"`, `"oauth2"`, `"env_service_account"`, `"internal_db"`. |
| `access_probe_hook` | `str` | Dotted Python import path to the function that performs instance-level connectivity verification (used by ADR-035 access-verify). Example: `"framework.adapters.confluence.probe.verify_access"`. |
| `granularity_filters` | `list[str]` | Filter dimensions the connector supports when scoping a source. Example: `["space_key", "label", "page_id_list"]`. |
| `notes` | `str \| None` | Optional. Known limitations, version constraints, or caveats. |

**Phase constraint**: `supported_operations` in this phase MUST be a subset of
`{"read", "query", "list", "search"}`. Operations `"write"`, `"delete"`, `"create"`,
`"update"` are reserved for ADR-037 Phase 1 and MUST NOT appear in registered manifests
until that phase is approved.

### C.2 Example Manifests (v1 — four initial connectors)

#### Confluence

```yaml
connector_id: confluence
display_name: Confluence
description: >
  Read-only access to Confluence spaces, pages, and attachments via the
  Confluence REST API. Primary source for PM/TPM wiki content and
  incident post-mortems.
resource_types:
  - page
  - space
  - attachment
  - blog_post
supported_operations:
  - read
  - search
  - list
auth_model: api_key
access_probe_hook: framework.adapters.confluence.probe.verify_access
granularity_filters:
  - space_key
  - label
  - page_id_list
  - ancestor_page_id
notes: ~
```

#### Jira

```yaml
connector_id: jira
display_name: Jira
description: >
  Read-only access to Jira issues, filters, epics, and sprints via the
  Jira REST API. Primary source for incident tickets, roadmap items,
  and sprint data.
resource_types:
  - issue
  - epic
  - sprint
  - filter
  - project
supported_operations:
  - read
  - query
  - list
  - search
auth_model: api_key
access_probe_hook: framework.adapters.jira.probe.verify_access
granularity_filters:
  - jql_filter
  - project_key
  - issue_type
  - label
  - sprint_id
notes: JQL is the primary query language. Filters are pre-saved JQL queries.
```

#### Git

```yaml
connector_id: git
display_name: Git Repository
description: >
  Read-only access to git repository content at a specified ref (branch,
  tag, or commit SHA). Used for code wiki generation and source file
  ingestion.
resource_types:
  - file
  - directory
  - commit
  - ref
supported_operations:
  - read
  - list
auth_model: env_service_account
access_probe_hook: framework.adapters.git_adapter.probe.verify_access
granularity_filters:
  - ref
  - path_prefix
  - file_extension
notes: >
  The git adapter supports both local clones and remote clone-on-demand.
  Large monorepos should scope with path_prefix to avoid full-tree traversal.
```

#### UDAP / Sentinel (Fleet)

```yaml
connector_id: udap
display_name: UDAP / Sentinel Fleet
description: >
  Read-through access to the live fleet state database (UDAP/Sentinel).
  Provides instance inventory, pod state, ops events, and service topology.
  No re-ingestion — data is queried live via allowlisted views.
resource_types:
  - instance
  - pod
  - service
  - ops_event
  - tenancy
supported_operations:
  - query
  - read
auth_model: internal_db
access_probe_hook: framework.adapters.udap_adapter.probe.verify_access
granularity_filters:
  - tenancy_id
  - region
  - service_name
  - event_type
  - time_window
notes: >
  UDAP/Sentinel is read-through only; no data is copied into framework stores.
  query_fleet and text_to_sql MCP tools operate over this connector's
  allowlisted view set.
```

### C.3 Registry Location and Authority

The Connector Registry is a **declarative catalog** — a set of manifests — that serves
as the single source of truth for both:

1. **Authoring-time gating** — CONFIGURE_SOURCES validates `source_type` against
   registered `connector_id` values before allowing design to proceed.
2. **Instance access-verify** — ADR-035's access-verify step calls the registered
   `access_probe_hook` for each declared source instance. The registry provides the
   hook; ADR-035 invokes it.

Conceptually the registry is a flat catalog that can be loaded at startup. It does NOT
prescribe a specific file format, loader class name, or import path — those are
implementation details deferred to the implementing developer per standard framework
discipline (this ADR specifies the contract; devs write the code).

The registry MUST be the only place where a connector type is declared as supported.
An adapter that exists in `framework/adapters/` but is not registered in the catalog
is treated as internal-only (not available to skill authors) until it is explicitly
registered.

---

## D. Authoring-Time Capability Gating Flow

### D.1 CONFIGURE_SOURCES Gate (revised with registry)

The revised gating sequence within the CONFIGURE_SOURCES step:

```
1. Parse declared sources from skill author input
   → extract (source_type, reference, output_destination) tuples

2. [ADR-036] For each source_type:
   a. Look up connector_id in the Connector Registry
   b. If NOT FOUND → HARD STOP (see D.2 — honest failure message)
   c. If FOUND → verify required operations against supported_operations
   d. If operation not supported → HARD STOP (see D.2)

3. [ADR-035] For each validated (connector, reference) pair:
   → instance access-verify (probe the specific endpoint/credential)
   → if unreachable → HARD STOP per ADR-035 protocol

4. All sources validated → proceed to DESIGN_SKILL
```

Step 2 (registry type-check) is strictly earlier than Step 3 (instance access-verify).
The registry check is a cheap, local catalog lookup. It runs first to eliminate
unsupported connectors before any network I/O.

### D.2 Honest Failure Message — Example

When a skill author declares `source_type: "lumberjack_logs"` and no manifest with
`connector_id: "lumberjack_logs"` exists in the registry, the system MUST emit a
message of this form (no LLM paraphrasing; verbatim structured output):

```
CONFIGURE_SOURCES failed: unsupported connector type "lumberjack_logs".

This connector type is not registered in the Connector Registry and cannot
be used as a skill source.

Supported connector types in this framework installation:
  - confluence      (Confluence — pages, spaces, attachments)
  - jira            (Jira — issues, filters, epics, sprints)
  - git             (Git Repository — files, commits, refs)
  - udap            (UDAP / Sentinel Fleet — instances, pods, events)

To use "lumberjack_logs" as a source, the connector must first be registered
in the Connector Registry with a capability manifest. This is an engineering
task, not a skill-author task.

Skill design has not been started. No partial state has been saved.
```

Key properties of this message:
- Names the exact unsupported `connector_id` verbatim.
- Lists every currently-supported connector with its display name and resource types.
- Distinguishes skill-author action (none) from engineering action (register a manifest).
- Confirms no partial state exists (fail-fast, clean slate).
- Does NOT ask the LLM to improvise an answer or suggest workarounds.

---

## E. Seam with ADR-035

ADR-035 (CONFIGURE/INSPECT instance access-verify) specifies that the authoring gate
must verify that the framework can actually reach each declared source instance (the
specific Confluence space, the specific Jira filter) before DESIGN_SKILL begins.

The connector registry (ADR-036) is the **prerequisite** step: it checks that the
connector TYPE is supported. ADR-035's access-verify step then checks that the specific
INSTANCE is reachable. Both gates live within the CONFIGURE_SOURCES / INSPECT_SOURCES
boundary; they are sequenced (type-check first, instance-check second) and
complementary.

**Integration note**: ADR-035 access-verify calls `access_probe_hook` per the
connector's registered manifest. This means ADR-036 must land first (or concurrently
in a coordinated way) so that ADR-035's code has manifests to read probe hook paths
from. The integration at the code level is a sequenced follow-up after ADR-035 is
implemented; this ADR does not specify the code paths — implementation is a separate,
later, approved step.

---

## F. Migration — Existing Adapters

Existing adapters in `framework/adapters/` were built without manifests. The migration
path is:

| Adapter | Required Action |
|---|---|
| `confluence/` | Add `probe.verify_access` function; register manifest (example above). |
| `jira/` | Add `probe.verify_access` function; register manifest (example above). |
| `git_adapter.py` | Add `probe.verify_access` function; register manifest (example above). |
| `udap_adapter.py` | Add `probe.verify_access` function; register manifest (example above). |
| `code_wiki_builder.py` | Evaluate: is this a connector (author-declarable source) or an internal pipeline tool? If internal-only, do NOT register — leave it as an unregistered internal tool. |
| `_base.py` | No change — abstract base is not a registered connector. |

Migration does not require rewriting the adapter logic. It requires:
1. Adding the `probe.verify_access` function (a lightweight connectivity check).
2. Authoring and registering the capability manifest.

Migration is a prerequisite for enabling the ADR-035 access-verify for those connectors.

---

## G. Non-Goals (Explicit)

The following are explicitly OUT OF SCOPE for this ADR:

- **Write operations** — no write, create, update, or delete connector operations.
  Write-capable manifest extensions are ADR-037 Phase 1.
- **Cross-connector data flow** — no connector graph, no connector-output-as-input
  chaining. That is ADR-037 Phase 2.
- **Orchestration runtime** — no execution runtime for multi-step connector workflows.
  That is ADR-037 Phase 3.
- **Dynamic connector registration at runtime** — manifests are static/startup-loaded.
  Dynamic plugin discovery is not specified here.
- **UI/dashboard for the registry** — the registry is a backend authoring gate,
  not a user-facing catalog UI.
- **Third-party/external connector marketplace** — all connectors in v1 are
  first-party framework connectors.

---

## H. Test Strategy (High Level)

| Test type | What it covers |
|---|---|
| Unit — registry catalog | Manifest schema validation. `get_connector("confluence")` returns correct manifest. `get_connector("lumberjack_logs")` returns `None`. `list_connectors()` returns exactly the registered set. |
| Unit — gating logic | Given `source_type="lumberjack_logs"`, CONFIGURE_SOURCES gate returns HARD_STOP with the correct structured error. Given `source_type="confluence"`, gate returns PASS and calls the probe hook. |
| Unit — error message | Hard-stop message names the unsupported connector verbatim; lists all supported connectors; no LLM-generated text in the message. |
| Integration — probe hooks | `verify_access` for each registered connector returns meaningful connectivity result (mocked for CI; real endpoint in staging). |
| Regression | Existing skill authoring flows for `confluence` and `jira` sources pass the new registry gate without modification to skill author input. |

---

## I. Consequences

### Positive

- Capability-dishonesty defect eliminated at authoring time. An unsupported connector
  produces a clear, immediate, actionable failure rather than a silent accept followed
  by a runtime crash.
- The registry is the single source of truth for what connector types are supported.
  Adding a new connector requires registering a manifest; there is no other path.
- ADR-035's instance access-verify now has a well-defined catalog to consult for probe
  hook paths — no ad-hoc `if source_type == "confluence":` dispatch in the gate code.
- Error messages name exactly what is missing and list what IS supported —
  consistent with the framework's capability-honesty principle.

### Negative

- Every existing adapter must have a `probe.verify_access` function added (migration
  cost). For the four current connectors this is a bounded, one-time task.
- Skill authors can no longer experiment with unregistered `source_type` strings
  (previously silently accepted). This is intentional: the old behavior was a bug.

### Reversibility

The registry is additive. Removing the type-check gate would revert to the previous
(broken) behavior. The catalog itself is strictly additive — registering a new manifest
has no impact on existing connectors.

---

## J. Alternatives Considered

### Option A — Runtime-only enforcement (no authoring gate)

Check connector support at ingestion pipeline startup, not at CONFIGURE_SOURCES.

Rejected: users complete expensive skill design steps before learning the connector is
unsupported. Violates fail-fast principle.

### Option B — Dynamic capability discovery (adapter introspection)

Each adapter exposes a `capabilities()` method; the gate calls it at authoring time
to discover what the adapter can do.

Rejected for v1: dynamic discovery adds complexity with no benefit when the adapter
set is small and static. A declarative catalog is simpler, testable without real
adapters, and avoids the chicken-and-egg problem (how do you know which adapters to
introspect if you have no catalog?). Dynamic discovery can be added later if the
connector set grows large enough to make static manifests burdensome.

### Option C — Prompt-based guidance only (tell the LLM what connectors are supported)

Inject the list of supported connectors into the `design_skill` prompt; let the LLM
warn the user if it infers an unsupported connector.

Rejected: LLM-based capability gating is unreliable, untestable, and violates the
framework's deterministic-extraction discipline. The gate must be a hard programmatic
check, not a soft LLM suggestion.

---

## K. Related ADRs

- **ADR-035** — CONFIGURE/INSPECT instance access-verify. ADR-036 (type-check) is the
  prerequisite step; ADR-035 (instance-check) runs after it. The registry provides
  probe hook paths that ADR-035 calls.
- **ADR-037** — Write connectors and multi-hop orchestration (roadmap). ADR-037 Phase 1
  extends the manifest schema with `"write"` and `"create"` operations; the registry
  structure defined here is the foundation it builds on.
- **DECISION-016** — Scope decision: registry-now + phased write/orchestration.
- **DECISION-015** — Principle decision: CONFIGURE/INSPECT gate access-verify (concurrent).
- **Spec §5** — Component map: adapters are read-only ingestion adapters. This ADR adds
  the registry layer on top of the existing adapter model.
- **Spec §6.2** — Parser contract: parsers operate on `RawItem` produced by adapters.
  Registry manifests define what kinds of `RawItem` each connector produces.

---

---

## L. Amendment 1 — New Connector Request Demand Capture

### L.1 Overview

When the CONFIGURE_SOURCES registry type-check gate (Section D.2) issues a
HARD STOP for an unsupported connector, the system MUST ALSO auto-log a
**New Connector Request** record so KBF_OPS developers accumulate a demand
backlog for unregistered connectors. The user still receives the honest
"not supported" message (unchanged from D.2). The demand capture is a
side-effect of the same gate point — it does not alter the user-facing
behavior or soften the hard stop.

### L.2 Storage and Distinction from Bug Reports

New Connector Requests reuse the existing ADB bug-record infrastructure
established in DECISION-008 and DECISION-013:

- **Table**: `KBF_BUG_REPORTS` (same table as `AdbErrorStore.record_user_bug`).
  No schema migration. Non-standard fields ride in the `extra_json` CLOB,
  consistent with the DECISION-013 precedent for `discovered_by`.

- **Discriminator field** (in `extra_json`):
  `record_kind: "connector_request"`. This distinguishes connector requests
  from ordinary bug records (`record_kind` absent or `"bug"`) without adding
  a new column.

- **Queue ID prefix**: `CONNECTOR-REQ-<uuid4_hex[:5]>` (e.g.
  `CONNECTOR-REQ-3a7f1`). This is structurally distinct from the bug prefix
  `BUG-queue-…`, making connector requests immediately identifiable by ID
  in exports and logs without parsing `extra_json`.

### L.3 Captured Fields

Each New Connector Request record MUST capture the following fields (carried
in `extra_json`):

| Field | Description |
|---|---|
| `record_kind` | `"connector_request"` (discriminator) |
| `requested_connector_id` | The unsupported `source_type` string verbatim (e.g. `"lumberjack_logs"`). Structured field — used for dedup/grouping. |
| `inferred_operation` | The operation the author was attempting: `"read"`, `"query"`, `"write"`, or `"unknown"` if not inferable from context. |
| `supported_set_at_rejection` | Snapshot of `list_connectors()` output at the time of rejection (for context; list of `connector_id` strings). |
| `user_request_text` | The original user question/request text that triggered CONFIGURE_SOURCES. |
| `persona` | Persona label of the skill author session (if known; `null` otherwise). |
| `session_id` | The authorSkill conversation session ID (if within a skill-authoring flow; `null` otherwise). |
| `request_id` | Request-level correlation ID from the framework's structured logging context. |
| `timestamp` | ISO-8601 UTC timestamp of the rejection event. |

### L.4 Surfacing to KBF Developers

The existing `kb-cli export-bugs` mechanism (DECISION-008: generated read-only
snapshot from ADB) is extended to surface connector requests SEPARATELY from
bug records:

- **Separate section or file**: connector requests are exported to a distinct
  section in the export output or a separate file. Suggested path:
  `pmo/connector-requests/` (parallel to `pmo/bugs/`). The export mechanism
  already owns this path — do NOT create a hand-maintained store alongside it.
  The generated snapshot IS the authoritative view.

- **Filter flag**: `kb-cli export-bugs --kind connector_request` MUST produce
  only connector request records; the default (no `--kind` filter) produces
  only ordinary bug records (existing behavior unchanged). Both may be emitted
  together via `--kind all`.

- **Demand grouping**: the export for connector requests MUST group records by
  `requested_connector_id` and include a count per connector identifier. This
  gives KBF developers a demand signal (e.g., "lumberjack_logs requested 7
  times"). Dedup/aggregation is a required behavior of the export step, not
  optional.

### L.5 No Silent Drop

If the connector-request ADB write fails (network error, ADB unavailable, etc.),
the failure MUST be logged at ERROR level with the full record details. The write
failure MUST NOT silently swallow the demand signal and MUST NOT be confused with
the user-facing hard stop (which has already been issued at this point). The
framework's no-silent-degradation rule applies to the demand capture path as
strictly as to any retrieval path.

### L.6 Gate Seam

The demand capture fires at EXACTLY the same point as the existing CONFIGURE_SOURCES
hard stop for an unsupported connector (Section D.1, Step 2b). The revised sequence
at that point is:

```
2b. connector_id NOT FOUND in registry:
    i.  Log New Connector Request to KBF_BUG_REPORTS via AdbErrorStore
        (queue_id = CONNECTOR-REQ-<uuid>; extra_json fields per L.3)
        → if write fails: log ERROR, continue to hard stop (do not suppress hard stop)
    ii. Emit HARD STOP message to user (verbatim format per D.2, unchanged)
    → skill design NOT started; no partial state saved
```

The same gate extension applies when a connector type IS found but the requested
operation is not in `supported_operations` (Step 2d): log a connector request
with `inferred_operation` set to the unsupported operation, then hard stop.

---

## M. Amendment 2 — Formal Adapter ABC and Connector Conformance Harness

### M.1 Motivation

The registry's declaration that a connector is "supported" is only meaningful
if every adapter registered in the registry actually honors a defined interface
contract. Currently `framework/adapters/_base.py` exists but is ad-hoc and
unenforced: `confluence/`, `jira/`, `git_adapter.py`, and `udap_adapter.py`
were built without a formal base class mandate. The registry's "supported"
guarantee is hollow unless every registered adapter is verified to implement
the required contract.

A formal adapter ABC and a per-adapter conformance test harness are therefore
**prerequisites for the registry to be trustworthy** and are folded into
ADR-036 scope (not deferred to a separate ADR).

### M.2 Adapter ABC Contract

`framework/adapters/_base.py` MUST be elevated from its current ad-hoc state
to a formal Abstract Base Class (ABC) defining the following interface:

| Method / property | Signature (conceptual) | Required | Notes |
|---|---|---|---|
| `connector_id` | `@property → str` | Yes | Must match the `connector_id` in the registered manifest. |
| `normalize(raw_item) → ContentItem` | Takes connector-specific raw output and produces a `ContentItem` per spec §6.1. | Yes | Output shape is the ContentItem schema — no adapter-specific shapes downstream. |
| `emit_citation(raw_item) → str` | Returns a stable, resolvable source URL or path for the item. | Yes | Every ContentItem must carry a citation. No citation = bug (cross-cutting requirement). |
| `content_hash_id(raw_item) → str` | Deterministic content-hash-based ID for idempotent ingestion. | Yes | Re-running ingestion on unchanged content must be a no-op (CLAUDE.md cross-cutting requirement). |
| `incremental_update_hook()` | Called on webhook/push trigger; yields only changed items since last run. | Yes | Full re-index only on schema/model change. |
| `map_error_to_rejection(exc) → ContentFilterRejection` | Maps connector-specific exceptions to the framework's `ContentFilterRejection` type. | Yes | Prevents connector-specific error types from leaking past the adapter boundary. |
| `probe_access() → AccessProbeResult` | Lightweight connectivity/credential check. | Yes | Called by ADR-035 access-verify; replaces ad-hoc `probe.verify_access` functions. |

Adapters that cannot implement a method (e.g., UDAP has no meaningful
`incremental_update_hook` because it is read-through) MUST explicitly raise
`NotImplementedError` with a clear message, NOT silently pass or return `None`.

### M.3 Connector Conformance Test Harness

Every adapter registered in the Connector Registry MUST pass a **connector
conformance test harness** before it can be marked `supported` in the registry.
The conformance harness is a test suite (part of the framework's eval/ layer)
that verifies:

| Conformance check | What it tests |
|---|---|
| ABC implementation | Adapter subclasses the formal ABC; all abstract methods are implemented (not just inherited). |
| `normalize()` output shape | Output is a valid `ContentItem` per spec §6.1 schema. Tested with fixture raw items per connector type. |
| `emit_citation()` non-empty | Citation is a non-empty string for every fixture item. |
| `content_hash_id()` determinism | Same raw input produces the same ID on repeated calls; different raw inputs produce different IDs (collision test). |
| `map_error_to_rejection()` coverage | At least the connector's documented error types are mapped; unmapped errors are caught and re-raised as `ContentFilterRejection` (no leakage). |
| `probe_access()` return type | Returns `AccessProbeResult` (not `None`, not raw exception). |
| `connector_id` manifest match | `adapter.connector_id` matches the `connector_id` in the registered manifest exactly. |

The conformance harness runs in CI. A newly implemented adapter MUST pass
the harness before the connector manifest is merged into the registry (i.e.,
before it is visible to skill authors). An existing registered adapter that
fails the conformance harness after a code change MUST NOT be left in the
`supported` set — its registration is suspended until the conformance failure
is resolved.

### M.4 Migration Path for Existing Adapters

The four existing adapters (`confluence/`, `jira/`, `git_adapter.py`,
`udap_adapter.py`) are retrofitted to the ABC as part of the ADR-036
implementation work. The migration is bounded and sequenced:

1. Elevate `framework/adapters/_base.py` to formal ABC (implement abstract
   method stubs with clear docstrings).
2. For each existing adapter: subclass the ABC, implement all abstract methods,
   migrate any existing `probe.verify_access` function into `probe_access()`.
3. Write conformance test fixtures for each adapter (using mocked connector
   responses for CI; real endpoints in staging).
4. Pass conformance harness for each adapter.
5. Register manifests (per Section C above) only after conformance passes.

The migration does not require rewriting adapter business logic — only formalizing
the interface boundary and adding the new required methods.

---

## N. Amendment 3 — Considered and Deferred: authorConnector Codegen Skill

A guided "authorConnector" skill was considered during the design session for
ADR-036: an LLM-driven workflow that would walk a user through specifying a new
connector and auto-generate adapter code (credential handling, HTTP client,
normalize() implementation) for direct commit.

**This was deliberately NOT pursued.** Reasons:

1. **Safety discipline**: LLM-authored credential-handling and network-client
   code that is auto-committed to the framework violates the project's
   deterministic-extraction discipline and the principle that load-bearing
   infrastructure changes require human review. The blast radius of an
   LLM-generated adapter with a subtly incorrect credential-handling pattern
   or a missing error mapping is high.

2. **Right tool**: KBF developers authoring new adapters use Claude Code directly
   — a supervised, human-in-the-loop interaction — not an autonomous codegen
   skill. The distinction is important: Claude Code with a human reviewing each
   step is materially different from an auto-commit pipeline.

3. **Framework role**: the framework's responsibility is (i) to honestly gate
   unsupported connectors at authoring time (ADR-036 capability gate) and
   (ii) to capture New Connector Requests as a structured demand backlog that
   feeds human-led adapter development work (Amendment 1 above). The framework
   does NOT own the adapter authoring workflow beyond that boundary.

No ADR-039 or follow-on codegen skill is planned. Connector/adapter authoring
remains a KBF developer task, informed by the demand signal from the
`CONNECTOR-REQ-…` records in the export.

---

## O. Amendment 4 — UDAP intentionally excluded from the registry (2026-05-17)

UDAP is **not registered** in the Connector Registry. The connector manifests directory
(`framework/connectors/manifests/`) contains exactly three manifests: Confluence, Jira,
Git. No `udap.yaml` exists or should be added until the UDAP adapter's production JDBC
path is implemented.

**Rationale**: `framework/adapters/udap_adapter.py` raises `NotImplementedError` for all
production `list`/`fetch`/`discover` operations. It only works in filestore/dev mode
against `_dev_fixtures/fleet/*.json`. Registering an adapter whose production path is
unimplemented would be exactly the capability-dishonesty this ADR was created to
eliminate — skill authors would see "udap" in the supported connector list, attempt to
build fleet-data skills, and get `NotImplementedError` at runtime. The hard stop must
happen here, not there.

Any `source_type: "udap"` or `source_type: "fleet"` at CONFIGURE_SOURCES hits the
honest hard-stop (Section D.2) and logs a `CONNECTOR-REQ-…` demand record per
Amendment 1 (Section L). This is the correct behavior — the demand signal accumulates
until the production JDBC path is implemented and UDAP passes the conformance harness
(Amendment 2, Section M), at which point its manifest is registered.

This is a scope decision, not a defect. See DECISION-016 Amendment (2026-05-17) for
the full rationale.

---

## P. Connector Discoverability (2026-05-18)

### P.1 Gap Addressed

ADR-036 shipped the gating/rejection surface only: CONFIGURE_SOURCES shows the
supported connector list reactively, when a skill author requests an unsupported
connector. There was no proactive way to discover the supported set.

This was filed as BUG-queue-19f2f (severity MEDIUM, discovered_by architect,
status fixed): "users could only learn supported connectors by failing — undermines
the capability-honesty intent of ADR-036."

### P.2 Two Surfaces Added

**1. `listConnectors` MCP tool** (`framework/deploy/mcp_tools.py`):
- Read-only, no side effects. No write/admin scope required — mirrors the auth
  gating of other read-only tools (reportBug, listSkills).
- Registered in `EXTERNAL_TOOLS_SCHEMA` (appears in `/mcp/tools/list`) and in
  `build_external_tool_registry()` (callable via `POST /mcp/tools/call`).
- Returns, per connector: `connector_id`, `display_name`, `description`,
  `resource_types`, `supported_operations`, `auth_model`.
- Strips internal fields (`access_probe_hook`, `granularity_filters`).
- Source: `get_registry().list_connectors_user_facing()` — the registry is the
  only source of truth. No duplicate connector lists anywhere.
- Returns exactly 3 connectors (confluence, git, jira). UDAP not present
  (capability-honesty, per Amendment 4 / Section O).

**2. Proactive supported-connector block at CONFIGURE_SOURCES entry** (`framework/skill_builder/conversation.py`):
- When the FSM enters CONFIGURE_SOURCES (`_advance_to_configure_sources` and
  the `_advance_to_configure_sources_v2` no-sources path), the prompt now includes:
  ```
  Supported source connectors:
    - confluence      (Confluence — page, space, attachment...)
    - git             (Git Repository — file, directory, commit...)
    - jira            (Jira — issue, epic, sprint...)
  ```
- The author sees the supported set BEFORE specifying sources, not only after
  requesting an unsupported one.

### P.3 Single Shared Helper — Drift Prevention

Both the proactive block and the hard-stop rejection message (Section D.2) render
through `format_supported_connectors_block()` in `framework/connectors/registry.py`.
This is the sole rendering code path — a connector added to or removed from the
registry automatically propagates to both surfaces without additional code changes.
`manifest_to_user_facing()` is the single projection that strips internal fields.

Test `TestSharedFormattingHelperDriftGuard` verifies this: change the registry
test-double once, assert both surfaces reflect it.

### P.4 Test Coverage

`framework/tests/unit/test_adr036_discoverability.py` (33 tests):
- `listConnectors` returns exactly 3 connectors with 6 user-facing fields, no
  internal probe fields, UDAP absent.
- `listConnectors` appears in `EXTERNAL_TOOLS_SCHEMA` and `build_external_tool_registry`.
- Callable with read-only scope, no-scope anonymous, or no _consumer.
- CONFIGURE_SOURCES proactive block contains all 3 display names and `connector_id`s.
- Drift guard: same helper → both surfaces update from one registry change.
- No regression in supported/unsupported CONFIGURE_SOURCES flows.

---

## References

- `framework/adapters/` — existing ad-hoc adapter implementations (migration target)
- `framework/adapters/_base.py` — to be elevated to formal ABC (Amendment 2)
- `framework/skill_builder/conversation.py:178-182` — current non-empty `source_type` check (to be replaced by registry gate)
- DECISION-016 — scope decision (amended with cross-ref to these amendments)
- DECISION-008 — ADB bug-record export mechanism (extended by Amendment 1)
- DECISION-013 — precedent for `extra_json` non-standard fields without schema migration
- ADR-035 — concurrent instance access-verify design
- ADR-037 — write/orchestration roadmap
- spec §5 (component map), §6.1 (ContentItem schema), §6.2 (parser contract), §8 (open problems)
