---
title: ADR-038 — EVAL Redesign: Internal Execute (Path A) + Route Dry-Run (Path B)
status: proposed
created: 2026-05-17
owner: architect
deciders: user
tags: [adr, eval, skill-builder, routing, workflow-executor, fsm, correctness]
related: [ADR-029, ADR-033, ADR-035, ADR-037, DECISION-017]
supersedes: ~
---

# ADR-038 — EVAL Redesign: Internal Execute (Path A) + Route Dry-Run (Path B)

## Status

**Proposed — 2026-05-17. Pending user approval before implementation.**

No implementation has been done. This document records the design for review.

---

## A. Context — The Defect Being Fixed

### A.1 Current `_run_eval` Behavior

`_run_eval` (conversation.py ~line 4210) validates the workflow axis by calling:

```
POST /api/v1/ask  { "query": <generic_canonical_question>, "persona": <persona> }
```

It reads `wf_tier` from the response and sets `skill_matched = (wf_tier == 1)` (~line 4675).
If `skill_matched` is True, it uses the returned `artifact_url` for the ADR-029 comparator.
The workflow-scoring block is ~lines 4420-4478.

### A.2 Root Cause

`shim_workflows.py` `all_cards()` (ADR-033, commit ee2740b) returns ONLY ADB-promoted skills.
A skill under authoring is at FSM state `EVAL`, which is BEFORE `PROMOTE`:

```
… PREVIEW_EXTRACTION → CONFIRM → COMMITTED → VALIDATE → INGEST → EVAL → PROMOTE → DONE
```

Because the skill is not promoted, `/api/v1/ask` never routes to it. Result:
- `wf_tier` is always 2 (fallback)
- `skill_matched` is always False
- `artifact_url` is always null
- The ADR-029 comparator is always skipped
- `structure_score` is always null during authoring EVAL

The current code reports this as a soft note. Per DECISION-013 §Severity and the
no-silent-degradation project rule, this is a HIGH-severity defect in the EVAL pipeline.

### A.3 Enabling Prior Art

**ADR-033** added `WorkflowExecutor.execute_from_config(cfg, inputs)` as a public in-process
method. This exists specifically as the hook for in-authoring execution without the promoted
router. ADR-038 uses it.

**ADR-035** established `has_bound_reference_artifact()` as the single truth for artifact
binding. `_run_eval`'s comparator gate already calls this method. ADR-038 preserves this.

**ADR-029** defines the structural comparator (candidate artifact vs bound reference).
ADR-038 extends the pipeline to reliably produce a candidate artifact.

### A.4 INGEST-or-Later Floor

A skill's KB is ingested at the `INGEST` state. Executing the skill before INGEST is
meaningless — the knowledge base does not exist. Both axes in ADR-038 require the skill
to be at INGEST-or-later state. FSM order naturally enforces this for the `EVAL` state, but
the gate is checked explicitly for defense against future FSM changes.

---

## B. Decision

### B.1 Two Orthogonal EVAL Axes

EVAL splits workflow validation into two independent axes per DECISION-017:

| Axis | Name | Mechanism | Exposure | Produces |
|---|---|---|---|---|
| Path A | Execution-fidelity | `WorkflowExecutor.execute_from_config(cfg, inputs)` | Internal only | Candidate artifact |
| Path B | Routing-correctness | Router resolve-only mode (INGEST+ scope) | Internal EVAL path only; default consumption unchanged | Routing decision only |

### B.2 Path A — Internal Execution

**Mechanism**: `_run_eval` calls `WorkflowExecutor.execute_from_config(cfg, inputs)` with:
- `cfg`: the session's committed/design config (the config that will be promoted if the
  skill passes EVAL)
- `inputs`: the session's real configured inputs — e.g., the bound Confluence page(s),
  the bound-reference artifact context

This produces a candidate artifact in-process. The candidate artifact is handed directly
to the ADR-029 comparator.

**Public exposure**: None. This call is made inside `_run_eval` in the authoring/EVAL
runtime. It is not accessible via any HTTP flag, query parameter, or endpoint. It does
not appear on the public `/api/v1/ask` wire. The ADR-033 invariant — "drafts never
consumable by real consumers" — is preserved because the execution is not on the
consumption wire. Promotability is still gated on EVAL passing, which now actually runs.

**INGEST-or-later gate**: `_run_eval` checks that the skill's FSM state is at INGEST
or later before calling `execute_from_config`. If the check fails, `_run_eval` raises a
loud error and halts. It does NOT silently skip.

**Execution failure is HIGH-severity**: If `execute_from_config` raises an exception or
returns an error, `_run_eval` surfaces the full error as a HIGH-severity EVAL failure
item. It is NOT caught and collapsed into "comparator skipped." The consolidated gap report
(§B.5) includes the execution failure prominently.

**Hot-reload safety**: `execute_from_config` is called by reference at runtime; no import
path changes are needed. If a new prompt is added to the execution path, it loads on the
next call without restarting the authoring session (existing hot-reload conventions apply).

### B.3 Path B — Router Resolve-Only Mode

**Mechanism**: A new invocation mode on the router:

```
resolve_only(query, scope="ingest_or_later") → { skill_id, skill_name, tier, confidence, matched }
```

This mode:
- Evaluates routing logic (embedding similarity, tier scoring) for the query
- Returns which skill + tier WOULD be selected
- Does NOT call the skill, does NOT produce any output, does NOT modify any state
- Considers skills in INGEST-or-later state when `scope="ingest_or_later"` (used by EVAL)
- Returns only a routing decision record

**Default consumption path unchanged**: The default `/api/v1/ask` path continues to call
`all_cards()` which returns only promoted skills. The resolve-only mode is a separate
invocation path, not a flag on the existing consumption path. Passing any parameter to the
existing `/api/v1/ask` endpoint does NOT trigger resolve-only behavior.

**Positive and negative assertions**:
- Positive: `resolve_only(query)` for queries in the positive set → assert
  `resolved.skill_id == this_skill_id` and `resolved.tier == 1`
- Negative: `resolve_only(query)` for queries in the negative set → assert
  `resolved.skill_id != this_skill_id`

**Per-query reporting**: Each assertion produces a pass/fail result with the resolved
skill name and tier. Failures are reported loudly in the consolidated gap report.

**Interim scoping — no AUTH layer (known limitation)**:

Path B considers ALL skills in INGEST-or-later state when invoked by the EVAL path.
Ideally this would be limited to the requesting author's own non-promoted skills, but no
AUTH/identity layer exists yet (spec §8 ACLs-v2; `persona_visibility` and `classification`
are placeholders per CLAUDE.md).

This interim compromise is acceptable ONLY because:
- (a) Path B does not execute or return skill output — only a routing decision
- (b) There is no identity layer to scope by today

**This MUST be tightened when the AUTH layer is built**: at that point, resolve-only EVAL
scope should be limited to `owner == requesting_author` (and filtered by
`persona_visibility` as appropriate). A dedicated AUTH ADR is the prerequisite. Do NOT
design the AUTH layer here.

The same AUTH-layer absence also gates the write-action roadmap in ADR-037.

### B.4 Candidate Query Sub-step (New Interactive EVAL Step)

Before Path B runs, a new sub-step generates and curates candidate routing queries.

**LLM prompt**: Given the skill's `intent`, `task_description`, `persona`, and
`output_kind`, the LLM generates:
- N positive phrasings: plausible end-user questions that SHOULD route to this skill
  (default N = 5; configurable via prompt YAML if externalized per ADR-030)
- M negative/out-of-scope phrasings: questions that should NOT route to this skill
  (default M = 2-3)

The prompt is a new externalized prompt (ADR-030 convention: stored as a named prompt
YAML alongside the existing authoring prompts; hot-reload-safe). Prompt YAML key:
`eval_candidate_query_generation`.

**Author interaction**:
The generated candidate sets are shown to the author with edit affordances:
- Add a query to either set
- Remove a query from either set
- Edit the text of a query
- Confirm the curated sets to proceed

The author-confirmed sets are stored on the session for the duration of the EVAL run.
They are NOT persisted across EVAL re-runs (each EVAL re-run regenerates and re-curates).

**Ordering constraint**: The curation step precedes Path B. Path B does not run until the
author confirms the curated sets.

### B.5 Full EVAL Ordering

```
1. INGEST-or-later gate check (hard fail if not met)
2. Candidate query generation (LLM) + author curation (interactive)
3. Path B — routing assertions
   a. Positive set: assert each query resolves to this skill at tier 1
   b. Negative set: assert each query does NOT resolve to this skill
4. Path A — in-process execution via execute_from_config
   (execution failure → HIGH-severity; halt comparator step with loud report)
5. ADR-029 comparator — candidate artifact vs ADR-035 bound reference
   (skipped loudly if: execution failed in step 4, OR has_bound_reference_artifact() = False)
6. Consolidated gap report (three distinct sections — see §B.6)
```

### B.6 Distinct Loud Reporting — Three Mandatory Sections

The consolidated gap report has three separable sections. Each is always present in the
report, even if the content is "N/A" with an explicit reason. Silent omission of any
section is prohibited.

**Section 1 — Routing Results (Path B)**

```
ROUTING ASSERTIONS
  Positive queries tested: {count}
    PASS: {query} → {skill_name} tier 1
    FAIL: {query} → {resolved_skill_name} tier {tier}  [HIGH]
  Negative queries tested: {count}
    PASS: {query} → {other_skill_name} (not this skill)
    FAIL: {query} → {this_skill_name} tier {tier}  [HIGH]
```

**Section 2 — Execution Result (Path A)**

```
EXECUTION
  Status: SUCCESS | FAILURE [HIGH]
  (on FAILURE) Error: {exception type and message}
  (on SUCCESS) Artifact produced: {artifact_id or descriptor}
```

**Section 3 — Comparator Scores (ADR-029)**

```
COMPARATOR
  Status: RAN | SKIPPED
  (if SKIPPED) Reason: execution failed | no bound reference artifact
  (if RAN) structure_score: {value}  density_score: {value}
            Gap items: {list from ADR-029 output}
```

The current behavior — collapsing "no artifact" into a single soft note and marking
`skill_matched = False` without distinguishing cause — is the explicit anti-pattern being
replaced by this three-section report.

---

## C. Non-Goals (Explicit)

These are out of scope for ADR-038 and must NOT be added during implementation:

1. **No AUTH/identity layer.** Path B interim scoping (all INGEST-or-later skills) is
   acceptable as-is until a dedicated AUTH ADR establishes the identity model.

2. **No public execute flag.** `execute_from_config` is not exposed via HTTP in any form.
   No `?execute_draft=true` or equivalent parameter on any endpoint.

3. **No changes to default consumer routing.** `/api/v1/ask` with no special invocation
   mode continues to use `all_cards()` (promoted-only). This is unchanged.

4. **No new FSM states.** EVAL remains a single FSM state. The sub-steps (query curation,
   Path B, Path A, comparator) are sub-steps within EVAL, not new FSM states.

5. **No redesign of the comparator algorithm.** ADR-029 comparator runs as-is. ADR-038
   only ensures it reliably receives a candidate artifact.

---

## D. Consequences

### Positive

- `structure_score` is no longer always-null during authoring EVAL
- Routing coverage is added without putting drafts on the consumption wire
- Three distinct outcome categories replace the current single soft note
- Execution failures are surfaced loudly as HIGH-severity rather than silently skipped
- The ADR-033 consumer-isolation invariant is fully preserved
- The ADR-035 single-truth artifact check is preserved and used correctly
- Author gains interactive control over which queries test their skill's routing

### Negative / Tradeoffs

- `_run_eval` rework is substantial; the method grows in complexity
- A new LLM prompt (eval_candidate_query_generation) is added to the authoring prompt
  surface — must be externalized per ADR-030 conventions on first implementation
- The candidate query curation sub-step adds an interactive turn before the route test,
  increasing EVAL session length by one human-loop round
- Path B in interim form considers all INGEST-or-later skills (not author-scoped) — this
  is the known AUTH-layer gap documented above and in DECISION-017

### Reversibility

- Path A (execute_from_config call) can be reverted to the HTTP /ask call without any
  schema changes; it is a runtime call-site change
- Path B (resolve-only mode) is additive to the router; removing it reverts to the
  current test-less behavior
- The candidate query curation step is a new LLM prompt + author interaction; removing
  it drops the step and runs Path B with no queries (effectively disabling Path B)

---

## E. Alternatives Considered

### E.1 Execute-Direct Only (Path A alone)

Replace HTTP `POST /api/v1/ask` with `execute_from_config`. No resolve-only mode.

**Rejected**: Provides execution coverage but zero routing-correctness coverage. A skill
that executes correctly could still be unreachable by real users due to routing configuration
issues. DECISION-017 establishes that both axes are required.

### E.2 Privileged Public Flags on /api/v1/ask

Add `?include_unpromoted=true` and `?dry_run_only=true` HTTP flags.

**Rejected**: `include_unpromoted=true` puts in-authoring skill execution on the public
consumption wire. Any misconfigured consumer or caller passing the flag gets draft output.
This directly violates the ADR-033 consumer-isolation invariant. The invariant is
foundational (established in response to a class of silent-wrong-output bugs) and must
not be weakened.

---

## F. Implementation Notes

### Files to Change (guidance for backend dev)

- `framework/skill_builder/conversation.py`
  - `_run_eval`: remove HTTP `POST /api/v1/ask` call; add Path A (`execute_from_config`)
    and Path B (resolve-only); add INGEST-or-later gate; add three-section report
  - Add candidate query curation sub-step as a new handler method called from `_run_eval`
  - Preserve `has_bound_reference_artifact()` gate before comparator (ADR-035)
- `framework/orchestrator/shim_workflows.py` (or router module)
  - Add `resolve_only(query, scope)` method; scope parameter accepts `"promoted_only"`
    (default, existing behavior) or `"ingest_or_later"` (used by EVAL Path B)
  - `all_cards()` is NOT modified; default routing is NOT changed
- `framework/prompts/eval_candidate_query_generation.yaml` (new, ADR-030 convention)
  - New externalized prompt for candidate query generation; hot-reload-safe

### No Changes To

- `api/openapi.yaml` — no new public endpoints
- Default `/api/v1/ask` handler — consumer routing unchanged
- `all_cards()` in `shim_workflows.py` — promoted-only behavior unchanged
- ADR-029 comparator algorithm — runs as-is

### Test Strategy

1. **Path B positive routing**: given a skill at INGEST-or-later state, a well-matched
   query resolves to that skill at tier 1 in resolve-only mode
2. **Path B negative routing**: a query for a different topic does NOT resolve to the
   skill under test
3. **Path A execution failure is loud**: mock `execute_from_config` to raise; assert
   the gap report shows HIGH-severity execution failure (not "comparator skipped")
4. **Path A success feeds comparator**: mock `execute_from_config` to return an artifact;
   assert ADR-029 comparator is called with that artifact
5. **INGEST floor gate**: attempt EVAL on a skill at COMMITTED state; assert hard failure
   (not silent skip)
6. **Resolve-only does not alter consumption path**: calling `resolve_only` does not
   modify `all_cards()` output; a subsequent `/api/v1/ask` call sees only promoted skills
7. **Three-section report always present**: even when Path A fails, the report has all
   three sections (routing results, execution failure, comparator skipped-with-reason)
8. **Candidate query curation**: mock LLM to return N positive + M negative queries;
   assert author can add/remove/edit before proceeding to Path B

---

## G. Cross-References

- **ADR-033** — `WorkflowExecutor.execute_from_config`; foundational consumer-isolation
  invariant (`all_cards()` = promoted-only); commit ee2740b
- **ADR-035** — `has_bound_reference_artifact()` single truth; atomic bind/clear
- **ADR-029** — structural comparator; candidate vs reference artifact pipeline
- **ADR-030** — prompt externalization conventions (governs the new prompt YAML)
- **ADR-037** — write-action roadmap; AUTH-layer dependency (shared with Path B scoping)
- **DECISION-017** — the policy decision this ADR implements
- **BUG-queue-2ad9a** (commit 8c2bec1) — formal record of the promoted-only routing defect
- **DECISION-013** — severity classification rules (HIGH = loud, not soft note)
- **spec §8** — open problems; ACLs-v2 listed as a v2 concern
