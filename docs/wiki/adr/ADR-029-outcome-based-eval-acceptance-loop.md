---
title: "ADR-029 — Outcome-based EVAL: demonstration-artifact acceptance loop"
status: proposed
created: 2026-05-15
owner: architect
deciders: user, tpm
supersedes: DECISION-010 (Option A auto-generated gold — see section E)
tags: [adr, eval, skill-builder, acceptance-loop, adr-027, adr-028, adr-015]
related: [ADR-015, ADR-027, ADR-028, DECISION-010, DECISION-011]
---

# ADR-029 — Outcome-based EVAL: demonstration-artifact acceptance loop

## Status

**Proposed** — awaiting user direction. Do NOT implement until the user accepts one of the options in section D and resolves the reconciliation questions in section E.

---

## A. Context — why intrinsic EVAL passed while the artifact was wrong

### The intrinsic EVAL model (current: _run_eval, conversation.py:3180-3563)

ADR-027 (DECISION-010 Option A) replaced the EVAL stub with a real harness.
The algorithm:

1. **Re-use cached source samples** from INSPECT_SOURCES (`_data.source_samples`).
   `conversation.py:3207-3245`

2. **Run extraction** on each sample with `_llm_extract`.
   `conversation.py:3257-3314`

3. **Compute recall@k** (lines 3316-3331): fraction of schema fields that are
   non-empty in the extracted output.
   ```python
   # conversation.py:3326-3331
   for f in all_fields:
       total_expected += 1
       if extracted.get(f):
           total_hits += 1
   recall_at_k = round(total_hits / max(total_expected, 1), 3)
   ```
   **This is field-coverage, not correctness.** Any non-empty string counts as a hit.

4. **Compute faithfulness** (lines 3334-3374): LLM judge asks "is the extracted
   value grounded in the source snippet?" — source snippet capped at 12,000 chars
   (`source_snippet = str(sample.get("content", ""))[:12000]`). The judge
   verifies extraction fidelity to the *source page*, not to the *user's intent*.

5. **Score workflow tier + latency** (lines 3377-3420): call `/api/v1/ask`,
   record `tier_used` and `artifact_url`. The PPTX artifact IS rendered at this
   step — but only `tier_used` and `ask_latency_ms` are checked against the gold
   row. The rendered artifact content is never compared against anything.
   ```python
   # conversation.py:3422-3434
   wf_gold_row = {
       "actual_tier_used": wf_tier,
       "actual_artifact_url": wf_artifact_url,
       "ask_latency_ms": ask_latency_ms,
       ...
   }
   ```

6. **Gate PROMOTE** on `recall@k >= 0.85 AND faithfulness >= 0.85`.
   `conversation.py:3484`

### What EVAL never sees

- The reference artifact the user uploaded at UPLOAD_ARTIFACT_EXAMPLE. It is
  parsed at `_handle_upload_artifact_example` (line 1244-1252) into a layout
  dict (`_data.artifact_layout`) and then **discarded as an evaluation signal**.
  The original file bytes are not retained; only the structural parse (section
  list, slide count, mapping dict) survives. `_run_eval` never reads
  `_data.artifact_layout` at all — zero lines of EVAL code touch it.

- The produced artifact's content. The rendered PPTX path appears in
  `wf_artifact_url` (line 3410) but is not read, parsed, or scored.

- Whether the produced artifact resembles what the user demonstrated. The only
  user-provided ground truth (the demonstration example uploaded at
  UPLOAD_ARTIFACT_EXAMPLE) is completely absent from the scoring path.

### The concrete failure

During this session: skill `tpm.26ai_fa_db_upgrade_to_26ai_pptx` passed EVAL
(recall@k and faithfulness both above threshold) yet the produced PPTX was
noticeably thinner than the user's reference slide (slide 15 of the FAaaS weekly
deck). The reference was image-only (`faaas-slide15-reference.pptx` at
`~/.kbf/store/uploads/synth-tpm-9d3b6233/.../`), so it produced `_data.artifact_layout = None`
and was silently skipped. But the real point is deeper: **even if the reference
had been text-bearing, EVAL would still not have compared the output PPTX against
it.** The observation in ADR-028 Item 4 (synthesisable fields excluded by
DESIGN_SKILL) was one contributing root cause. The other is that EVAL has no
mechanism to detect the structural gap — empty sidebars, 1-bullet "Status", no
Next Steps or Risks — because it never looks at the output artifact.

### The intrinsic vs outcome distinction

| Intrinsic EVAL (current) | Outcome-based EVAL (proposed) |
|---|---|
| Ground truth = same LLM's extraction of the same source | Ground truth = user's demonstrated output artifact |
| Scores extraction *consistency* (does re-running produce the same values?) | Scores artifact *resemblance* (does the output look like what the user showed?) |
| Faithfulness = extracted value in source snippet | Fidelity = produced artifact contains right sections, density, data |
| Never runs the full workflow end-to-end during scoring | Runs the full workflow to produce the artifact, then scores it |
| Circular: the evaluation model is the same as the production model | External reference: the user's own example is the standard |
| Passes when extraction is internally consistent | Passes when the human says "yes, that's what I wanted" |

---

## B. Proposed Decision — the 6-step outcome-based acceptance loop

This ADR proposes replacing intrinsic EVAL (the ADR-027 / DECISION-010 Option A
algorithm) with an **outcome-based acceptance loop** that closes ADR-015's
"skill by demonstration" promise: the skill is correct if and only if its output
resembles the user's demonstration, populated with real data.

### The 6-step loop

1. **Extract** field values from real source(s) using the committed schema.
   (Same as current EVAL step 2 — unchanged.)

2. **Run the full workflow skill end-to-end** to produce the output artifact
   (PPTX, DOCX, email) — not just extract fields.
   Currently the `/api/v1/ask` call at EVAL step 6 does produce an artifact,
   but only records tier and latency. The produced artifact must be retained
   and parsed for scoring.

3. **Compare the produced artifact against the reference** (the demonstration
   example the user uploaded at UPLOAD_ARTIFACT_EXAMPLE) and produce a
   structured score:
   - Structure score: sections/slides present in reference vs produced
   - Density score: content volume (fields populated) relative to reference
   - Fidelity score: extracted values are grounded in the real source (existing
     faithfulness judge — retained)
   - Layout score: column structure, slide count conformance

4. **If score is not acceptable**: surface the scoring data to the user in plain
   language ("The produced PPT has 4 sections; your reference had 7. Missing:
   Risks, Next Steps, Exec Asks."). Then run a **troubleshooting LLM call** to
   diagnose the root cause and produce a **CHANGE PROPOSAL** — a concrete edit
   to the extraction schema and/or the workflow layout — for the user to review.

5. **Based on the user's chosen change**: route back to the appropriate prior
   state to apply the fix. The routing is a **constrained map** (see section C.3
   for the full map), not a free LLM choice. The LLM identifies the failure class;
   the map determines the target state.

6. **Loop** until the user explicitly accepts. **User acceptance is the terminal
   gate, not a numeric threshold.** The loop is bounded by max iterations, a cost
   ceiling, and a "ship as draft" escape hatch.

### State-machine delta against the ADR-027 16-state machine

The EVAL state becomes a branch point, not a linear gate. No new states are added
to the machine; instead, EVAL gains routing intelligence.

**Current EVAL exit paths (ADR-027):**
```
EVAL → PROMOTE  (if recall@k >= 0.85 AND faithfulness >= 0.85)
EVAL → EVAL     (if metrics fail — offers guidance text, waits for force-promote)
```

**Proposed EVAL exit paths (ADR-029):**
```
EVAL → REVIEW_DESIGN           (failure class: missing/thin fields → schema edit)
EVAL → CONFIGURE_SOURCES       (failure class: source genuinely lacks content)
EVAL → INSPECT_SOURCES         (failure class: wrong source / missing pages)
EVAL → [layout edit sub-loop]  (failure class: wrong layout or section order)
EVAL → PROMOTE                 (user explicitly accepts output)
EVAL → DONE as draft           (user chooses escape hatch)
```

The user-acceptance gate replaces the numeric threshold gate. The numeric scores
become **diagnostic signals that inform the CHANGE PROPOSAL**, not pass/fail gates.

### Reference artifact retention (the drop point and the fix)

**Current drop:** `_handle_upload_artifact_example` (conversation.py:1243-1252)
parses the artifact into a layout dict and stores it at `_data.artifact_layout`.
The file bytes themselves are cleaned up by `ArtifactStore.cleanup(synth_id)` at
session DONE (`mcp_server.py` lifespan). No pointer to the original file is
retained in `_SessionData` past the UPLOAD_ARTIFACT_EXAMPLE state.

**Required change:** `_SessionData` must gain a field:
```python
artifact_reference_id: str | None = None  # ArtifactStore ID, NOT a local path
```
populated at `_handle_upload_artifact_example` alongside `artifact_layout`. The
ArtifactStore must NOT call `cleanup()` until after EVAL completes (or until the
escape hatch is taken). `_run_eval` reads the reference artifact via:
```python
ref_bytes = self._artifact_store.read(self._data.artifact_reference_id)
```
and passes it to the semantic comparator alongside the produced artifact bytes.

The produced artifact bytes are obtained by parsing the file at `wf_artifact_url`
returned from the `/api/v1/ask` call, rather than only recording the URL.

---

## C. Feasibility — the hard parts

### C.1 Image-only reference problem

**The observed case:** `faaas-slide15-reference.pptx` is an image-only PPTX (one
picture shape per slide, zero text runs) — verified this session. `analyze_artifact`
raises `ValueError` at `_handle_upload_artifact_example:1258-1268` and the file
is hard-rejected. `_data.artifact_layout = None` is silently set. EVAL receives
no reference.

**Why this matters for the proposed model:** The entire outcome-based comparator
depends on being able to read the reference. With an image-only reference, a
text/structure diff is impossible.

**Options for image-only references:**

| Option | Mechanism | Accuracy | Cost | Recommendation |
|---|---|---|---|---|
| **(i) Vision-LLM comparator** | Send reference image slide(s) and produced PPTX rendered slide(s) to a multimodal LLM. Ask it to compare sections, content density, and layout. | High (sees what the user sees) | Medium-high: ~$0.01-0.05 per comparison call on GPT-4V / OCI vision model | **Recommended** — aligns with the user's expectation (they're comparing visually) |
| **(ii) Require text-bearing reference** | Hard-reject image-only PPTX at UPLOAD_ARTIFACT_EXAMPLE with a clear message. User must provide a text-bearing file. | N/A (prevents the comparison entirely) | Zero | Acceptable only if the no-silent-degradation rule is satisfied — a clear rejection is not silent. But it breaks the common case where users only have a screenshot or a scanned PPT. |
| **(iii) Score against structure-spec derived at UPLOAD_ARTIFACT_EXAMPLE** | Extract the structure spec from the reference during UPLOAD_ARTIFACT_EXAMPLE (what sections should appear, what density each should have) and save it as a structured spec. EVAL scores the produced artifact against the spec, not the reference bytes. | Medium — misses visual layout but captures section/content requirements | Low | Valid fallback when the reference is image-only but a structure spec can be extracted. Requires a vision-LLM pass at UPLOAD_ARTIFACT_EXAMPLE to produce the spec — same cost as option (i), but happens earlier. |
| **(iv) Graceful fallback to intrinsic EVAL** | When no usable reference exists (image-only, no reference uploaded, or reference parse failed), fall back silently to the current ADR-027 intrinsic EVAL. | Low (same as current) | Zero | **Hard no.** The user has a no-silent-degradation rule. A skill authored without a reference must be told explicitly that its EVAL is intrinsic-only and therefore has weaker guarantees. Fallback is acceptable if and only if it announces loudly what it is doing and why. |

**Recommended handling:** Option (i) — vision-LLM comparator — as the primary
path. A multimodal model receives the reference image slide(s) (rendered via
`python-pptx` → `Pillow` PNG export, or passed as the PPTX bytes directly to a
vision-capable model) and the produced PPTX rendered slide(s), and scores the
comparison. This is the only option that handles image-only references without
requiring the user to provide a text-bearing alternative.

**Cost/accuracy tradeoff for option (i):** A vision-LLM comparison call runs
once per EVAL iteration, not per extraction. At estimated $0.02-0.05 per call
(GPT-4V or OCI vision, two images), the per-iteration cost is dominated by the
extraction calls, not the comparator. Accuracy depends on the model's visual
reasoning quality; multimodal models at the frontier (GPT-4V, Claude 3.5 Sonnet)
are reliable for structural comparison (sections present/absent, density, layout)
but less reliable for fine-grained content verification. This is acceptable for
the purpose: the comparator is a signal, not a theorem prover, and the user
makes the final call.

**OCI constraint:** OCI GenAI Inference (current LLM backend per ADR-014) does
not expose a vision-capable model as of this writing. This means vision comparison
requires either (a) a second LLM provider (OpenAI, Anthropic) for comparator
calls only, or (b) an OCI vision model when one becomes available. This is a
real feasibility risk — see section F.

**Fallback chain (required, non-silent):**
```
reference_available AND text-bearing → text/structure diff comparator
reference_available AND image-only → vision-LLM comparator [if OCI vision available]
reference_available AND image-only AND no vision model → structure-spec comparator (option iii)
reference_not_available OR all comparators unavailable → intrinsic EVAL ONLY
  → MUST display: "No usable reference artifact. EVAL is intrinsic-only (consistency
    check, not correctness check). This skill should be validated manually before
    fleet promotion."
```

### C.2 Semantic artifact comparator — the rubric

No pixel diff. The comparator scores the produced artifact against the reference
across four rubric dimensions:

| Dimension | What it measures | How scored |
|---|---|---|
| **Structure** | Are the same sections/slides/headings present? | Exact match on section names from reference; LLM used to normalise synonyms (e.g. "Next Steps" = "Action Items") |
| **Density** | Is each section populated at similar content volume as the reference? | Word count ratio per section, OR field count ratio; flagged when density < 0.5x reference |
| **Data fidelity** | Are the extracted field values grounded in the real source? | Existing faithfulness judge (ADR-027) retained — this dimension is unchanged |
| **Layout conformance** | Do columns, slide count, and visual structure match? | python-pptx structural comparison for text-bearing files; vision-LLM for image-only |

The rubric produces a score per dimension (0.0-1.0) and a list of
`missing_elements` (sections/fields absent in the produced artifact but present
in the reference). The missing_elements list drives the CHANGE PROPOSAL in step 4.

**The "model grades its own output" caveat.** Intrinsic EVAL (ADR-027) is fully
circular: the same LLM that designs the extraction also grades the extraction. The
outcome comparator breaks this cycle for three of the four dimensions: Structure,
Density, and Layout are scored against the USER-PROVIDED reference, not against
the LLM's own expectations. Only Data Fidelity retains an LLM judge, but the
reference point (source snippet) is the real Confluence page, not a self-generated
gold row. This is materially stronger ground truth.

The residual circularity risk is the CHANGE PROPOSAL step: the LLM diagnoses the
gap and proposes a fix. If the LLM systematically misdiagnoses the gap (e.g.
always proposes "add more source pages" when the real fix is "expand the schema"),
the loop will cycle without converging. The guardrails in C.3 bound this.

### C.3 Replan-routing safety

"Let the LLM pick which step to go back to" is unsafe as a free choice — it can
produce loops (LLM routes to DESIGN_SKILL, re-runs, scores, routes to DESIGN_SKILL
again indefinitely). The LLM's role must be constrained to **failure class
identification only**; the routing map is deterministic.

**Failure classification:** The troubleshooting LLM call (step 4) returns one of
the following failure classes:

```
MISSING_FIELDS     — required sections/fields not in the schema at all
THIN_FIELDS        — fields in schema but values are empty or too short
WRONG_LAYOUT       — structure/sections present but in wrong order or wrong column count
SOURCE_COVERAGE    — source genuinely lacks the required content (content not on the page)
WRONG_SOURCE       — wrong page/space configured; right content exists elsewhere
UNSUPPORTABLE      — content cannot be automated from any configured source (human judgment needed)
```

**Constrained routing map:**

| Failure class | Allowed target state | Rationale |
|---|---|---|
| `MISSING_FIELDS` | `REVIEW_DESIGN` | Schema needs new fields or relaxed constraints |
| `THIN_FIELDS` | `REVIEW_DESIGN` | Field extraction instructions need improvement (ADR-028 Item 4 — synthesisable fields) |
| `WRONG_LAYOUT` | `REVIEW_DESIGN` (workflow_shape edit) | Layout is a workflow_shape parameter, editable at REVIEW_DESIGN |
| `SOURCE_COVERAGE` | `CONFIGURE_SOURCES` | Add more source pages or a different Confluence space |
| `WRONG_SOURCE` | `INSPECT_SOURCES` | Re-inspect with corrected source list |
| `UNSUPPORTABLE` | `DONE` as draft + user message | Tell the user: this field cannot be automated from the configured sources. Provide a clear explanation. Do not loop. |

The LLM NEVER selects the target state. It selects the failure class. The routing
map is code, not a prompt.

**Loop guardrails (mandatory, not optional):**

```
max_eval_iterations: 3          # per session; configurable in workflow YAML
cost_ceiling_usd: 5.00          # cumulative EVAL cost; stops and ships as draft
escape_hatch: "ship as draft"   # user can exit at any EVAL turn
```

When `max_eval_iterations` is reached or `cost_ceiling_usd` is exceeded, the
session transitions to `DONE` with a "shipped as draft" message: the skill
exists in the ADB (committed) but is flagged `status: draft` (not promoted). The
user can manually trigger a new EVAL session later.

**Pathological loop detection:** If the same failure class is returned on two
consecutive iterations, the loop is detected and the session falls through to
`DONE as draft` with a message: "EVAL has cycled twice on the same failure class
(`{class}`). This likely means the root cause is structural (the source genuinely
lacks this content). The skill is saved as draft for manual review."

### C.4 Cost and latency per iteration

Each EVAL iteration under the proposed model:

| Operation | Calls | Est. cost (OCI GenAI) | Est. latency |
|---|---|---|---|
| Extraction per sample (2-3 samples) | 2-3 LLM | ~$0.003-0.006 | 5-10s |
| Workflow run (end-to-end PPTX render) | 1 `/api/v1/ask` | ~$0.002-0.01 (synthesis) | 10-30s |
| Vision-LLM comparator (if image-only) | 1 vision LLM | ~$0.02-0.05 | 3-10s |
| Text comparator (if text-bearing) | 0 LLM (deterministic) | ~$0 | <1s |
| Troubleshooting + CHANGE PROPOSAL | 1 LLM | ~$0.002-0.005 | 3-8s |
| **Total per iteration** | **4-6 LLM** | **~$0.03-0.07** | **20-60s** |

With a `max_eval_iterations: 3` cap and `cost_ceiling_usd: 5.00`:
- **Worst case:** 3 iterations × $0.07 = $0.21 in EVAL alone
- **Per-session ceiling:** $5.00 (generous; the skill authoring session costs
  ~$0.10-0.30 in other states — EVAL adds at most $0.21 in 3 iterations)

The $5.00 ceiling is a safety net for edge cases (large schemas, many source
pages, vision-LLM calls). In practice, 1-2 EVAL iterations is the expected path.

A sane cap for v1: `max_eval_iterations: 3`, `cost_ceiling_usd: 2.00`.

---

## D. Options — overall approach

### Option A — Full acceptance loop (all 6 steps, recommended)

Implement the complete proposal: outcome scoring, CHANGE PROPOSAL, constrained
replan-routing, and the user-acceptance terminal gate.

**Pros:**
- Closes the ADR-015 "skill by demonstration" promise fully.
- Catches structural gaps (missing sections, thin density) that intrinsic EVAL
  cannot detect.
- The routing map turns a frustrating "your eval failed, figure it out" into a
  guided "here's what's missing and here's the proposed fix."
- User acceptance as the terminal gate aligns with how persona teams actually
  validate output (they look at it, not at a number).

**Cons:**
- Highest implementation effort: semantic comparator + vision-LLM path + replan
  routing + loop guardrails. Estimated 8-12 days of framework dev.
- OCI vision model availability is a real constraint (see C.1). The image-only
  path requires a second LLM provider or a structured workaround.
- Each EVAL iteration is 20-60 seconds. Three iterations = 60-180 seconds of
  additional authoring latency (acceptable for a one-shot authoring flow, but
  the user should expect it).
- CHANGE PROPOSAL quality depends on the troubleshooting LLM call quality. A
  bad diagnosis sends the user to the wrong state.

**Effort:** 8-12 days (semantic comparator: 3d, vision path: 2d, routing: 2d,
loop guardrails: 1d, tests: 2-4d).

### Option B — Outcome scoring only (steps 1-4, no automated replan)

Implement steps 1-4: produce the artifact, score it against the reference,
surface the scores and gap report to the user. The user then manually goes back
to REVIEW_DESIGN (or wherever) to fix the issue.

**Pros:**
- Materially simpler: no replan routing, no loop logic, no constrained map.
  Estimated 3-5 days.
- Eliminates the "model grades its own output" problem for Structure, Density,
  and Layout dimensions.
- The gap report is useful even without automated routing.

**Cons:**
- The user must figure out which state to return to from the gap report. This is
  better than today but still requires the user to understand the state machine.
- No convergence guarantee: the user may iterate manually without the system
  helping them route correctly.
- Still requires the reference artifact retention fix and the semantic comparator
  (shared work with Option A).

**Effort:** 3-5 days (semantic comparator: 3d, artifact retention fix: 0.5d,
gap report: 1d, tests: 1-2d). Option B work is a strict subset of Option A.

### Option C — Hybrid phased (recommended compromise)

Ship Option B first (outcome scoring + gap report), then add Option A's
replan-routing in a follow-up iteration.

**Why this is the recommended option:**
- Option B's outcome scoring already eliminates the core failure mode (EVAL
  passes while the artifact is structurally wrong). This is the value-at-risk.
- Option A's replan-routing is the UX layer on top. It makes the fix easier but
  is not required for correctness.
- Phasing means the semantic comparator (the shared hard part) is built once and
  reused. The routing layer is additive.
- Decouples the OCI vision-model dependency: Option B can ship with the
  text-bearing comparator + structured fallback (Option C.1.iii); the vision-LLM
  path is added when an OCI vision model is available.

**Sequencing:**
- Phase 1 (Option B): artifact retention fix + text comparator + gap report.
  3-5 days. Ships immediately.
- Phase 2 (adds Option A layers): vision-LLM comparator + troubleshooting LLM +
  constrained routing map + loop guardrails. 5-8 additional days.

**Effort:** Same total as Option A (~8-12 days) but split into shippable
increments with earlier user value.

**Recommendation: Option C.** Ship the scoring first; add the routing loop when
the comparator is validated against real sessions.

---

## E. Reconciliation with DECISION-011 / ADR-028

### Item-by-item fate table

| ADR-028 Item | Description | Fate under ADR-029 |
|---|---|---|
| **Item 1 — Persona-aware prompts** | Static LLM prompts; persona is a label, not an instruction shaper | **Independent — still needed.** ADR-029 changes EVAL; Item 1 changes DESIGN_SKILL and CAPTURE_INTENT prompts. No overlap. Persona-aware prompts improve extraction quality regardless of how EVAL works. Should be implemented in parallel. |
| **Item 2 — `must_show_human` / human-in-loop** | ConversationTurn has no machine-readable signal to block the smart client | **Prerequisite for ADR-029 to work correctly.** The CHANGE PROPOSAL and gap report (step 4) MUST be shown to the real human — the client cannot auto-respond to "here's the gap, here's the proposed fix." Without `must_show_human=true` on EVAL turns, an agentic client could silently route to REVIEW_DESIGN and re-run without the user ever seeing the gap analysis. ADR-029 adds more `must_show_human` turns (every CHANGE PROPOSAL turn). Item 2 should land before ADR-029's EVAL is live. |
| **Item 3 — CLARIFY state / conversational clarification** | "ok" steamrolls past ambiguities; REVIEW_DESIGN is a JSON dump | **Partially absorbed, partially independent.** ADR-029's troubleshooting dialogue (step 4) IS a conversational clarification loop — but it happens at EVAL, not at CAPTURE_INTENT or DESIGN_SKILL. The ambiguity steamroll problem (CAPTURE_INTENT → "ok") is upstream of EVAL and is not addressed by ADR-029. Item 3's CLARIFY state remains needed for the pre-EVAL states. The prose conversation in EVAL (step 4) should follow the same conversational pattern proposed in Item 3 Option A. |
| **Item 4 — Synthesisable fields** | DESIGN_SKILL excludes synthesisable fields (e.g. "risks" from WBS) as if unavailable | **Still needed, and is a fast independent win.** ADR-029 cannot fix a gap it cannot score: if "Risks" is missing from the schema entirely, the CHANGE PROPOSAL can suggest adding it, but the routing still sends the user to REVIEW_DESIGN, which sends them to DESIGN_SKILL again — which will exclude it again unless the synthesisable field confidence level is fixed. Item 4 is the proximate fix for the PPT thinness regression AND a prerequisite for the CHANGE PROPOSAL routing to work correctly for the `MISSING_FIELDS` class. Implement Item 4 before or alongside ADR-029. |
| **DECISION-010 Option A (auto-gold)** | Auto-generate gold rows from live samples; gate on recall@k + faithfulness | **Explicitly superseded for the terminal gate.** ADR-029 replaces the `exit_criteria.passed` numeric gate with user-acceptance. The auto-generated gold rows are RETAINED as diagnostic data (they are still computed and shown) but they are not the pass/fail gate. DECISION-010's `kind=auto_generated` disclaimer is preserved and extended: "These metrics are signals for the CHANGE PROPOSAL, not a pass/fail gate. Your acceptance of the output is the gate." The gold JSONL files continue to be written for CI regression purposes. Migration: no code deletion required — the metric computation stays; only the PROMOTE gating logic changes. |

### Recommended single decision path

The four items and two decisions currently form an overlapping cluster that is
hard to act on independently. This ADR recommends the following consolidated path:

**Step 1 (immediate, ~1.5 days) — Accept ADR-028 Items 2 + 4:**
- Item 4 (synthesisable fields): 1 day. Direct fix for the PPT regression.
  Independent of everything else.
- Item 2 (`must_show_human`): 0.5 days. Low-effort prerequisite. Land this before
  any EVAL changes ship.

**Step 2 (next, ~2-3 days) — Accept ADR-028 Item 3 (CLARIFY state):**
- Builds the conversational infrastructure that ADR-029's step 4 (CHANGE
  PROPOSAL dialogue) reuses.
- Item 1 (persona prompts) can run in parallel with Step 2.

**Step 3 (after Steps 1-2, ~3-5 days) — Accept ADR-029 Option C Phase 1:**
- Artifact retention fix + semantic comparator + gap report.
- Retire DECISION-010 as terminal gate; demote to diagnostic signal.
- Collapse DECISION-011 Items 2 + 4 as done (they're done by then).

**Step 4 (follow-on, ~5-8 days) — ADR-029 Option C Phase 2:**
- Add constrained replan-routing + loop guardrails + vision-LLM comparator.
- At this point, the full ADR-029 acceptance loop is live.

**Collapse DECISION-011 as follows:**
- Item 1 (persona prompts): keep as separate implementation task (parallel).
- Items 2 + 4: implement as Step 1 above; close DECISION-011 items 2 + 4 as done.
- Item 3 (CLARIFY state): implement as Step 2; close DECISION-011 item 3.
- Then ADR-028 can be marked accepted for Items 2, 3, 4; Item 1 separately.

**DECISION-010 disposition:**
- Mark DECISION-010 as superseded by ADR-029 for the terminal gate function.
- The auto-gold rows continue to exist and continue to be computed. They are
  demoted from "gate" to "diagnostic signal." No code deletion; one line of
  gate logic changes.

---

## F. Consequences

### Positive
- The ADR-015 "skill by demonstration" premise is finally honoured end-to-end:
  the user's shown output is the ground truth.
- Structural gaps (missing sections, thin density) are caught before the user
  manually discovers them by running the skill in production.
- The CHANGE PROPOSAL loop converts "eval failed, go fix it" into "here is the
  specific thing that's missing and here is how to fix it" — lower authoring
  friction for non-technical persona teams.
- User acceptance as the terminal gate aligns with how persona teams actually
  evaluate deliverables.

### Negative / Costs
- **OCI vision model dependency.** OCI GenAI does not currently expose a vision-
  capable model. Image-only references require either a second LLM provider or the
  structured fallback (structure-spec extraction at UPLOAD_ARTIFACT_EXAMPLE time).
  This must be resolved before the vision path ships.
- **Authoring latency.** Each EVAL iteration adds 20-60 seconds. Three iterations
  = 60-180 seconds of additional authoring latency. This is acceptable for a
  one-shot authoring flow (the user is present for the entire session) but the
  user should expect it.
- **Implementation scope.** 8-12 days total (Option C: 3-5d + 5-8d), versus the
  current EVAL which took ~2 days to implement. The comparator and routing map
  are the complex pieces.
- **CHANGE PROPOSAL quality risk.** If the troubleshooting LLM misdiagnoses the
  failure class, the routing sends the user to the wrong state. The constrained
  map limits blast radius (no infinite loops) but does not guarantee correct
  diagnosis. The pathological-loop detector (same failure class twice → exit)
  is the safety valve.

### Reversibility
- Option B is a strict subset of Option A. Shipping Option B and stopping is
  a coherent state.
- The numeric EVAL thresholds (recall@k + faithfulness) are retained as
  diagnostic signals. Rolling back to DECISION-010's numeric gate requires
  only reverting the gate logic in `_run_eval` — the gold rows are unchanged.
- ADR-027's EVAL remains structurally in place; this ADR extends it, does not
  replace it wholesale.

### Sequencing dependencies
```
ADR-028 Item 4 (synthesisable fields)  →  ADR-029 CHANGE PROPOSAL works correctly for MISSING_FIELDS
ADR-028 Item 2 (must_show_human)       →  ADR-029 CHANGE PROPOSAL reaches the real user
ADR-028 Item 3 (CLARIFY state)         →  ADR-029 step 4 dialogue reuses the same pattern
ADR-029 Phase 1 (comparator)           →  ADR-029 Phase 2 (routing + vision-LLM)
```

### Rough effort summary

| Work item | Owner | Effort |
|---|---|---|
| Artifact retention fix (`artifact_reference_id` on `_SessionData`, ArtifactStore lifecycle) | Backend Dev | 0.5d |
| Text-bearing semantic comparator (structure + density + layout scores) | Backend Dev | 2-3d |
| Vision-LLM comparator (render slides + vision model call) — blocked on OCI vision availability | Backend Dev | 2d (when unblocked) |
| Troubleshooting LLM call + CHANGE PROPOSAL output | Backend Dev | 1-2d |
| Constrained routing map + loop guardrails | Backend Dev | 1-2d |
| `must_show_human` on EVAL turns (ADR-028 Item 2) | Backend Dev | 0.5d |
| Synthesisable fields (ADR-028 Item 4) | Backend Dev | 1d |
| CLARIFY state (ADR-028 Item 3) | Backend Dev | 2-3d |
| Tests (comparator unit, routing unit, loop-guard unit, E2E) | QA / Backend Dev | 2-4d |
| **Total** | | **12-17d** (phased: 4-6d Phase 1, 8-11d cumulative) |

---

## Cross-references

- [ADR-015 — Skill-by-demonstration](ADR-015-skill-by-demonstration.md) — original
  "skill by demonstration" premise; ADR-029 is its EVAL closure
- [ADR-027 — Design-first authorSkill](ADR-027-design-first-authorskill.md) — the
  16-state machine; EVAL state is extended, not replaced
- [ADR-028 — Prompt investment, human-loop, clarification](ADR-028-authorskill-prompt-investment-human-loop-conversation.md)
  — Items 2 and 4 are prerequisites; Item 3 is complementary
- [DECISION-010 — EVAL gold sets](../../pmo/decisions/DECISION-010-eval-gold-sets-auto-vs-human.md)
  — superseded as terminal gate; auto-gold rows retained as diagnostic signal
- [DECISION-011 — authorSkill prompt and human-loop direction](../../pmo/decisions/DECISION-011-authorskill-prompt-and-human-loop-direction.md)
  — see reconciliation in section E for item-by-item fate
- `docs/wiki/authorskill-flow.md` — state-by-state map; EVAL section requires update
  once this ADR is accepted
- `framework/skill_builder/conversation.py:3180-3563` — `_run_eval` implementation
  (the intrinsic EVAL being extended)
- `framework/skill_builder/conversation.py:1243-1276` — `_handle_upload_artifact_example`
  (the artifact drop point being fixed)
