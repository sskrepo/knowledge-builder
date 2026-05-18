# DECISION-019: author_fixed Source Propagation Gap (RC1) + Layout-ID Resolution Gap (RC2)

**Status**: Accepted — all three root causes resolved (2026-05-17)
**Date**: 2026-05-17
**Raised by**: architect (post-investigation of junk-PPTX bug, request id 146)
**Skill under investigation**: `tpm.faaas_kiwi_project_pptx`, authoring session `synth-tpm-b518aab6`
**Related**: ADR-032 (RC1), ADR-034 (RC2), DECISION-014, ADR-038/DECISION-018, spec §8

---

## Accepted Decisions (2026-05-17)

All five items resolved in one coherent implementation pass (commit on branch `claude/vigilant-kare-a4d86f`):

1. **RC1 fix direction**: **Option A** — `synthesize_workflow_skill` now calls `derive_pinned_source(sources, source_samples)` and emits `source_binding: {mode: author_fixed, pinned_ref: ..., space_allow_list: [...], ingest_on_demand: false}`. Executor dispatches to `_retrieve_author_fixed_pinned()` for this mode, hard-fails (`ConfluencePageNotInKBError`) if the pinned page is not resolvable — never falls through to generic KB retrieval.

2. **RC2 fix direction**: **Option A** — `design_skill` prompt bumped to v1.3. `{layout_valid_ids}` injected at render time (from `layout_catalog.internal_ids()`). The valid ID enum appears ONLY in the OUTPUT SCHEMA CONSTRAINT section (DECISION-014 mitigation: no hardcoded IDs in reasoning rules). `_run_design_skill` in `conversation.py` validates the returned `workflow_shape.layout` against the catalog at design time and raises loud (`RuntimeError`) if a non-catalog value is returned.

3. **Finding B fix direction**: **Option A** — `PptxRenderer.render()` now raises `ValueError` (hard error, surfaced as `[HIGH]` executor failure) when `get_preset(layout)` returns `None`. No silent fallback to stub.

4. **Sequencing**: One-pass implementation (RC1 + RC2 + Finding-B implemented together).

5. **Backfill**: **No backfill**. Existing promoted `author_fixed` PPTX skills have no prose-layout artifacts (confirmed by ADB query: 6 promoted skills, all with `layout=None` — authored before ADR-034). Re-author on next use.

**Bug record**: `BUG-queue-b03d7` filed in `KB_SHIM.KBF_BUG_REPORTS` (2026-05-17).

**Re-author scope**: 0 existing promoted skills need immediate re-authoring (ADB query confirmed all 6 promoted `workflow_skill` artifacts have `layout=None` and `source_binding.mode=None`).

**Test coverage**: 28 new tests in `framework/tests/unit/test_decision019_fixes.py` (all pass). Full suite: 1550 passed, 8 failed (all pre-existing baseline failures), 0 new regressions.

---

## Context

Request id 146 invoked the promoted skill `tpm.faaas_kiwi_project_pptx` and received a junk 6-slide PPTX stub — a key/value metadata dump of the wrong Confluence page ("Project Plan", not "FAaaS Kiwi Project") — while the response reported `tier_used=1, confidence=0.95` and a success message confirming it "generated the FAaaS Kiwi project update PPTX."

Investigation was conducted against authoring session `synth-tpm-b518aab6` and the committed ADB artifact set (5 artifacts, `SYNTH_ID=synth-tpm-b518aab6`, created 2026-05-17 20:21:44, promoted 21:01:24).

### What Was Ruled Out

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Artifact corruption or version skew | RULED OUT | Single artifact set; no version conflict; committed `workflow_skill` artifact faithfully matches session design |
| Synthesis degradation (synthesize_workflow broke the schema) | RULED OUT | Committed skill schema matches what DESIGN_SKILL produced |
| Wrong page fetched at authoring / bad INSPECT | RULED OUT | Session `source_samples` had exactly one entry `confluence:https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project`, `space=OCIFACP`, `title="FAaaS Kiwi Project"`, `text_len=3987` — correct page, correctly fetched |
| DESIGN_SKILL produced a bad schema from the right page | RULED OUT | Design output matched the authored intent; the schema fields (slide_title, rag_summary, schedule_health, key_metrics, blockers, next_steps, exec_asks) were correctly designed |

### What Was Confirmed: Two Root Causes + One Amplifier

- **RC1** — `author_fixed` mode does not persist the fixed source into the runtime skill artifact; the runtime executor has no record of which Confluence page this skill was authored from.
- **RC2** — DESIGN_SKILL (after ADR-034) emits `workflow_shape.layout` as a prose sentence; the renderer dispatches on catalog `internal_id`; the prose never resolves to a valid preset ID; renderer falls back to the generic stub.
- **Finding B** — The executor/renderer emitted a confident success response instead of failing loud when the skill's contract was unsatisfiable; RC1+RC2 were amplified as silent degradation.

---

## RC1 — author_fixed Does Not Propagate the Fixed Source into the Runtime Skill

### Precise Statement

`source_binding_mode = 'author_fixed'` (the default for all pre-ADR-032 skills and for skills where the author confirms a fixed, non-parameterized source) is defined in ADR-032 §D.1 to emit NO `source_binding` block in the committed YAML artifact. This is correct by design: `source_binding` was introduced for `ask_parameterized` skills to communicate the per-request page reference at execution time.

However, the design has a contract gap: the fixed source page that was fetched and inspected during `INSPECT_SOURCES` / `DESIGN_SKILL` is **used only to design the extraction schema at author time and is then discarded**. Nothing persists, for the runtime executor, that this skill's fixed content source IS the FAaaS Kiwi Confluence page (`https://confluence.oraclecorp.com/confluence/display/OCIFACP/FAaaS+Kiwi+Project`). The committed `trigger.on_request.inputs` is the generic `{name: input, type: string}` — a free-text query. No page binding survives into the artifact.

At execution time, the skill has no bound source. The executor falls through to generic KB retrieval over the OCIFACP space, which returns the highest-scoring match ("Project Plan", a different OCIFACP page), not the intended "FAaaS Kiwi Project" page.

### Evidence

- Session `source_samples`: `confluence:https://…/OCIFACP/FAaaS+Kiwi+Project`, `title="FAaaS Kiwi Project"`, `text_len=3987`.
- Committed artifact: `source_binding: None`, `trigger.on_request.inputs = [{name: input, type: string}]`.
- Runtime request 146: executor retrieved "Project Plan" page (`Page Id / …/OCIFACP/Project+Plan`), not "FAaaS Kiwi Project".
- ADR-032 §D.1 explicitly states: absent `source_binding` block = `author_fixed`; `author_fixed` = no page binding in YAML. There is no `author_fixed` variant of the binding block that would pin a specific page.

### Blast Radius

**Likely affects every `author_fixed` skill that has an external fixed source (i.e., a Confluence page, Jira filter, or git ref identified at author time).** Any such skill whose fixed source page is not also the highest-scoring KB hit for the user's query will silently draw from the wrong content at runtime. The defect is silent: the skill executes, the executor finds _something_ in the KB, and synthesis proceeds.

Skills with only in-KB content (fixture-backed or wiki-backed) that happened to be the top hit may have been working by coincidence.

### Fix Options (no decision — user decides)

**Option A — author_fixed persists a runtime-resolvable fixed-source binding (pinned source ref in artifact).**

Introduce a `source_binding.mode: author_fixed` variant of the binding block (symmetric with `ask_parameterized`) that carries a pinned source reference (page URL or page ID) derived from `source_samples` at synthesis time. The executor, when it sees `source_binding.mode: author_fixed` with a pinned ref, resolves that specific page from the KB (or optionally fetches it on demand) before running retrieval — guaranteeing the right content.

- Pro: complete contract — the artifact is fully self-describing; executor is deterministic.
- Pro: consistent with `ask_parameterized` design; minimal new surface area.
- Con: requires synthesizer change (emit the block), executor change (handle author_fixed binding), and potentially a `kb-cli` backfill for existing promoted skills.
- Con: does not help existing promoted skills until re-authored or backfilled.

**Option B — bake the fixed source into requires_extractions / retriever config.**

At synthesis time, populate `requires_extractions[0].page_id` (or equivalent retriever filter field) with the specific page ID(s) from `source_samples`. The executor reads this field and restricts KB retrieval to those page IDs.

- Pro: lower schema surface area; no new `source_binding` block needed.
- Pro: retriever-side enforcement — no executor path change beyond reading an existing field.
- Con: `requires_extractions` was designed as an extraction schema, not a source selector; conflating the two concerns muddies the schema.
- Con: multi-page skills with many source pages may hit field-width limits or ordering issues.

**Option C — other mechanism (e.g., INGEST-time tagging to a skill-scoped KB namespace).**

At `authorSkill → INGEST` time, tag the ingested pages with a skill-scoped KB name (e.g., `tpm.faaas_kiwi_project_pptx.sources`). The executor retrieves from that scoped KB. The skill YAML carries the KB name (already in `requires_extractions[0].kb`).

- Pro: retriever scoping is already the designed isolation boundary (skills retrieve from their own KB, not the shared KB).
- Con: relies on the KB name being scoped tightly enough that only the right pages are in it; currently that KB could have been populated from any page at ingest time.
- Con: does not address the root issue (the KB may contain the wrong pages if the INGEST step ingested from a broader scope than intended).

---

## RC2 — DESIGN_SKILL Emits layout as Prose; No Prose-to-Renderer-ID Resolution Step Exists

### Precise Statement

ADR-034 (accepted 2026-05-17) correctly removed internal preset IDs from the `design_skill` prompt: the LLM no longer receives `weekly_exec_review_v1` as a constrained output value. Instead, the prompt injects `{layout_preset_catalog}` — plain-language descriptions of presets (human_label, description, when_to_use, structural_shape; no internal_id).

The ADR-034 design (§B, Prompt injection) states the LLM should "Select the catalog entry whose `when_to_use` best matches the intent" and "Emit the corresponding `internal_id` in `workflow_shape.layout`." However, in this session, the DESIGN_SKILL LLM produced `workflow_shape.layout` as a descriptive prose sentence — the same prose that appears in `when_to_use` / `structural_shape` — not a catalog `internal_id`.

The committed artifact `synthesis.layout` field carries that same prose sentence (originating from DESIGN_SKILL, faithfully preserved through synthesize_workflow). At execution time, `PptxRenderer.render()` calls `get_preset(layout)`, which returns `None` for any prose string that is not a registered `internal_id`. The ADR-034 renderer fallback is: `log.warning("unknown layout id …; falling back to default")` — it produces the generic default renderer output, which in this skill's case is the 6-slide key/value stub.

The prompt instruction to "emit the corresponding `internal_id`" was not enforced by structured output or validation; the LLM followed the prose-description reasoning but output prose instead of the ID token.

### Evidence

- Committed artifact `synthesis.layout`: the prose sentence "Standard executive order single-slide: title/status first; then RAG summary + schedule health; then key metrics/progress; then blockers/risks; then next steps + exec asks. Links/metadata go to speaker notes."
- Session `design.workflow_shape.layout` (from DESIGN_SKILL LLM output): the same prose sentence — the prose originates at DESIGN_SKILL, not at synthesis.
- ADR-034 §B renderer dispatch: `get_preset(layout)` → `None` for prose → `log.warning` → fallback to default stub renderer.
- Runtime request 146 output: 6-slide stub, none of the designed fields (slide_title, rag_summary, schedule_health, key_metrics, blockers, next_steps, exec_asks) present.

### Blast Radius

**Likely affects every skill designed after ADR-034 shipped (2026-05-17) that involves a PPTX or other layout-dispatched output.** Any skill where the DESIGN_SKILL LLM emitted a prose layout description instead of a catalog `internal_id` will fall through to the stub renderer at execution time. Skills designed before ADR-034 (which had hardcoded `weekly_exec_review_v1` in the artifact) are not affected.

The precise set of affected skills = all PPTX skills authored in sessions after the ADR-034 prompt v1.2 shipped where `layout` in the committed artifact is a prose string rather than a registered `internal_id`.

### Fix Options (no decision — user decides)

**Option A — DESIGN_SKILL must emit a catalog internal_id (constrained output).**

Change the `design_skill` prompt v1.2 to require the LLM to emit `workflow_shape.layout` as exactly one of the catalog's registered `internal_id` values (e.g., by including the IDs as a constrained enum in the output schema, or by using structured output / function-calling with an enum field). The human-readable `layout_rationale` field continues to carry the prose explanation.

- Pro: closes the gap at the source — the artifact always carries a machine-resolvable ID.
- Pro: consistent with how other machine fields (e.g., `source_binding_mode`) are emitted.
- Con: partially re-introduces an internal ID into the prompt (as a constrained output enum), which was the original concern in DECISION-014. Mitigation: the ID appears only in the output schema (not as a rule or example in the reasoning instructions); the LLM reasons over descriptions, then maps to an ID as the final step.
- Con: does not fix already-committed prose-layout artifacts (backfill needed).

**Option B — post-design resolver maps prose layout description to a catalog internal_id.**

After the DESIGN_SKILL LLM call returns, a post-design resolver step (LLM-based or deterministic string matching) maps the prose `workflow_shape.layout` to the best-fit `internal_id` from the catalog. The `must_show_human` card review gate (DECISION-018 §C) is the natural point to surface the mapping for author confirmation.

- Pro: does not require the DESIGN_SKILL LLM to know about internal IDs at all; maintains full separation of "reasoning about layout" from "resolving to a machine ID."
- Pro: the author sees both the reasoned prose and the resolved ID at review time.
- Con: adds a resolver step (new code); LLM resolver can mis-map; deterministic resolver is brittle for prose variation.
- Con: the resolver is a new failure mode; if it produces `None`, the skill is committed with no renderable layout.

**Option C — renderer-side resolver (prose-tolerant dispatch).**

The renderer's `get_preset()` lookup is extended to fall back to a fuzzy/semantic match over catalog descriptions when the layout string is not a registered `internal_id`. The renderer emits a WARNING and proceeds with the best-fit preset rather than falling back to the default stub.

- Pro: fixes execution for already-committed prose-layout artifacts without backfill.
- Pro: no authoring-side change required.
- Con: moves the resolution concern into the renderer (an execution-time component), making renderer behavior dependent on LLM/fuzzy matching — adds complexity and a new failure mode to a component that was previously deterministic.
- Con: does not fix the artifact contract (the artifact still carries prose); future tooling that reads the artifact (eval harness, routing, card inspection) may misinterpret the layout field.

---

## Finding B — Silent-Stub Amplifier (Related Framework Defect)

### Statement

The executor/renderer emitted `tier_used=1, confidence=0.95` and a success message ("generated the FAaaS Kiwi project update PPTX") for a request where:

1. The skill's fixed source was not resolved (RC1 — wrong content retrieved).
2. The skill's layout was prose-unresolvable (RC2 — renderer fell back to stub).
3. The `requires_extractions` schema fields (slide_title, rag_summary, etc.) did not appear in the output.
4. The rendered PPTX contained only a key/value metadata dump of the wrong page.

None of RC1, RC2, or the unmet `requires_extractions` contract caused the executor or renderer to fail or signal degradation. The response was indistinguishable from a successful execution.

This is a **silent-degradation / cardinal-rule violation**, amplifying RC1 and RC2: neither defect was observable from the API response. This violates the no-silent-degradation invariant established in ADR-031 and reinforced in ADR-032 §G.

Finding B is **distinct from RC1 and RC2** (it is the amplifier, not the cause), and warrants a separate fix decision.

### Fix Options (no decision — user decides)

**Option A — fail-loud guard on unresolvable layout ID.**

`PptxRenderer.render()`: if `get_preset(layout)` returns `None`, raise a hard error (not just log a WARNING) — surfaced as an executor failure, propagated as `[HIGH]` in the three-section EVAL/execution report (DECISION-018 §H). The renderer never silently falls back to the stub when the contracted layout is unresolvable.

- Pro: directly addresses the silent-degradation violation; consistent with ADR-031.
- Pro: minimal scope — one line change in the renderer.
- Con: existing skills with `layout: "default"` or `layout: "weekly_exec_review_v1"` are unaffected; only prose-layout skills (all authored post-ADR-034 without a resolved ID) start hard-failing. May surface latent defects in other promoted skills.

**Option B — executor-level requires_extractions completeness gate.**

Before returning the synthesis result, the executor checks that the rendered output contains at least one field from `requires_extractions`. If zero extraction fields are present in the output, it raises a `[HIGH]` error: "rendered output does not satisfy the skill's requires_extractions contract."

- Pro: catches a broader class of silent degradation (not only layout failures but also empty-extraction results).
- Con: requires the executor to inspect the rendered artifact content — couples executor and renderer concerns.
- Con: the check must be robust to format differences (pptx, email, markdown) — non-trivial.

**Option C — confidence score gating.**

Do not emit `confidence=0.95` (or any positive confidence) unless the extraction completeness threshold is met. A partial/stub output is `confidence < 0.5`, surfaced in the response.

- Pro: consumer-observable signal without a hard failure — consumers can detect and retry.
- Con: does not prevent the junk output from reaching the consumer; only changes the signal alongside it.
- Con: confidence scoring is currently not extraction-completeness-aware; implementing this requires rethinking the confidence model.

---

## Cross-References

| Reference | Relevance |
|---|---|
| ADR-032 | RC1 contract gap — `author_fixed` source propagation; see §D.1 |
| ADR-034 | RC2 layout catalog — prose-vs-ID gap; see §B prompt injection |
| DECISION-014 | Established the no-internal-preset-ID-in-prompts rule; RC2 is a downstream gap from that decision |
| DECISION-018 / ADR-038 | EVAL routing self-test and PROMOTE gate — EVAL did not catch RC1/RC2 (chicken-and-egg: skills designed in this session were authored before the ADR-038 routing self-test gate shipped; the EVAL path never exercised the full render pipeline against the fixed source) |
| spec §8 | RC2 is related to the open problem of faithful arbitrary-layout rendering (§8); RC1 is a framework contract gap, not an open research problem |
| ADR-031 | No-silent-degradation invariant violated by Finding B |

---

## What the User Must Decide

1. **RC1 fix direction**: Option A (persist `source_binding.mode: author_fixed` + pinned ref in artifact), Option B (bake into requires_extractions), or Option C (INGEST-time KB scoping).
2. **RC2 fix direction**: Option A (constrained ID output from DESIGN_SKILL), Option B (post-design prose→ID resolver), or Option C (renderer-side fuzzy dispatch).
3. **Finding B fix direction**: Option A (fail-loud on unresolvable layout), Option B (executor extraction gate), or Option C (confidence gating) — or defer.
4. **Sequencing**: RC1 and RC2 are independent and can be addressed in either order. Finding B (amplifier) is lower priority than RC1/RC2 but should be addressed before the next round of author_fixed PPTX skills are promoted.
5. **Backfill**: existing promoted author_fixed PPTX skills authored after ADR-034 (prose-layout artifacts) will need either a YAML backfill (map prose → internal_id) or a re-author session, regardless of which RC2 fix option is chosen.

---

*See ADR-032 §Known Gap (RC1) and ADR-034 §Known Gap (RC2) for the formal gap records appended to those ADRs.*
