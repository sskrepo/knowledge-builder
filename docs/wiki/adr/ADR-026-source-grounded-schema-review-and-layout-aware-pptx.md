---
title: ADR-026 — Source-grounded schema review + layout-aware PPTX rendering
status: accepted
created: 2026-05-13
owner: architect
deciders: user, tpm
tags: [adr, skill-builder, rendering, pptx, llm-in-authoring]
related: [ADR-015, ADR-016, ADR-021]
---

# ADR-026 — Source-grounded schema review + layout-aware PPTX rendering

## Status

Accepted (2026-05-13).

## Context

An end-to-end `authorSkill` session for `tpm.weekly_exec_review_26ai` completed
and reached PROMOTE, but the rendered PPT was "junky" relative to the reference
slide the user supplied (Oracle slide 15 — single slide, two-column layout,
Scope+Assumptions+Status+Next Steps left, Key Milestones+ORM+Risk sidebar right,
real 26ai data, Oracle template styling).

An architectural audit surfaced five structural gaps:

### Gap 1 — `_analyze_pptx` returns nothing for image-only slides

`framework/skill_builder/analyze_artifact.py::_analyze_pptx` reads slide titles
and body text frames via python-pptx. When the uploaded reference artifact is an
image-only PPTX (a single PICTURE shape, 0 text frames, 0 tables) the function
returns `(["title"], None)` — essentially nothing. Downstream field discovery
then falls through to keyword heuristics ("weekly" + "status" in task.lower()).

The single LLM call at `_llm_analyze_artifact` (line 521, `_ANALYZE_ARTIFACT_PROMPT`)
only assigns `{type, description}` to fields the heuristic **already picked** —
it never sees the artifact visually and never proposes new fields.

### Gap 2 — `sampler.py` always reads fixtures, never live Confluence

`fetch_samples` checks `KBF_STORE_BACKEND` and dispatches to `_fetch_from_fixtures`
or `_fetch_from_adapter`. The production adapter path is a stub that returns
synthetic placeholders. The user's declared Confluence URL
(pageId=20030556732) is never fetched during `authorSkill` — only at ingest
time, after PROMOTE. So field discovery, schema review, and extraction preview
all run against placeholder data, not the actual 26ai page content.

### Gap 3 — `review.py::review_extractions` calls `_extract_stub` unconditionally

`_extract_stub` is case-insensitive key matching on flat dicts. The "production
LLM extraction" that ADR-015 §REVIEW promises does not exist. The PREVIEW state
shows a fake extraction that does not reflect what the LLM will actually
produce at query time.

### Gap 4 — No source-grounded schema coherence check at REVIEW_SCHEMA

Between REVIEW_FIELDS and REVIEW_SCHEMA, the framework synthesises field
descriptions from artifact structure alone. It never fetches source content
and therefore cannot:
- flag fields the declared source pages cannot support
- suggest fields the source clearly contains but the artifact missed
- warn that the schema's enum values differ from what the source uses

### Gap 5 — `PptxRenderer` produces one slide per section key

`framework/renderers/pptx_renderer.py::render` iterates `data["sections"]` and
creates one slide per entry using layout[1] (the blank content layout). For the
reference slide-15 layout (single slide, two-column, Oracle template look) this
produces 6-10 slides with plain content blocks, none matching the desired
structure.

## Decision

### Fix 1 — Hard-fail `_analyze_pptx` for image-only slides (no stub fallback)

When `_analyze_pptx` finds 0 text shapes, it raises `ValueError` with a message
directing the user to either (a) supply a text-bearing PPTX/DOCX/Markdown, or
(b) enter field names manually. A vision-LLM path for image-only slides is
deferred to ADR-027 (requires multimodal model support).

Rationale: no-stub-mode policy. Silently falling through to keyword heuristics
is a form of stub mode — it produces fields that have no connection to the
artifact or the source.

### Fix 2 — `sampler.py` fetches live Confluence when page IDs/URLs are available

`fetch_samples` gains a new path: if `source_query` contains `{"page_id": "..."}`
or `{"page_url": "..."}`, it calls `_build_confluence_adapter` (same factory used
at INGEST) and fetches the page directly, regardless of `KBF_STORE_BACKEND`.
This requires no new adapter — it reuses `ConfluenceEmcpDirectAdapter.fetch`.

The `filestore` fallback is retained for sources that have no page-id (label-only
Confluence queries, Jira, Git). A hard-fail is thrown only if the caller explicitly
passes `require_live=True` and the adapter is unavailable.

### Fix 3 — `review.py::review_extractions` calls LLM when wired; hard-fails otherwise

`review_extractions` gains an `llm` parameter. When `llm is not None`, it
substitutes `_llm_extract` for `_extract_stub`. The LLM receives the schema
properties plus the raw sample text and returns a JSON object — the same pattern
used in `WorkflowExecutor._llm_extract_fields`. When `llm is None`, the function
raises `RuntimeError` — no silent stub fallback.

The existing `_extract_stub` remains in the module but is only reachable in tests
that explicitly pass `stub_mode=True`.

### Fix 4 — Source-grounded schema coherence review at REVIEW_SCHEMA (PRIMARY FIX)

A new method `_source_grounded_review` is inserted at the end of
`_advance_to_review_schema`, BEFORE the prompt is rendered to the user.

Algorithm:
1. For each configured Confluence source that has a page ID or URL, call
   `sampler.fetch_samples` (Fix 2) to get 2-3 live items.
2. Build a prompt: (intent, candidate_schema, sample_content) → ask the LLM to:
   a. Flag fields in the schema the sample content cannot support (mark
      `"supportable": false`)
   b. Suggest fields present in the sample content that are missing from the
      schema (return as `"suggested_additions"`)
   c. For enum fields, compare declared enums to actual values seen in the
      sample
   d. Return a short prose note to display to the user alongside the schema
3. The LLM findings are attached to `data["source_review"]` in the
   `REVIEW_SCHEMA` ConversationTurn so the user can see them.
4. The session continues regardless of whether the LLM agrees with the schema —
   the grounded review is advisory, not blocking.

Cost: one additional LLM call per `_advance_to_review_schema`. At typical Confluence
page size (~5-15k chars truncated to 8k) and a 2048-token response budget, this
adds ~3-5 seconds of latency to the REVIEW_SCHEMA transition.

State machine impact: REVIEW_SCHEMA state is unchanged. No new state is inserted
(inserting a state would require DECISION-NNN per ADR-015 §State machine). The
source-grounded review output is surfaced inside the existing REVIEW_SCHEMA message.

### Fix 5 — Layout-aware PPTX rendering for `weekly_exec_review_v1`

Option selected: **extend `PptxRenderer` to support a `layout` directive**, not
a separate renderer class and not a hand-authored PPTX template.

Rationale:
- Option A (hand-author .pptx template + slide_mapping YAML): works but the
  template file is a binary blob in git; any structural change requires binary
  editing. Fragile for CI.
- **Option B (programmatic layout, selected)**: `PptxRenderer.render` inspects
  `data.get("layout")`. If `"weekly_exec_review_v1"`, it delegates to
  `_render_weekly_exec_review_v1(data)`, which builds the single-slide two-column
  layout programmatically via python-pptx. Self-contained Python; no binary blob
  dependency; testable with a PPT parse assertion.
- Option C (new renderer class): violates the registry contract (registry keys
  `output_format`, not layout) and duplicates renderer boilerplate.

`_render_weekly_exec_review_v1` builds:
- Widescreen (13.33" × 7.5") blank slide
- Title text box top-left: `data["title"]` (e.g. "FA DB Upgrade to 26ai")
- Jira ID text box top-right (accent color): `data["jira_id"]` if present
- Left column (55% width) — 2-row table:
  - Row 1: Scope (single line from `data["scope"]`)
  - Row 2: Assumptions + Status bullets (with keyword bolding for
    Completed/Approved/In Progress/On Hold) + Next Steps
- Right sidebar (40% width) — 3 stacked boxes:
  - Key Milestones (from `data["key_milestones"]` list)
  - ORM Status (from `data["orm_status"]`)
  - Risk/Mitigation (from `data["risks_mitigations"]` list)
- Oracle brand palette: header band #C74634 (Oracle red), body bg #FFFFFF,
  section header text #C74634, body text #3D3D3D

The `weekly_exec_review_26ai` workflow YAML gains:
```yaml
synthesis:
  output_format: pptx
  layout: weekly_exec_review_v1
```

The executor passes `layout` through to the renderer via `data["layout"]`.

## Consequences

### Positive

- Source-grounded schema review surfaces real findings against live 26ai Confluence
  content BEFORE the skill is committed — the #1 quality lever (per ADR-015).
- `review_extractions` now shows what the LLM will actually extract from real
  samples, not a placeholder.
- The PPT produced for `weekly_exec_review_26ai` matches the reference slide-15
  structure without requiring a binary PPTX template in the repo.
- No new states in the state machine = no breaking change to session persistence.
- Hard-fail on image-only artifacts removes a silent quality degradation.

### Negative / Costs

- One additional LLM call at `_advance_to_review_schema` (~3-5 s added latency
  during `authorSkill`). Acceptable: this is an authoring flow, not a real-time
  query.
- `review_extractions` now requires an LLM client at call sites that previously
  ran without one. Test suites must pass a mock LLM. Legacy call sites that
  relied on the stub path will break at import-time if they don't pass `llm`.
- Layout-aware rendering is coupled to field name conventions
  (`scope`, `jira_id`, `key_milestones`, etc.). Any schema rename requires a
  matching renderer update — acceptable because the schema is TPM-authored and
  stable once promoted.

### Reversibility

- Fix 1 (image-only hard-fail): low friction to revert if vision-LLM support
  (ADR-027) makes the hard-fail unnecessary.
- Fix 4 (source-grounded review): the LLM call is additive; removing it is a
  single-function deletion.
- Fix 5 (layout renderer): the `if data.get("layout") == "weekly_exec_review_v1"`
  branch is self-contained; removing it falls back to the existing multi-slide path.

## Spec §6 interface impact

No Protocol changes. `PptxRenderer.render(data, template)` signature is
unchanged — `layout` is passed inside `data`, consistent with how the executor
already passes `title` and `sections`. `review_extractions` gains an `llm`
keyword argument (default `None`); existing callers that don't pass it will
hit the hard-fail, which is intentional.

## Alternatives considered

### "Rely solely on user intent quality"
The user writes a detailed intent and manually enters field names. If they also
write good descriptions at REVIEW_SCHEMA, quality is acceptable. Rejected:
the whole premise of skill-by-demonstration is that the system sees the same
sources and corroborates the schema automatically.

### "Fully LLM-driven, drop heuristics entirely"
Remove all keyword heuristics from field inference; always use LLM for every
step. Rejected: the spec principle #3 says "Deterministic extraction rules over
autonomous LLM extraction". Heuristics provide a reliable structural backbone;
LLM is used for the content-grounded refinement layer on top.

### "Insert a new SOURCE_REVIEW state between REVIEW_FIELDS and REVIEW_SCHEMA"
This would give the source-grounded review its own state with explicit
accept/reject by the user. Rejected for this ADR: adding a state requires
DECISION-NNN per the ADR-015 §State machine rule (state count = session
serialization format = backward-compat surface). The advisory findings can be
surfaced inside REVIEW_SCHEMA without a new state, and the user already has
edit commands at that state. If the advisory review proves important enough to
warrant a dedicated state, DECISION-010 will be filed.

### "Use a hand-authored PPTX template binary"
A `.pptx` template file checked into git provides pixel-perfect Oracle branding.
Rejected: binary blobs in git are non-diffable, require out-of-band tooling to
edit, and a corrupt template silently produces bad output. Programmatic
construction from python-pptx is fully transparent and testable.

## References

- [ADR-015 — Skill-by-demonstration](ADR-015-skill-by-demonstration.md)
- [ADR-016 — Workflow skills](ADR-016-workflow-skills.md)
- [ADR-021 — Artifact upload](ADR-021-artifact-upload-oci.md)
- Reference slide: `/Users/sravansunkaranam/.kbf/store/uploads/synth-tpm-9d3b6233/art-c665fcc6/faaas-slide15-reference.pptx`
- Failing session: `synth-tpm-9d3b6233` (image-only PPTX, zero text shapes)
