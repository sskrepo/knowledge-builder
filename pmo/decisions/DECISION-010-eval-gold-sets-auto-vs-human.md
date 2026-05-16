---
title: "DECISION-010 — EVAL gold sets: auto-generated vs human-curated"
status: superseded
created: 2026-05-14
superseded: 2026-05-15
owner: architect
deciders: user
outcome: "Option A — auto-generate gold rows from live samples; surface to user with kind=auto_generated"
superseded_by: "ADR-029 (Accepted, 2026-05-15) — terminal gate function only"
tags: [eval, gold-sets, quality]
related: [ADR-027, ADR-029]
---

> **SUPERSEDED (2026-05-15) — ADR-029 Accepted.**
> ADR-029 (Accepted) replaces the numeric recall@k + faithfulness TERMINAL GATE
> with user-acceptance as the gate. DECISION-010's Option A outcome survives for
> gold-row generation: auto-generated gold rows are RETAINED and continue to be
> computed, but they are demoted from "terminal gate" to "diagnostic signal" shown
> alongside the gap report in the EVAL turn. No code deletion — only the PROMOTE
> guard logic changes. Do NOT delete this file. See ADR-029 "Accepted decision"
> block for the full disposition.

# DECISION-010 — EVAL gold sets: auto-generated vs human-curated

## Context

When authorSkill reaches the EVAL state, it needs gold-set rows to score the
skill against. Prior to ADR-027, the eval harness was a stub that produced
null metrics and unconditionally advanced to PROMOTE. ADR-027 makes eval real.

The central question is: who provides the "expected" values in the gold rows?

## Options considered

### Option A — Auto-generate gold rows from live samples

At EVAL time:
1. Re-use the source samples cached at INSPECT_SOURCES (same LLM extraction
   already run at PREVIEW_EXTRACTION).
2. For each sample, run the extraction schema against it and treat the output
   as `expected_extraction`.
3. Freeze the source snippet alongside the expected values so future runs can
   replay against the same input (not a moving target).
4. Write rows with `kind="auto_generated"` to the gold JSONL files.
5. For workflow-level scoring: submit the standard query to `/api/v1/ask`,
   check that the skill routes correctly and returns the expected fields.
6. Score recall@k and faithfulness (LLM judge against frozen snippet).
7. Gate PROMOTE on exit thresholds (default 0.85/0.85).
8. Surface the rows and metrics to the user with an honest disclaimer:
   "kind=auto_generated — these were created from the same LLM that did the
   extraction, so they measure consistency, not correctness. Human review
   encouraged before promoting to production fleet-wide."

**Pros:**
- No human required — skills can be promoted in a single session.
- Auto-generated rows still provide real signal: they verify that the schema
  is extractable, that the workflow routes, that the output contains the
  declared fields.
- The `kind=auto_generated` flag makes the limitation explicit.
- Consistent with the "eval discipline is non-negotiable" rule in CLAUDE.md —
  something runs, something scores.

**Cons:**
- Circular: the LLM that designed the extraction also grades it.
- A consistently wrong extraction scores 100% recall/faithfulness.
- Not appropriate as a production quality gate for fleet-wide deployment.

### Option B — Human-curated gold rows (separate step)

A human fills in `expected_extraction` values by reading the real source and
writing down the correct answers. The eval harness compares the LLM extraction
against these human answers.

**Pros:**
- Catches cases where the LLM consistently hallucinates or misextracts.
- Appropriate for a production fleet-wide quality gate.

**Cons:**
- Blocks promotion until a human completes the review.
- Adds 1-5 days to the authoring cycle.
- Not available at session time.

### Option C — Hybrid: auto-generate, then require human sign-off before fleet promotion

Auto-generate for the first promotion (single-project use). Before fleet-wide
rollout, require a human to validate the gold rows and re-run eval.

**Pros:**
- Unblocks initial use while ensuring production quality.

**Cons:**
- Requires a second authoring ceremony.
- More complex workflow state machine.

## Decision

**Option A chosen** (confirmed by user, 2026-05-14).

Rationale: auto-generated gold is the pragmatic v1 path. The `kind=auto_generated`
flag is honest about the limitation. The open question below captures when to
require human validation.

## Open question (for future DECISION)

**When do we require human validation before fleet promotion?**

Current answer (v1): never required, always encouraged.

Candidate future trigger: when `fleet_scope` is set to "all" in the workflow
YAML's promotion metadata (meaning the skill will be used by all instances, not
just the authoring team). Fleet-wide promotion could require at least 5
`kind=human` gold rows with recall@k ≥ 0.90.

This is deferred to a future DECISION when the fleet-wide promotion concept is
more concretely defined.

## Implementation

Implemented in `framework/skill_builder/conversation.py::_run_eval` per ADR-027.
See `docs/wiki/adr/ADR-027-design-first-authorskill.md` for full implementation
spec.
