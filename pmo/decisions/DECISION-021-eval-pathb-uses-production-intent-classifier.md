# DECISION-021: EVAL Path-B Routing Self-Test Must Use the Production IntentClassifier

**Status**: Accepted
**Date**: 2026-05-18
**Decided by**: User (authorized design+implementation in same session)
**Informed by**: 2026-05-18 routing-precision loop analysis — token-overlap `ShimWorkflows.resolve_only` is the root cause of repeated Mango/Kiwi and single-fact/agenda false-fails at the EVAL PROMOTE gate
**Amends**: ADR-038 §B.5 (Path-B routing decision mechanism)
**Cross-references**: DECISION-017, DECISION-018, ADR-038, ADR-033, BUG-queue-2ad9a

---

## Context

ADR-038 §B.5 specified that EVAL Path-B routing self-test uses `ShimWorkflows.resolve_only(q, scope="ingest_or_later")` — a token-overlap scorer against card signal fields. DECISION-017 correctly rejected public HTTP flags on `/api/v1/ask` to avoid silent-wrong-output via draft-skill consumption. ADR-038 §B.5 then conflated "no public flag" with "cheap token heuristic" — treating them as the only two options. This was an architectural gap, not a product decision.

Consequence: the EVAL PROMOTE gate has been perpetually stuck in a routing-precision loop. Token-overlap cannot distinguish:

- "What is the Mango Project RAG status?" vs. "What is the Kiwi Project RAG status?" — shared project-name vocabulary inflates scores for both skills regardless of which one is being tested.
- "What is the RAG status?" (single-fact, Tier-2 KB retrieval) vs. "Generate a weekly exec status email for the project" (Tier-1 workflow invocation) — shared domain vocabulary scores both at similar token-overlap.

The production `/api/v1/ask` consumption path uses `IntentClassifier` (LLM gpt-4o via `_classify_llm`, with `_classify_stub` fallback when LLM provider is not oci_genai/openai_direct). This is the mechanism that determines whether a real consumer query reaches a skill. EVAL Path-B self-test must use the SAME mechanism — otherwise it certifies routing under a non-production heuristic, and the PROMOTE gate is meaningless.

---

## Options Considered

### Option A — Keep token-overlap `resolve_only` (status quo)

`ShimWorkflows.resolve_only` with negative-penalty and `do_not_invoke_if_phrases` hard-veto continues to be the Path-B decision mechanism.

**Pros**: No LLM call at EVAL time. Fully deterministic.

**Cons**: Structurally cannot distinguish shared-vocabulary cases (Mango vs. Kiwi; single-fact vs. agenda-email). The routing-precision loop has produced 3+ separate patches (`do_not_invoke_if_phrases` prompt fix, negative-penalty scoring, improved prompt) with no exit condition. Token-overlap is not the mechanism production uses. Rejected.

### Option B — Public HTTP flag on /api/v1/ask (e.g. `?include_unpromoted`)

Expose a flag so EVAL Path-B can call `/api/v1/ask` with the in-authoring skill included in the candidate set.

**Rejected by DECISION-017**: public flag means draft skills are reachable by production consumers = silent-wrong-output risk. This rejection STANDS. Not reconsidered here.

### Option C — Internal IntentClassifier call, INGEST+ scope, non-executing (CHOSEN)

EVAL Path-B constructs `IntentClassifier(self._llm, shim_faaas_instance)` internally — mirroring how `ContextBuilder.__init__` constructs it in `context_builder.py`. The candidate set for `available_workflows` is `ShimWorkflows.all_cards_including_draft()` filtered to INGEST-or-later skills — reusing the same enumeration that `resolve_only(scope="ingest_or_later")` used. The classifier's `.classify(q, available_workflows=ingest_plus_cards)` is called to get a routing decision. No skill is executed. Path-B pass/fail semantics, hard PROMOTE gate, and `must_show_human` gate are unchanged.

**Pros**:
- Production fidelity: EVAL Path-B and `/api/v1/ask` use the same routing brain.
- No public flag: the IntentClassifier is constructed internally; no new HTTP surface. DECISION-017 satisfied.
- Stub-mode honest parity: when `self._llm.provider` is not oci_genai/openai_direct, `IntentClassifier._classify_stub` runs — the same stub behavior production has in that config. This is documented, not hidden.
- Exits the routing-precision loop structurally: LLM classifier handles semantic disambiguation that token-overlap cannot.
- Candidate set `all_cards_including_draft()` (INGEST+) is already proven to enumerate in-authoring skills correctly.
- `do_not_invoke_if_phrases`, `routing_queries`, `do_not_use_for` card fields remain legitimate — they are now classifier prompt signal (injected via `render_for_persona_prompt`) instead of token-overlap operands.

**Cons**:
- One extra LLM call per positive + negative query at EVAL time (~5+3=8 calls). Acceptable: EVAL is already a multi-LLM-call operation.
- In stub mode, `_classify_stub` uses keyword/example matching — less precise than the real LLM, but this is correct parity with how production behaves on a stub LLM config.

---

## Decision

**Option C: EVAL Path-B uses `IntentClassifier` constructed internally from `self._llm`, with `all_cards_including_draft()` INGEST+ candidate set, non-executing.**

### Implementation contract

In `framework/skill_builder/conversation.py` `_run_eval` Path-B section:

- Replace `_shim.resolve_only(q, scope="ingest_or_later")` with `IntentClassifier(self._llm, shim_faaas_instance).classify(q, available_workflows=ingest_plus_cards)`.
- `shim_faaas_instance` is constructed from `REPO_ROOT / "framework/config/shim_faaas.yaml"` (the same file `ContextBuilder` uses via its `ShimFaaas` argument).
- `ingest_plus_cards` = `ShimWorkflows.all_cards_including_draft()` (no state filter needed — `all_cards_including_draft` already includes in-authoring skills).
- Positive query pass condition: `classification.tier == 1 AND classification.workflow_skill == skill_name` (same semantic as before).
- Negative query pass condition: `classification.tier != 1 OR classification.workflow_skill != skill_name` (same semantic as before).
- `ShimWorkflows.resolve_only` is NOT removed — it has direct-call tests that remain valid. Only the `_run_eval` call sites (lines 5130 and 5153) are repointed.
- Public consumption path (`/api/v1/ask`, `all_cards()` promoted-only) is UNCHANGED per ADR-033/BUG-queue-2ad9a.

### Stub note (documented, not hidden)

When `self._llm.provider not in ("oci_genai", "openai_direct")`, `IntentClassifier._classify_stub` runs. This uses keyword matching against `example_invocations` and KB `use_when` — less semantically precise than the real LLM, but it IS exactly the behavior production has in that LLM config. The goal is production parity, not "always LLM". In full laptop mode with OCI GenAI wired, the real `_classify_llm` runs.

---

## Consequences

- EVAL Path-B routing self-test now certifies the same routing mechanism production uses.
- The PROMOTE gate is meaningful: a skill that passes Path-B will route correctly in production (modulo non-stub LLM determinism).
- `do_not_invoke_if_phrases` / `routing_queries` / `do_not_use_for` card fields are retained — they now serve as classifier prompt signal via `render_for_persona_prompt` injection rather than token-overlap operands.
- Routing-precision loop exits structurally: no more heuristic patches needed for shared-vocabulary cases.
- `ShimWorkflows.resolve_only` remains in the codebase with its existing tests. It is dead code in `_run_eval` after this change.

---

## Standing Practice (going forward)

Any future change to the production routing mechanism (`IntentClassifier`) automatically affects EVAL Path-B fidelity. If the production classifier is replaced or significantly altered, the EVAL Path-B wiring must be updated in the same change.

---

*See also: DECISION-017 (public-flag rejection stands), ADR-038 (amended by this decision — §B.5 and component table), ADR-033 (promoted-only default path unchanged), DECISION-013 (bug channel — bug filed in ADB as part of this change).*
