---
title: ADR-038 — Consumer-Facing Card at DESIGN_SKILL + EVAL Internal Execute (Path A) + Route Dry-Run (Path B)
status: accepted
created: 2026-05-17
accepted: 2026-05-17
amended: 2026-05-18
owner: architect
deciders: user
tags: [adr, eval, skill-builder, routing, workflow-executor, fsm, correctness, card-gen, routing-queries, intent-classifier]
related: [ADR-029, ADR-030, ADR-033, ADR-035, ADR-037, DECISION-017, DECISION-018, DECISION-021]
supersedes: ~
---

# ADR-038 — Consumer-Facing Card at DESIGN_SKILL + EVAL Internal Execute (Path A) + Route Dry-Run (Path B)

## Status

**Accepted — 2026-05-17. Implemented in commit feat(authorskill+eval): consumer-facing card + routing_queries at DESIGN_SKILL (DECISION-018, ADR-038).**

**Amended — 2026-05-18 (DECISION-021):** §B.5 Path-B routing mechanism corrected. Token-overlap `ShimWorkflows.resolve_only` replaced by production `IntentClassifier` in `_run_eval`. See §B.5 and §D below for the updated contract.

**Implementation Note (2026-05-17 — Option B correction):** `_generate_design_skill_card()` MUST be called AFTER the main design LLM call and AFTER `self._data.output_format` is set from `design["workflow_shape"]["output_format"]`; calling it before (as the b1adf33 workaround did) means `output_format` is the user's pre-design guess (`normalised_intent.output_kind`) rather than the design-authoritative value, corrupting the card's `example_invocations`, `routing_queries.negative`, and the `output_format` token in all three fields — degrading Tier-1 routing discrimination. The call ordering in `_run_design_skill` is therefore load-bearing. Additionally, the ADR-028 persona-injection test (`test_persona_fragments_injected_in_run_design_skill`) MUST locate the design_skill LLM call by inspecting prompt content (`persona_key_fields` / `exec-safe`, tokens structurally absent from `design_skill_card`) rather than by call order (`call_args` = last call), so that this brittleness cannot recur if the two LLM calls are ever reordered again.

---

## A. Context — The Defects Being Fixed

### A.1 Static Card Overwrote DESIGN_SKILL Card (BUG-queue-2ad9b)

`synthesize_workflow._build_skill_card()` was called unconditionally in `synthesize_workflow_skill()`, which is called from `_synthesize_preview()`. This overwrote any LLM-generated `skill_card` with a static template using authoring-intent language ("This skill is designed to create…"). The static card produced low token-overlap scores in the Tier-1 classifier, causing real consumer queries to fall through to Tier-2. Severity: HIGH.

### A.2 EVAL Always-Null structure_score (BUG-queue-5f2a1)

`_run_eval` validated the workflow axis by calling `POST /api/v1/ask`. That endpoint calls `ShimWorkflows.all_cards()` which returns only ADB-promoted skills (ADR-033). A skill at EVAL state is not promoted, so it was invisible. Result: `wf_tier` always 2, `skill_matched` always False, `structure_score` always null. Severity: HIGH.

### A.3 Enabling Prior Art

- **ADR-033**: `WorkflowExecutor.execute_from_config(cfg, inputs)` — the in-process hook for authoring-time execution without the promoted router
- **ADR-035**: `has_bound_reference_artifact()` — single truth for artifact binding
- **ADR-029**: Structural comparator (candidate artifact vs bound reference)
- **ADR-030**: Prompt externalization conventions (hot-reload-safe YAML prompts)

---

## B. Decision — Eight Locked Components (DECISION-018)

### B.1 Component A — Consumer Card Generated at DESIGN_SKILL

A new LLM call runs at the end of `_run_design_skill()` using the externalized prompt `design_skill_card` (ADR-030 convention; stored in `framework/config/prompts/skill_builder.yaml`). The prompt generates a consumer-facing card:

```json
{
  "summary": "...",
  "use_when": "...",
  "example_invocations": ["...", "..."],
  "routing_queries": {
    "positive": ["...", "..."],
    "negative": ["...", "..."]
  }
}
```

The card describes what the skill DOES at runtime and when a consumer should invoke it — NOT how it was created. 5 positive routing queries, 3 negative. NOT a `failure_classifier`-style locked prompt.

### B.2 Component B — synthesize_workflow MUST NOT Clobber the Card (LOAD-BEARING)

`_synthesize_preview()` explicitly replaces `wf_struct["skill_card"]` with `self._data.design_skill_card` after `synthesize_workflow_skill()` returns. This ensures the static `_build_skill_card` template does NOT overwrite the LLM-generated card. The `routing_queries` sub-key is preserved through the full ADB artifact. This is load-bearing — violating it silently regresses routing.

### B.3 Component C — must_show_human Gate at DESIGN Time

After card generation, `_run_design_skill()` returns `_prompt_review_skill_card()` — an interactive turn with `must_show_human=True` and `awaiting_user=True`. The FSM surfaces the card to the author for review/edit/confirm before transitioning to REVIEW_DESIGN. JSON edits to the card are applied and the review turn is re-shown. Persisted via `to_dict()`/`from_dict()` round-trip.

### B.4 Component D — routing_queries as Tier-1 Classifier Signal

`ShimWorkflows.render_for_persona_prompt()` injects `routing_queries.positive` entries (up to 5) into the per-skill prompt block alongside `summary`, `use_when`, and `example_invocations`. Negative queries are NOT injected — they are for EVAL Path B self-test only. The ADR-033/BUG-queue-2ad9a invariant (`all_cards()` promoted-only default consumption path) is unchanged.

### B.5 Component E — EVAL Path B Self-Test

**Amended 2026-05-18 (DECISION-021):** Path-B now uses `IntentClassifier` — the production routing mechanism — instead of `ShimWorkflows.resolve_only` token-overlap scoring. The amendment closes the routing-precision loop: token-overlap cannot distinguish shared-vocabulary cases (e.g. Mango vs. Kiwi project, or single-fact vs. agenda-email queries). `ShimWorkflows.resolve_only` is retained in the codebase but is no longer called by `_run_eval`.

**Amended Path-B contract:**

Path B constructs `IntentClassifier(self._llm, shim_faaas_instance)` internally — mirroring `ContextBuilder.__init__` — and calls `.classify(q, available_workflows=ingest_plus_cards)` for each `routing_queries.positive` and `routing_queries.negative` entry without executing the skill:

- `shim_faaas_instance` is a `ShimFaaas` loaded from `REPO_ROOT / "framework/config/shim_faaas.yaml"`.
- `ingest_plus_cards` = `ShimWorkflows.all_cards_including_draft()` (the same INGEST+ enumeration `resolve_only(scope="ingest_or_later")` used).
- **Positive**: `classification.tier == 1 AND classification.workflow_skill == skill_name` must be true.
- **Negative**: `classification.tier != 1 OR classification.workflow_skill != skill_name` must be true.
- No skill is executed. No public HTTP flag. DECISION-017's public-flag rejection stands.
- `scope="ingest_or_later"` semantics preserved: candidate set includes INGEST-or-later (in-authoring) skills; promoted-only `all_cards()` is NOT used.
- When `self._llm.provider not in ("oci_genai", "openai_direct")`, `IntentClassifier._classify_stub` runs — the same stub behavior production has in that LLM config. This is correct parity, documented, not hidden.
- `do_not_invoke_if_phrases`, `routing_queries`, `do_not_use_for` card fields are retained. They are now classifier prompt signal (injected via `render_for_persona_prompt`) rather than token-overlap operands.

**Original (superseded by amendment):** ~~Path B used `ShimWorkflows.resolve_only(query, scope="ingest_or_later")` — token-overlap scoring against `routing_queries.positive + example_invocations + summary + use_when` with negative-token penalty and `do_not_invoke_if_phrases` hard-veto.~~

### B.6 Component F — PROMOTE is a HARD BLOCKER on Routing Self-Test Failure

In `_handle_eval_response()`, when `path_b_ran` and `routing_self_test_passed == False`, PROMOTE is refused. A `must_show_human=True` turn is returned listing the failing assertions. NO override is provided. The user may "ship as draft", "review design", or "stop here". This cannot be bypassed.

### B.7 Component G — No Migration for Already-Promoted Broken Skills

Skills promoted before ADR-038 do not have `routing_queries` in their `skill_card`. This is DOCUMENTED ONLY. No migration is run. The Tier-1 classifier gracefully degrades to the existing `summary + use_when + example_invocations` signal for cards without `routing_queries`.

### B.8 Component H — Path A Unchanged

In-process execution via `WorkflowExecutor.execute_from_config`. The three-section EVAL report always contains:

1. `=== SECTION 1: ROUTING ASSERTIONS (Path B) ===`
2. `=== SECTION 2: EXECUTION (Path A) ===`
3. `=== SECTION 3: COMPARATOR (ADR-029) ===`

Pre-INGEST skills: `_run_eval` raises `RuntimeError("EVAL: INGEST-or-later gate failed…")` with the entering FSM state. Execution failure is labeled `[HIGH]`. All three sections are always present even when Path A fails.

---

## C. INGEST-or-Later Floor

`_run_eval` captures `_entering_state` before mutating `self._state` to "EVAL". If `_entering_state in {"COMMITTED", "VALIDATE"}`, `_run_eval` raises a loud `RuntimeError` (not a silent skip). This guards against future FSM changes that might expose `_run_eval` before INGEST.

---

## D. New Method Surface

| Method | Where | Purpose |
|---|---|---|
| `_generate_design_skill_card()` | `conversation.py` | Call `design_skill_card` prompt; fallback static; store on `_data.design_skill_card` |
| `_prompt_review_skill_card()` | `conversation.py` | must_show_human turn for card review |
| `ShimWorkflows.resolve_only(q, scope)` | `shim_workflows.py` | Token-overlap routing decision without execution — **retained in codebase for its own tests; no longer called by `_run_eval` Path-B after DECISION-021 amendment** |
| `IntentClassifier(llm, shim_faaas).classify(q, available_workflows=...)` | `_run_eval` in `conversation.py` | **[Added by DECISION-021]** Production routing mechanism used for Path-B self-test; INGEST+ candidate set; non-executing |

New `_SessionData` fields (backward-compatible defaults):

```python
design_skill_card: dict | None = field(default=None)
routing_self_test_passed: bool | None = field(default=None)
```

New `PromptMeta` field (backward-compatible default):

```python
required_vars: List[str] = field(default_factory=list)
```

New externalized prompt (ADR-030):

```yaml
# framework/config/prompts/skill_builder.yaml
design_skill_card:
  id: design_skill_card
  version: "1.0"
  model: synthesis
  max_tokens: 1024
  response_format: json_object
  required_vars: [skill_name, persona, task_description, output_format, intent_summary]
```

---

## E. Non-Goals (Explicit)

1. No AUTH/identity layer — Path B considers all INGEST-or-later skills (interim)
2. No public execute flag — `execute_from_config` is not exposed via HTTP
3. No changes to default consumer routing — `/api/v1/ask` uses `all_cards()` (promoted-only)
4. No new FSM states
5. No redesign of ADR-029 comparator algorithm

---

## F. Consequences

### Positive

- `structure_score` is no longer always-null during authoring EVAL
- Consumer-facing cards replace authoring-intent text in the Tier-1 classifier signal
- Routing self-test gates PROMOTE on routing correctness — no more routing-blind promotions
- Hard PROMOTE gate prevents silent routing regressions reaching production
- No migration needed for existing skills (graceful degradation on missing `routing_queries`)
- EVAL gap report is now three distinct sections — no more single soft "comparator skipped" note

### Negative / Tradeoffs

- One extra interactive turn added to DESIGN_SKILL (card review)
- `_run_eval` grew in complexity (Path A + Path B + three-section report)
- Path B in interim form considers all INGEST-or-later skills (not author-scoped) — AUTH-layer gap from DECISION-017/ADR-037
- **[DECISION-021 amendment]** Path-B now incurs ~8 LLM calls (5 positive + 3 negative) at EVAL time. Acceptable cost for production-fidelity routing certification. In stub mode these are fast keyword-match calls.
- **[DECISION-021 amendment]** ~~Token-overlap `resolve_only` was the original Path-B mechanism.~~ `ShimWorkflows.resolve_only` is retained but is dead code in `_run_eval`.

### Reversibility

- Path A is a call-site change (execute_from_config vs HTTP call) — trivially reversible
- Path B is additive to the router — removing resolve_only reverts to test-less behavior
- Card generation is a new method called from _run_design_skill — removing the call reverts to static card

---

## G. Alternatives Considered

### G.1 Execute-Direct Only (Path A alone)

Rejected: no routing-correctness coverage (DECISION-017 requires both axes).

### G.2 Privileged Public Flags on /api/v1/ask

Rejected: `include_unpromoted=true` violates ADR-033 consumer-isolation invariant.

### G.3 Separate Query-Generation Prompt (EVAL sub-step)

The original ADR-038 proposed a separate `eval_candidate_query_generation` prompt and an interactive curation step at EVAL time. Replaced by DECISION-018: queries are generated at DESIGN_SKILL time, curated by the author at card review, and persisted in the ADB artifact. This avoids a redundant interactive turn at EVAL and ensures routing queries are part of the committed skill card.

---

## H. Cross-References

- **ADR-033** — `all_cards()` = promoted-only; `execute_from_config` hook; commit ee2740b
- **ADR-035** — `has_bound_reference_artifact()` single truth
- **ADR-029** — structural comparator; candidate vs reference artifact
- **ADR-030** — prompt externalization conventions (governs `design_skill_card` prompt)
- **ADR-037** — write-action roadmap; AUTH-layer dependency
- **DECISION-017** — routing-vs-execution two-axis policy
- **DECISION-018** — consumer-facing card + routing self-test (this implementation's governing decision)
- **BUG-queue-2ad9b** — static `_build_skill_card` routing-miss defect (fixed)
- **BUG-queue-5f2a1** — EVAL always-null structure_score (fixed)
- **BUG-queue-a3f7e** — kb-cli export-skills KeyError: extraction_schema (open)
- **DECISION-013** — severity classification + agent-discovery bug channel
- **DECISION-021** — EVAL Path-B routing self-test uses production `IntentClassifier` (amends §B.5 of this ADR)
