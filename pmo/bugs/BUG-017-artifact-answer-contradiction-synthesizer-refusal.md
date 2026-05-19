# BUG-017: artifact_url skill returns a synthesizer REFUSAL next to a valid artifact_path (internally contradictory response)

**Queue ID**: BUG-queue-decision013
**Status**: FIXED
**Severity**: HIGH (every artifact_url skill where the tier-1 synthesizer declines the "generate X" request returns a response that tells the user it failed while a real artifact was delivered)
**Session**: 2026-05-19 authorSkill PROMOTE mission
**Filed**: 2026-05-19
**Fixed in**: `framework/deploy/routes/ask.py` — `maybe_render_artifact` backfill guard
**Discovered by**: live mcp_server askKnowledgeBase id 251, skill `faaas_kiwi_project_pptx` (immediately after BUG-016 fix unblocked the render path)

---

## Symptom

askKnowledgeBase id 251 returned a single, internally contradictory payload:

- `artifact_path` = `/Users/.../faaas_kiwi_project_pptx.pptx` (a real 30 KB OOXML
  pptx), `citations[]` with the Confluence page at relevance 1.0 → **success**.
- `answer.Answer` = *"I cannot generate or provide a project tracking PPTX file
  for the FAaaS Kiwi Project based on the provided information… You may need to
  manually create the presentation…"*, `answer.Citations` = "(no support in
  retrieved context)" → **failure**.

A consumer reading `answer` concludes the request failed, despite a delivered
artifact.

---

## Root Cause

`ContextBuilder` tier-1 passage synthesis runs in `ctx.answer()` BEFORE
`maybe_render_artifact`. The synthesizer LLM is handed the retrieved page text
plus the question ("Generate a project tracking PPTX…") and, acting as a text
Q&A synthesizer, correctly states that *it* cannot produce a PPTX — a confident
refusal sentence. `maybe_render_artifact` then runs the WorkflowExecutor, which
DOES render the pptx and sets `artifact_path` + citations.

The backfill guard ([ask.py](../../framework/deploy/routes/ask.py),
`maybe_render_artifact`) only replaced the inline answer when it was **empty**
or matched the **`"(no relevant context found)"` sentinel**. A verbose LLM
refusal ("I cannot generate…") matches neither, so `_needs_backfill` stayed
`False` and the misleading refusal was surfaced next to the valid artifact.
Same class as the original BUG-016 backfill bug, but the trigger (a confident
refusal sentence vs. the empty/sentinel case) slipped past the heuristic.

---

## Fix

Broadened the backfill trigger with a **refusal-class** detector, gated on a
delivered artifact:

- Compute `_artifact_delivered = bool(art_path)` (this block only runs on a
  successful `exec_result`, so a path/url means the executor delivered).
- Detect refusal/inability lead-ins (`i cannot`, `i can't`, `i am unable`,
  `unable to generate/create/provide`, `cannot generate/create/provide`, …)
  within the first 200 chars of the answer text.
- `_needs_backfill` now also fires when `(_artifact_delivered and _is_refusal)`,
  **in addition to** the existing empty/sentinel conditions.

A GENUINE synthesized summary is still preserved — the override is limited to
the refusal class, never real content. (`test_real_upstream_answer_is_preserved`
remains green; new `test_synthesizer_refusal_with_artifact_is_backfilled`
locks the fix.)

---

## Verification

- Logic repro against the exact id-251 payload + 7 edge cases (genuine summary
  preserved; refusal w/o artifact not forced; empty/sentinel unchanged;
  "Unfortunately, I cannot create…" mid-lead refusal caught) — all pass.
- `TestAnswerBackfill` (4 tests incl. new BUG-017 case) green.
- ask-route / serialization regression set: 201 passed.
- Full unit suite: pre-existing 8-failure baseline only (confirmed identical on
  stashed HEAD for BUG-016) — **zero new regressions**.

---

## Related

- BUG-016: same flow; its fix unblocked the render path that exposed this.
- DECISION-023 (HELD): the `"• (none)"` bullet seen in the same artifact is
  extraction/fidelity scope, NOT this bug.
- DECISION-013: every fix files a bug record.
- Follow-on note (separate, not fixed): `cost_tokens` all-zero on the executor
  render path — telemetry not threaded; tracked, low severity.
