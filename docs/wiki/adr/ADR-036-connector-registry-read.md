---
title: ADR-036 — Connector Registry (Read-Only) — Capability-Honest Authoring Gate
status: accepted
created: 2026-05-17
accepted: 2026-05-17
owner: architect
deciders: user, architect
tags: [adr, connector, registry, capability, authoring, configure-sources, fail-fast]
related: [ADR-035, ADR-037, DECISION-016, DECISION-015]
supersedes: ~
---

# ADR-036 — Connector Registry (Read-Only) — Capability-Honest Authoring Gate

## Status

**Accepted — 2026-05-17.** Fixes the capability-dishonesty defect where
CONFIGURE_SOURCES silently accepts any `source_type` string and only fails at
runtime when the connector does not exist, violating the framework's fail-fast,
no-silent-degradation ethos. See DECISION-016 for the scoping decision that
established this as the immediate deliverable (read-only registry now; write/
orchestration as a phased roadmap in ADR-037).

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

## References

- `framework/adapters/` — existing ad-hoc adapter implementations (migration target)
- `framework/skill_builder/conversation.py:178-182` — current non-empty `source_type` check (to be replaced by registry gate)
- DECISION-016 — scope decision
- ADR-035 — concurrent instance access-verify design
- ADR-037 — write/orchestration roadmap
- spec §5 (component map), §6.2 (parser contract), §8 (open problems)
