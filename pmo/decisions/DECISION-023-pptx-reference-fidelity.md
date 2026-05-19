# DECISION-023: PPTX Reference Fidelity — RCA + Options for Reproducing an Uploaded Reference Slide's Layout

**Status**: Proposed — awaiting user decision  
**Date**: 2026-05-19  
**Raised by**: architect  
**Skill under investigation**: `tpm.faaas_kiwi_project_pptx` (session `synth-tpm-628a9647`, PROMOTED)  
**Comparator scores**: structure_score=0.0, density_score=0.0  
**Related**: ADR-026, ADR-028, ADR-029, DECISION-014, ADR-034, spec §8  
**This is an ADR-028 §8 open problem (research task) — do NOT implement without a user decision**

---

## Problem Statement

The skill `tpm.faaas_kiwi_project_pptx` is genuinely PROMOTED (ADB-confirmed, all 5 artifacts
`status=promoted`). Its ADR-029 EVAL comparator ran cleanly against the uploaded reference slide
`/Users/sravansunkaranam/Downloads/2026-05-14 FAaaS-LCM Update Kiwi Slide only 2.pptx` and
returned **structure_score=0.0, density_score=0.0**. The produced artifact
(`~/.kbf/outputs/faaas_kiwi_project_pptx.pptx`, 30,057 bytes, real OOXML) bears essentially zero
structural resemblance to the reference slide.

PROMOTE passed because the ADR-029 comparator is diagnostic-only (gate = "comparator ran + user
accept"; DECISION-010 numeric gate superseded). This is NOT a gate bug. It is a genuine fidelity
gap: **the framework never reproduces the uploaded reference's layout; it renders a hardcoded
programmatic preset**.

The upstream intent was "skill by demonstration" (ADR-015): the user uploads a reference slide and
the produced artifact should look like that reference slide, populated with live data. That promise
is not fulfilled.

---

## Root Cause Analysis

### The seam where the reference layout is dropped on the floor

The path from reference upload to rendered output has five steps. The reference's layout is
discarded at Step 2 and never recovered at Steps 3-5.

**Step 1 — UPLOAD_ARTIFACT_EXAMPLE (conversation.py:2228-2243)**

`analyze_artifact(str(resolved_path))` is called on the uploaded reference PPTX.

`analyze_artifact` dispatches to `_analyze_pptx` (analyze_artifact.py:41-108). What `_analyze_pptx`
extracts:
- Slide titles → field names (snake_case)
- `body_text` snippet per slide (up to 400 chars of non-title text)
- `slide_count` (derived from the mapping dict)

What `_analyze_pptx` does NOT extract:
- Slide master / slide layout XML
- Shape geometry (left, top, width, height) for any shape
- Table structure (row count, column count, column widths)
- Text frame structure (multi-column, text box positions)
- Placeholder types (title, body, table, picture)
- Font sizes, paragraph spacing, fill colors beyond what python-pptx exposes from text frames
- The visual arrangement of shapes on the slide (e.g., two-column split at 55/40%)

The returned `layout` dict is:
```python
{
    "sections": ["title", "rag_summary", "schedule_health", ...],   # field names
    "slide_count": 1,
    "mapping": { "rag_summary": {"kind": "slide_title", "slide": 0, ...}, ... }
}
```

This dict is stored as `_data.artifact_layout` (conversation.py:2241).

**The exact drop point**: `_data.artifact_layout` carries only field names and slide-title
positions. All geometry — shape coordinates, table structure, column layout — is absent after
`analyze_artifact.py:_analyze_pptx` returns. The reference file's structural information is not
available to any downstream step.

**Step 2 — DESIGN_SKILL (conversation.py, `_run_design_skill`)**

`_data.artifact_layout` is passed to the `design_skill` prompt as `{artifact_layout}`, which
receives the dict above (field names, slide_count, mapping). The prompt uses this to determine
WHICH fields to include in the schema. It does NOT use it to specify HOW the output should be
laid out — that is the `workflow_shape.layout` field, which is chosen from the layout preset
catalog (ADR-034), not derived from the reference slide's geometry.

The `design_skill` prompt selects `workflow_shape.layout = "weekly_exec_review_v1"` (a hardcoded
programmatic preset) or `"default"` based on matching the catalog description to the user's intent.
There is no option in the catalog that says "clone the reference slide's exact layout."

**Step 3 — synthesis (synthesize_workflow_skill)**

The committed workflow YAML artifact carries `synthesis.layout: "weekly_exec_review_v1"` (or
`"default"`). It does NOT carry any geometric specification derived from the reference slide.
`_data.artifact_layout` is session state — it is not persisted into the committed artifact.

**Step 4 — PptxRenderer.render() (pptx_renderer.py:51-81)**

`render()` reads `data.get("layout")` — the `internal_id` from the committed YAML — and
dispatches:

- `"weekly_exec_review_v1"` → `_render_weekly_exec_review_v1(data)` (pptx_renderer.py:87-361)
- `"default"` → `_render_default(data)` (pptx_renderer.py:367-422)
- any other non-empty string → `ValueError` (hard error, DECISION-019 Finding-B fix)

`_render_weekly_exec_review_v1` builds a single-slide widescreen deck with:
- Oracle red header band at `(0", 0")` → `(13.333", 0.45")`
- Left column table at `(0.25", 1.1")` → `(7.2" wide, 5.9" tall)`, 2 rows
- Right sidebar 3-box stack at `(7.7", 1.1")` → `(5.3" wide)` with fixed heights

These dimensions, positions, and structure are **hardcoded constants** in
`pptx_renderer.py:259-322`. They were hand-derived from the original reference slide-15 (the
`weekly_exec_review_26ai` session, ADR-026 Fix 5) and are not read from any per-skill or
per-session configuration.

**Step 5 — ArtifactComparator.compare() (comparator.py:403-499)**

The comparator extracts sections from the produced artifact by calling
`_extract_pptx_sections(produced_bytes)` (comparator.py:157-185). This function reads
slide titles via `slide.shapes.title.text`. For the `weekly_exec_review_v1` output,
the produced PPTX has ONE slide. That slide was created from `prs.slide_layouts[6]`
(the blank layout, conversation.py:113-120 of the renderer) — **which has no title
placeholder**. There is a textbox for the title (`slide.shapes.add_textbox(...)`,
pptx_renderer.py:153) but it is NOT a `shapes.title`; `slide.shapes.title` returns
`None` for a blank-layout slide. Therefore:

```python
# comparator.py:172-173
title_text = ""
if slide.shapes.title and slide.shapes.title.text:
    title_text = slide.shapes.title.text.strip()
```

`slide.shapes.title` is `None` → `title_text = ""` → the slide is skipped (comparator.py:182-183).

`_extract_pptx_sections` returns `[]` for the produced artifact.

Meanwhile, the reference slide (the Kiwi slide) also uses non-title placeholders for its content
sections, so its sections list depends on whether its slides have titled placeholders. If the
reference similarly has no slide-title shapes, `ref_sections = []` as well, causing:
```python
# comparator.py:444-449
if not ref_sections:
    raise ValueError(...)
```
The comparator raises, and `comparator_result` is left `None`.

Alternatively, if the reference has some titles and the produced has none:
- `ref_sections` = N entries, `prod_sections` = []
- `prod_lookup` = {} (empty)
- Every reference section is `missing` → `structure_score = 0 / N = 0.0`
- `density_ratios` = [0.0, 0.0, ... N times] → `density_score = 0.0`

Either path produces the observed 0.0/0.0 result.

**Summary of the causal chain:**

```
UPLOAD_ARTIFACT_EXAMPLE
  → _analyze_pptx extracts field NAMES only; geometry/structure dropped
  → artifact_layout = {sections: [...], slide_count: 1, mapping: {...}}

DESIGN_SKILL
  → artifact_layout used to pick schema fields only
  → workflow_shape.layout chosen from preset catalog (hardcoded presets only)
  → no reference-geometry path exists in the catalog or the prompt

PptxRenderer._render_weekly_exec_review_v1
  → hardcoded geometry (pptx_renderer.py:259-322)
  → produces blank-layout slide with textbox title (no shapes.title placeholder)

ArtifactComparator._extract_pptx_sections
  → comparator.py:172: slide.shapes.title is None for blank-layout slide
  → sections = [] for the produced artifact
  → structure_score = 0.0, density_score = 0.0
```

The exact seam is `analyze_artifact.py:_analyze_pptx` (lines 41-108): the function reads text
content from the reference but discards all geometric and structural metadata. From that point
forward, no downstream component has access to the reference's layout structure.

---

## Options

### Option A — Template-Clone Rendering

**Approach**: Treat the uploaded reference PPTX as a real python-pptx template. At
UPLOAD_ARTIFACT_EXAMPLE, instead of (or in addition to) calling `analyze_artifact`, open the
reference PPTX and identify its "fill placeholders" — shapes that appear to be text regions
containing placeholder-style content (section headers, bullet lists, tables). At render time,
clone the reference PPTX using `python-pptx`'s copy mechanism, clear the placeholder shapes'
text, and inject the extracted field values.

**What changes:**
- `analyze_artifact.py:_analyze_pptx` gains a second return path: in addition to field names,
  it extracts a `shape_map` — for each identified fill region, the shape's index/identifier and
  its content type (text, table, bullet list). (~50 lines)
- A new `ArtifactStore.retain(reference_id)` lifecycle extension is needed so the reference PPTX
  bytes survive through to render time (currently they are stored by ArtifactStore, accessible
  via `artifact_reference_id`, but the renderer never reads them — see ADR-029 §B reference
  retention fix).
- `PptxRenderer` gains a `_render_template_clone(data, ref_bytes)` path: opens the reference
  bytes as a Presentation, iterates shapes, finds fill regions, injects text. (~100-150 lines)
- The `workflow_skill` YAML artifact gains no new fields — the reference is looked up at render
  time from `artifact_reference_id` stored in the session, or a new `reference_artifact_id`
  field in the workflow YAML for durable retention after session close.

**Fidelity ceiling**: Very high for template-driven skills (same header, same background, same
font sizes, same column split). The output looks like a populated version of the reference slide.

**Limitations and risks:**
- Only works if the reference slide uses actual text-bearing shapes (text frames, tables,
  placeholders). Image-heavy or non-text-frame slides cannot be cloned (same image-only
  limitation as today, already handled by ADR-029 hard-reject).
- "Fill regions" must be identifiable heuristically — e.g., shapes whose text contains
  placeholder patterns ("TBD", "Insert here", empty strings), or shapes whose names in the
  XML match common patterns. Ambiguous when a slide mixes content and decorative text.
- Dynamic content overflow: if extracted data is longer than the reference text it replaces,
  text boxes will overflow slide boundaries. python-pptx has no auto-shrink; requires explicit
  truncation or font-size reduction logic.
- Non-placeholder shapes (decorative headers, logos, fixed callouts) are cloned as-is, which
  is correct for matching the reference's look, but requires the reference to be clean.
- The reference PPTX must be retained durable beyond the authoring session. The current
  `ArtifactStore.cleanup(synth_id)` at session DONE (mcp_server.py lifespan) must be deferred
  for reference artifacts that are bound to promoted skills — a lifecycle extension not yet
  implemented.

**What it touches:**
- `analyze_artifact.py:_analyze_pptx` (extend to extract shape_map)
- `pptx_renderer.py` (new `_render_template_clone` method + dispatch branch)
- `layout_catalog.py` (new preset: `template_clone` or reference-driven rendering signal)
- `conversation.py` (pass reference bytes path to renderer at execution time)
- `ArtifactStore` lifecycle (durable retention of reference artifact post-promotion)
- `comparator.py:_extract_pptx_sections` (sections from blank-layout slides via textbox scan, not
  just `shapes.title` — fixes the 0.0 scoring even under the current approach)

**Interaction with ADRs:**
- Directly implements the "skill by demonstration" promise of ADR-015.
- Resolves the ADR-026 Gap 5 comment that a binary template "works but is a blob in git" — here
  the template is the USER-SUPPLIED reference, not a repo-checked-in blob.
- Requires extending ADR-029 comparator scoring to handle blank-layout slides (the
  `shapes.title = None` bug, which is actually independent of this option and should be fixed
  regardless).
- Does NOT require ADR-028 multimodal analysis — the template clone is structural, not visual.

**Scope and effort:** Medium. ~3-5 days (shape-map extraction: 1d; template clone renderer: 2d;
ArtifactStore lifecycle extension: 0.5d; comparator blank-layout fix: 0.5d; tests: 1d).

---

### Option B — Structured Layout-Spec Extraction (the ADR-028 §8 Research Path)

**Approach**: Deep (possibly multimodal) analysis of the reference slide at UPLOAD_ARTIFACT_EXAMPLE
time produces a structured layout spec: a serializable description of every shape's position,
dimensions, content type, and visual role. The renderer consumes this layout spec to build the
slide programmatically, independent of any hardcoded preset. The layout spec is committed to the
workflow YAML artifact, making the skill durable.

**What a layout spec looks like (conceptual):**
```yaml
slide_layout_spec:
  slide_dimensions: {width_in: 13.333, height_in: 7.5}
  background_color: "#FFFFFF"
  shapes:
    - id: header_band
      type: rectangle
      position: {left_in: 0, top_in: 0, width_in: 13.333, height_in: 0.45}
      fill_color: "#C74634"
      content_role: decorative
    - id: left_col_table
      type: table
      position: {left_in: 0.25, top_in: 1.1, width_in: 7.2, height_in: 5.9}
      rows: 2
      columns: 1
      content_role: fill_region
      field_mapping: [scope, status_bullets_and_next_steps]
    ...
```

**Two sub-approaches:**

*B1 — python-pptx-only (deterministic, text-bearing only):*
Walk the reference PPTX's XML tree via python-pptx and extract position, size, and shape type
for all shapes. Infer `content_role` heuristically (text-filled shapes are `fill_region`; colored
rectangles with no text are `decorative`; images are `image`). This works without LLM or vision.

*B2 — Multimodal (image-only or mixed slides, spec §8):*
Render the reference slide to a raster image (via `python-pptx`'s `slide.shapes` iteration or
a headless LibreOffice/Pillow export), send to a vision-capable LLM, and ask it to produce a
structured layout spec. This is the ADR-028 §8 open problem: OCI GenAI does not currently
expose a vision model (ADR-029 §F: "OCI vision model availability is a real constraint").

**Fidelity ceiling:**
- B1: high for text-bearing slides; fails silently for image-only or complex decorative slides.
- B2: very high theoretically; blocked on OCI vision model availability (or requires a second LLM
  provider). Feasibility is currently UNKNOWN — this is the spec §8 research task.

**What it touches:**
- `analyze_artifact.py:_analyze_pptx` (major extension — geometry extraction, shape classification)
- A new `LayoutSpecExtractor` module (framework/renderers/layout_spec_extractor.py or similar)
- `pptx_renderer.py` (new spec-driven renderer path that consumes layout spec dict)
- `workflow_skill` YAML schema (new `slide_layout_spec` field committed to the artifact)
- `DESIGN_SKILL` prompt (inject layout spec alongside artifact_layout hint)
- `layout_catalog.py` (new `spec_driven` preset as the dispatch trigger)
- `comparator.py` (blank-layout scoring fix is a prerequisite regardless)

**Interaction with ADRs:**
- This IS the ADR-028 §8 open problem for multimodal analysis.
- B1 (deterministic) is feasible without vision LLM but limited to text-bearing slides — it is
  essentially a deeper version of Option A's shape_map.
- B2 (multimodal) is blocked by OCI vision model availability (ADR-029 §F) and the lack of a
  known feasible implementation path today.
- ADR-034's preset catalog would need a new entry (or a bypass mechanism) for spec-driven
  rendering, since the current catalog only has `weekly_exec_review_v1` and `default`.

**Scope and effort:**
- B1 (deterministic geometry extraction + spec-driven renderer): Large. ~8-12 days.
  (Spec extractor: 3d; spec-driven renderer: 3-4d; DESIGN_SKILL prompt changes: 1d;
  YAML schema extension: 0.5d; tests: 2-3d).
- B2 (multimodal): Unknown. Blocked until vision-LLM becomes available on OCI GenAI.
  Estimated additional effort on top of B1: +3-5 days (vision call, spec validation, fallback).

This is the general solution to spec §8. The research investment is substantial and the
multimodal path has an unresolved external dependency.

---

### Option C — Honest-Signal Interim: Block Promote on 0.0 Fidelity (No Layout Fix)

**Approach**: Keep the current hardcoded presets exactly as they are. Do not attempt to reproduce
the uploaded reference's layout. Instead, change the EVAL gate so that a comparator result of
structure_score=0.0 (or below a configured threshold, e.g. 0.1) is a HARD BLOCKER on PROMOTE —
not just a diagnostic signal — when a reference artifact is bound. This prevents a structurally
dissimilar PPTX from quietly promoting.

Separately, fix the comparator's blank-layout scoring bug (comparator.py:172 `shapes.title = None`
for blank-layout slides) so that 0.0 actually means "structurally dissimilar" rather than "sections
extractor failed to find titled slides."

**What changes:**
- `comparator.py:_extract_pptx_sections` (lines 157-185): add a fallback path for blank-layout
  slides. When `slide.shapes.title` is None, scan `slide.shapes` for text-bearing textboxes and
  use the largest/first as the section title. (~15 lines)
- `conversation.py:_run_eval` (around line 5589-5624): add a PROMOTE blocker: when
  `has_bound_reference_artifact() == True` AND `comparator_result is not None` AND
  `comparator_result.structure_score < threshold`, refuse PROMOTE with a message that names
  the fidelity gap explicitly.
- The EVAL gap report (Section 3) already shows the scores; the options text at the end of `_run_eval`
  would gain a `[HIGH]` routing message when score < threshold (parallel to the routing self-test
  blocker at line 5791-5798).
- No changes to the renderer, the preset catalog, DESIGN_SKILL, or analyze_artifact.

**Fidelity ceiling**: Zero improvement in actual fidelity. The produced PPTX still looks nothing
like the reference. The option removes the silent-degradation aspect — the author is forced to
acknowledge the gap before promoting — but the gap itself remains.

**What it touches:**
- `comparator.py` (blank-layout section extraction fix — standalone, ~15 lines)
- `conversation.py:_run_eval` (promote gate addition for structure_score < threshold)

**Interaction with ADRs:**
- Directly implements the "loud, explicit" signal that ADR-029 intended for structural gaps
  but which is currently only diagnostic (ADR-029 §C.2 explicitly lists `WRONG_LAYOUT` as a
  failure class that routes back to `REVIEW_DESIGN`, but the routing map is "not yet active"
  per the EVAL options text at conversation.py:5805).
- Does NOT address the ADR-015 "skill by demonstration" premise.
- Does NOT advance the ADR-028 §8 research.
- Can be implemented in parallel with or as a prerequisite to Options A or B (the scoring fix
  is needed regardless).

**Scope and effort:** Small. ~1-2 days.
(comparator blank-layout fix: 0.5d; promote gate: 0.5d; tests: 0.5d).

---

## Recommendation

**Recommended sequence: Option C now (prerequisite fix) + Option A as the primary fidelity upgrade.**

Rationale:

**Option C first** (0.5-1 day): The comparator's blank-layout scoring bug (comparator.py:172,
`shapes.title = None`) is the reason structure_score=0.0 even when the produced and reference
slides share some semantic content. This is a standalone bug fix that should land regardless of
which larger option is chosen. Adding a promote blocker at structure_score < threshold is the
minimum viable "honest signal" — it prevents the current pattern where a 0.0 score passes
silently.

**Option A next** (3-5 days): Template-clone rendering is bounded, implementable with
python-pptx today (no vision LLM dependency), and directly fulfills the "skill by demonstration"
premise for the primary use case: the user uploads a text-bearing PPTX, the produced output
inherits its geometric structure, and the sections are populated with live extracted data. This
is the right scope for v1 reference-fidelity.

**Option B deferred**: The spec §8 research path (structured layout spec, especially the B2
multimodal variant) is unblocked OCI vision-model availability and requires 8-12+ days of
research/implementation with uncertain yield. It remains the right long-term answer but should
not block Option A's simpler, bounded implementation. B1's deterministic geometry extraction
is essentially a superset of Option A's shape_map — it can be pursued as an evolution of A
once A is proven.

**What is OUT of scope for this decision:**
- Any changes to the PPTX output for skills that do NOT have an uploaded reference artifact
  (the existing preset behavior is correct for those skills).
- Vision-LLM analysis of image-only reference slides (ADR-029 hard-reject is the correct
  behavior until OCI vision is available).
- Backfilling existing promoted `weekly_exec_review_v1` skills to a template-clone path
  (those skills were authored without a clone-ready reference; they are correct by their own
  design).
- Changing the PROMOTE gate mechanism beyond the structure_score threshold guard in Option C
  (the full ADR-029 Phase 2 replan-routing loop is a separate workstream).

---

## Cross-References

| Reference | Relevance |
|---|---|
| ADR-015 | "Skill by demonstration" — the gap this decision addresses |
| ADR-026 | Origin of `weekly_exec_review_v1` preset (Fix 5); hardcoded geometry |
| ADR-028 | `ANALYZE_ARTIFACT` does decoration, not design; §8 open problems include multimodal artifact analysis |
| ADR-029 | Comparator architecture; blank-layout scoring gap; image-only hard-reject |
| ADR-034 | Layout preset catalog; only 2 presets registered; no reference-clone preset |
| DECISION-014 | No-internal-preset-ID-in-prompts rule (context for why preset list is limited) |
| DECISION-019 | RC2 — layout-id prose resolution; confirmed fixed; produced artifact is real OOXML |
| spec §8 | Open problem: faithful arbitrary-layout rendering from uploaded reference |

---

## What the User Must Decide

1. **Sequence / priority**: Do Option C (honest-signal interim) first before any renderer work,
   or skip straight to A?
2. **Option A vs B1 for the primary fidelity upgrade**: Template-clone (bounded, ships sooner)
   or structured layout-spec extraction (general, more research)?
3. **Comparator blank-layout bug** (comparator.py:172): confirm this should be fixed standalone
   regardless of which larger option is chosen (the architect recommends yes).
4. **Reference artifact durability**: Option A requires the reference PPTX to survive beyond
   session DONE (`ArtifactStore.cleanup`). Confirm: should promoted skills retain a pointer to
   their reference artifact permanently, or only through the authoring session?
5. **Promote-gate threshold** (Option C): what structure_score threshold should block PROMOTE
   when a reference is bound? (Suggested: 0.1 — anything below 10% structural match is a
   hard blocker; 0.0 is always a blocker. This can be configurable per skill.)

---

*Status: Proposed — awaiting user decision. No implementation done. No ADR status changes. No
framework code changes.*
