# DECISION-018: Consumer-Facing Card at DESIGN_SKILL + Routing Self-Test

**Status**: DECIDED
**Date**: 2026-05-17
**Decided by**: User (directive given 2026-05-17 as amendment to ADR-038/DECISION-017)
**Informed by**: Static card overwrite defect (BUG-queue-2ad9b); EVAL always-null structure_score (BUG-queue-5f2a1); ADR-038 original design (separate query-generation step at EVAL time)
**Implements**: ADR-038 (updates and extends DECISION-017)

---

## Context

ADR-038 original design (before this decision) proposed a separate `eval_candidate_query_generation` LLM prompt and an interactive curation sub-step at EVAL time. This would add one extra human loop at a time when the author is focused on evaluating results, not re-designing the card.

Separately, investigation found that `synthesize_workflow._build_skill_card()` was unconditionally overwriting any LLM-generated card with a static authoring-intent template, causing routing misses in production (BUG-queue-2ad9b, HIGH severity).

These two concerns are addressed together: the card and its routing queries belong at DESIGN_SKILL time — curated by the author as part of the design review — not regenerated at EVAL time.

---

## Eight Locked Components (Fully Decided — No Re-Litigation)

### A. Card Generated at DESIGN_SKILL (LLM)

A new `design_skill_card` LLM call (ADR-030 externalized prompt, `skill_builder.yaml`) generates a consumer-facing card at the end of `_run_design_skill()`. The card contains:

- `summary`: what the skill does at runtime
- `use_when`: when a consumer should invoke it
- `example_invocations`: 2-3 consumer-phrased examples (output_format token included)
- `routing_queries.positive`: 5 queries that SHOULD route to this skill
- `routing_queries.negative`: 3 queries that MUST NOT route to this skill

The card describes RUNTIME behavior from the consumer perspective — NOT "create a skill that…".

### B. synthesize_workflow MUST NOT Overwrite the Card (LOAD-BEARING)

`_synthesize_preview()` explicitly replaces `wf_struct["skill_card"]` with `self._data.design_skill_card` after calling `synthesize_workflow_skill()`. The static `_build_skill_card()` template is NOT called after the DESIGN_SKILL card exists. `routing_queries` is preserved in the committed ADB artifact. Violating this silently regresses routing.

### C. must_show_human Gate at DESIGN Time

After card generation, `_run_design_skill()` returns `_prompt_review_skill_card()` — a turn with `must_show_human=True` and `awaiting_user=True`. The author reviews, optionally edits (JSON), and confirms ("ok"/"yes"/"confirm") before REVIEW_DESIGN. Persisted via `to_dict()`/`from_dict()` round-trip.

### D. routing_queries as Tier-1 Classifier Signal

`ShimWorkflows.render_for_persona_prompt()` injects `routing_queries.positive` (up to 5) per-skill. Negative queries are EVAL-only. The ADR-033 promoted-only `all_cards()` invariant (BUG-queue-2ad9a protection) is unchanged.

### E. EVAL Path B Self-Test (curated positives/negatives from card)

`_run_eval` uses `ShimWorkflows.resolve_only(query, scope="ingest_or_later")` to test each `routing_queries` entry without executing the skill. Positive must resolve to this skill at tier 1; negative must NOT resolve to this skill. `resolve_only` does not modify `all_cards()`.

### F. PROMOTE is a HARD BLOCKER on Routing Self-Test Failure

In `_handle_eval_response()`, if `path_b_ran and not routing_self_test_passed`: PROMOTE is refused. Turn: `must_show_human=True`, message shows failing assertions, options are "ship as draft"/"review design"/"stop here". No override.

### G. No Migration for Already-Promoted Skills

Skills promoted before ADR-038 lack `routing_queries` in `skill_card`. No migration. The classifier degrades gracefully to existing `summary + use_when + example_invocations` signal.

### H. Path A Unchanged — In-Process Execution + Three-Section Report

`WorkflowExecutor.execute_from_config` for Path A. Three-section report always in turn message (Section 1: Routing, Section 2: Execution, Section 3: Comparator). Pre-INGEST = loud RuntimeError. Execution failure = `[HIGH]`.

---

## Answered Forks (Locked)

**Fork 1 — Where to generate routing queries: EVAL or DESIGN?**
DESIGN_SKILL. Rationale: (a) author knows the intent at design time; (b) queries become part of the committed artifact and persist across EVAL re-runs; (c) avoids a redundant interactive turn at EVAL. The original ADR-038 proposal (EVAL-time query generation) is superseded.

**Fork 2 — Hard block or warning on routing self-test failure?**
Hard PROMOTE block. No override. Rationale: a skill that cannot be routed to by consumers has zero value in production. A warning would be ignored. The PROMOTE gate is the last enforcement point before production.

**Fork 3 — Migration of existing promoted skills?**
Document only. Rationale: (a) migrations are risky; (b) existing skills continue to work with graceful degradation; (c) authors can re-author the card via a design session update if needed. A migration script is explicitly NOT provided.

---

## Consequences

- Every new skill designed after this commit will have consumer-facing routing queries in its ADB artifact
- The Tier-1 classifier signal is richer (routing_queries.positive added alongside existing fields)
- PROMOTE is now gated on routing correctness — "routing-blind" promotions are impossible
- pmo/bugs: BUG-queue-2ad9b (routing-miss, HIGH, fixed), BUG-queue-5f2a1 (EVAL null, HIGH, fixed), BUG-queue-a3f7e (kb-cli KeyError, LOW, open)

---

*See ADR-038 for full technical design and implementation notes.*
*See DECISION-017 for the two-axis EVAL policy this extends.*
*See DECISION-013 for bug classification rules.*
