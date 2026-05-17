# DECISION-017: EVAL Two-Axis Redesign — Internal Execution vs Route Dry-Run

**Status**: DECIDED
**Date**: 2026-05-17
**Decided by**: User (directive given in DECISION-017 session, 2026-05-17)
**Informed by**: Root-cause analysis of `_run_eval` chicken-and-egg defect (BUG-queue-2ad9a,
  commit 8c2bec1); ADR-033 analysis of promoted-only routing; ADR-029 comparator pipeline
**Implemented in**: ADR-038

---

## Context

### The Root Defect

`_run_eval` validates workflow-scoring by issuing an HTTP `POST /api/v1/ask` with a generic
canonical question + persona. The router (`shim_workflows.py` `all_cards()`, established by
ADR-033, commit ee2740b) returns ONLY ADB-promoted skills. A skill under authoring sits at
FSM state `EVAL`, which is BEFORE `PROMOTE` in the FSM order:

```
… PREVIEW_EXTRACTION → CONFIRM → COMMITTED → VALIDATE → INGEST → EVAL → PROMOTE → DONE
```

Because the skill is not yet promoted, `/api/v1/ask` can never route to it. Consequently:
- `wf_tier` falls to 2 (fallback), never 1
- `skill_matched = False` always
- `artifact_url = null`
- The ADR-029 comparator is silently skipped
- `structure_score` is always null during authoring EVAL

This is a chicken-and-egg problem: EVAL must validate the skill BEFORE PROMOTE, but the
validation path only reaches PROMOTED skills. The current code collapses "no artifact
produced" into a soft diagnostic note — violating the project's no-silent-degradation rule
(DECISION-013 §Severity).

### The Key Constraint

A skill's KB is ingested at the `INGEST` FSM state. Executing the skill before INGEST is
semantically meaningless — its knowledge base does not exist yet. Therefore:

**Floor rule: EVAL axis A (execution) and axis B (routing) both require the skill to be
at INGEST-or-later FSM state.**

### Relevant Prior Art

- **ADR-033** (commit ee2740b): Added `WorkflowExecutor.execute_from_config(cfg, inputs)` as
  a public in-process method. This is the hook that makes Path A possible without the promoted
  router.
- **ADR-035**: Established `has_bound_reference_artifact()` as the single truth for artifact
  binding — consumed by the comparator gate in `_run_eval`.
- **ADR-029**: The structural comparator (candidate artifact vs bound reference artifact).
- **BUG-queue-2ad9a** (commit 8c2bec1): Formally documented the promoted-only routing defect
  producing always-null `structure_score` at EVAL time.

---

## Options Considered

### Option 1 — Execute-Direct Only (Path A alone)

Replace the HTTP `POST /api/v1/ask` in `_run_eval` with a call to
`WorkflowExecutor.execute_from_config(cfg, inputs)` directly. The comparator then runs
against the in-process artifact.

**Pros**: Fixes the chicken-and-egg; comparator runs; simple surface area change.

**Cons**: Loses routing-correctness coverage entirely. EVAL would confirm the skill
EXECUTES correctly but provide no signal about whether the ROUTER would ever SELECT it
for real users. A skill could pass EVAL and be promoted while routing to the wrong skill
or being unreachable — a qualitatively different failure mode. User's explicit insight:
execution coverage alone is insufficient.

**Rejected**: Missing axis makes EVAL blind to a critical category of defect.

### Option 2 — Privileged Public /ask Flags for Both Axes

Add HTTP flags to `/api/v1/ask`:
- `?include_unpromoted=true` — router considers non-promoted skills
- `?dry_run_only=true` — route without executing

EVAL calls both variants.

**Cons**: `include_unpromoted=true` puts in-authoring skill execution on the public
consumption wire. Even if gated by a token, this reintroduces the "drafts consumable
by real consumers" risk that ADR-033 explicitly ruled out. Any caller (or misconfigured
consumer) that passes the flag gets draft output. This is a non-starter given the
ADR-033 invariant.

**Rejected**: Unpromoted-execute on the public wire violates ADR-033's foundational
consumer-isolation invariant.

### Option 3 — Path A Internal-Only + Path B Resolve-Only (CHOSEN)

Split EVAL into two orthogonal axes:

**Path A (execution-fidelity)**: `WorkflowExecutor.execute_from_config(cfg, inputs)` called
in-process, internal to the authoring runtime. Never exposed as a public flag or endpoint.

**Path B (routing-correctness)**: A router resolve-only mode that returns the routing
decision (skill + tier selected) WITHOUT executing. This mode considers INGEST-or-later
skills when invoked by the internal EVAL path. The default public consumption path is
UNCHANGED — real consumers still get promoted-only routing.

**Pros**:
- Both coverage axes provided
- Draft execution stays off the public wire (ADR-033 invariant preserved)
- Route dry-run has zero side effects and returns no skill output — safe interim exposure
- Each axis reports distinctly and loudly (no silent collapse)

**Cons**:
- Path B in its interim form (no AUTH layer) considers ALL INGEST-or-later skills, not
  just the requesting author's own. This is a known, deliberate interim limitation (see
  §Interim Scoping below).

**Chosen.**

---

## Approved Design

### Path A — Execution-Fidelity (Internal Only)

`_run_eval` calls `WorkflowExecutor.execute_from_config(cfg, inputs)` using the session's
committed/design config and the session's real inputs (configured Confluence page,
bound-reference context). The result is a candidate artifact, handed to the ADR-029
comparator against the ADR-035 bound reference.

**INGEST-or-later gate**: If the skill is at COMMITTED or VALIDATE state (pre-INGEST),
`_run_eval` must hard-fail with a loud error explaining the floor requirement. Reaching EVAL
state naturally satisfies this gate (FSM order enforces it), but the gate is checked
explicitly to guard against future FSM changes.

**This capability is NEVER exposed as a public HTTP flag or endpoint.** It exists only
inside the authoring/EVAL runtime. Zero public attack surface. The ADR-033 invariant
("drafts never consumable by real consumers") is preserved because the execution is not
on the consumption wire.

**Execution failure is a loud HIGH-severity EVAL failure.** An exception or error from
`execute_from_config` is NOT collapsed into a quiet "comparator skipped" note. It is
surfaced as a distinct, loudly-reported failure item with severity HIGH.

### Path B — Routing-Correctness (Resolve-Only)

A router resolve-only mode: given a query, return which skill + tier WOULD be selected,
without executing anything. No side effects. No skill output. No artifact produced.

When invoked by the internal EVAL path, this mode considers skills in INGEST-or-later
state. The default public consumption path (`/api/v1/ask` with no special mode) is
UNCHANGED — it continues to return only promoted skills.

Path B is used for two assertion types:
1. **Positive routing assertions**: queries that SHOULD route to this skill (assert
   `resolved_skill_id == this_skill_id` and `tier == 1`).
2. **Negative routing assertions**: queries that should NOT route to this skill (assert
   `resolved_skill_id != this_skill_id`).

The candidate queries for both types are generated and curated in the new interactive
EVAL sub-step (see §Candidate Query Sub-step below).

### Candidate Query Sub-step

Before the route test, an LLM generates:
- N plausible end-user phrasings for the skill's intent (positive: SHOULD route here)
- A smaller set of negative/out-of-scope queries (SHOULD NOT route here)

These are shown to the author to edit, add, remove, and confirm. The curated sets are
then used for Path B routing assertions.

**Full EVAL ordering:**
1. Curate candidate queries (positive + negative sets, author-confirmed)
2. Path B routing assertions (positive set: assert routes to this skill; negative set:
   assert does not route to this skill)
3. Path A execution via `execute_from_config` (produces candidate artifact)
4. ADR-029 comparator (candidate artifact vs ADR-035 bound reference)
5. Consolidated gap report (three distinct outcome sections — see §Distinct Reporting)

### Distinct Loud Reporting (No Silent Degradation)

EVAL reports THREE separable outcome categories, each mandatory and distinct:

1. **Routing result per query (Path B)**: Per-query pass/fail. Failure = the skill would
   not be reached by real users. Reported loudly; must NOT be collapsed into a soft note.

2. **Execution success/failure (Path A)**: Binary. Failure is a HIGH-severity EVAL failure.
   Must NOT be reported as "comparator skipped" or any other euphemism. An execution
   failure surfaces the exception/error explicitly.

3. **Comparator structure/density scores (ADR-029)**: Structural comparison result.
   Skipped ONLY when Path A execution failed (legitimate, loudly-noted skip) or when no
   bound reference artifact exists (ADR-035 `has_bound_reference_artifact() = False`,
   also loudly noted). Never silently skipped.

The current behavior — collapsing "no artifact produced" into a soft diagnostic — is the
explicit anti-pattern being fixed.

### Interim Scoping — No AUTH Layer

**Known limitation, mandatory future re-scope.**

Path B ideally limits non-promoted skill visibility to the requesting author's OWN skills.
However, no AUTH/identity layer exists in the system today (spec §8 ACLs-are-v2;
`persona_visibility` and `classification` are placeholders on ContentItem per CLAUDE.md).

**Interim compromise**: Path B considers ALL INGEST-or-later skills (not scoped by author).
This is acceptable only because:
- (a) Path B does not execute or return skill output — only a routing decision
- (b) No identity layer exists to scope by today

This compromise MUST be revisited when the AUTH layer is built. The tightening:
Path B should consider only skills where `owner == requesting_author` (and optionally
filtered by `persona_visibility`). A dedicated AUTH ADR is the prerequisite for this
tightening — do NOT design the AUTH layer in ADR-038.

The same absent AUTH layer also gates the write-action roadmap in ADR-037.

---

## Decision

**Option 3: Path A internal-only execution via `execute_from_config` + Path B route-dry-run
considering INGEST-or-later skills with the default consumer routing path unchanged +
interim all-INGEST-or-later skills scoping (no auth) with mandatory re-scope when AUTH
layer is built.**

### Principles Established (Standing Practice)

1. **EVAL execution is internal-only.** `execute_from_config` inside the authoring runtime
   is not the same as exposing an execute flag on the public wire.

2. **Route dry-run is separate from execution.** A resolve-only mode that returns only a
   routing decision is safe to run against a broader skill set without violating the
   consumer-isolation invariant, because no output is produced.

3. **INGEST-or-later is the execution floor.** Pre-INGEST execution is prohibited.

4. **Three distinct outcome categories are mandatory.** Collapsing any category into a soft
   note is a bug, not a feature.

5. **Interim AUTH gap is documented, not hidden.** The absence of an AUTH layer is a
   first-class known dependency. It must be tracked against the AUTH ADR (to be filed).

---

## Consequences

- `_run_eval` is reworked substantially (see ADR-038 for implementation design)
- Router gains a resolve-only invocation mode (no public flag; invoked by EVAL internally)
- New EVAL sub-step: candidate query generation + author curation (new LLM prompt)
- `structure_score` will no longer be always-null for in-authoring skills
- AUTH ADR becomes a dependency for scoping Path B to author-owned skills
- No changes to the default `/api/v1/ask` consumption path

---

## Related

- **ADR-038** — implementation design (proposed; pending user approval)
- **ADR-033** — `WorkflowExecutor.execute_from_config`; promoted-only `all_cards()`
- **ADR-035** — `has_bound_reference_artifact()` single truth; comparator gate
- **ADR-029** — structural comparator; candidate vs reference artifact
- **ADR-037** — write-action roadmap; AUTH layer dependency (shared gap)
- **BUG-queue-2ad9a** (commit 8c2bec1) — formal record of promoted-only routing defect
- **DECISION-013** — agent-discovered defect channel; severity classification rules
- **spec §8** — open problems; ACLs-v2 as a v2 concern

---

*See also DECISION-015 (FSM access gate), DECISION-016 (connector model scope).*
