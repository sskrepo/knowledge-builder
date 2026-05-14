---
title: authorSkill conversation flow — state-by-state LLM usage map
owner: architect
created: 2026-05-14
updated: 2026-05-14
related: [ADR-015, ADR-026, ADR-016, ADR-017]
---

# authorSkill conversation flow — state-by-state LLM usage map

## Executive summary

5 of 15 states use the LLM. The LLM is involved at the following inflection
points: (1) **ANALYZE_ARTIFACT** — one LLM call assigns types and extraction
instructions to every field discovered in the uploaded artifact; (2)
**REVIEW_FIELDS** → **REVIEW_SCHEMA** transition — a second LLM call synthesizes
descriptions for user-added delta fields (fields not already covered by call 1);
(3) **REVIEW_SCHEMA** (ADR-026 Fix 4) — a third LLM call fetches 2-3 live
Confluence pages and performs a source-grounded coherence check, flagging
unsupportable fields and suggesting missing ones; (4) **PREVIEW** — for the
extraction preview, `review_extractions` calls the LLM once per sample to
simulate what the parser will extract at query time (ADR-026 Fix 3).
Everything else — persona identification, source/trigger configuration, field
editing, validation, ingest, eval (current implementation), and promotion — is
heuristic or deterministic. The eval step is still a stub: it loads gold set
counts but does not execute the harness or compute recall/faithfulness.

---

## IDENTIFY_PERSONA

**Trigger.** `start()` is called without a persona, or `start()` is called with
a persona but no intent, leaving the session at this state. The user then calls
`respond()` with a combined "persona — intent" string.

**What runs.** `conversation.py:_prompt_identify_persona` (line 421) reads
available personas from `_list_available_personas()` (module-level helper) which
scans the `framework/persona_builders/` directory. On `respond()`,
`_handle_identify_persona` (line 439) splits the input on em-dash/colon,
validates the persona against the scanned list, slugifies the intent, and
transitions to ANALYZE_ARTIFACT.

**LLM involvement.** No LLM — fully heuristic/deterministic. Persona validation
is a string match against filesystem directory entries. Intent is slugified via
`_slugify` (regex, no model call).

**External I/O.** Reads `framework/persona_builders/*.yaml` to enumerate known
persona names.

**Output / next state.** Sets `_data.persona`, `_data.intent_description`,
`_data.skill_name` (slug) on session. Transitions to ANALYZE_ARTIFACT.

---

## ANALYZE_ARTIFACT

**Trigger.** IDENTIFY_PERSONA completes and the user sends the path to a
reference artifact (PPTX/DOCX/MD/TXT) or a comma-separated field list.

**What runs.**
1. `_handle_analyze_artifact` (conversation.py:609) branches on the input type.
2. For local filesystem paths: calls `analyze_artifact.analyze_artifact`
   (analyze_artifact.py:20), which dispatches to `_analyze_pptx`, `_analyze_docx`,
   or `_analyze_markdown` depending on file extension. Each parser reads the file
   with the relevant library (python-pptx, python-docx) and produces a
   `(fields: list[str], mapping: dict | None)` tuple via deterministic heading/
   title extraction.
3. For uploaded artifacts (`artifact:` prefix, ADR-021): `_handle_uploaded_artifact`
   (conversation.py:637) resolves the artifact via `ArtifactStore.resolve()`,
   then calls the same `analyze_artifact` path.
4. For manual field lists: `_parse_fields_from_input` (conversation.py:2286)
   splits on commas/newlines and normalises to snake_case.
5. After any of the above, `_llm_analyze_artifact` (conversation.py:524) is
   called.

**LLM involvement.** LLM (model=`synthesis`) called once in `_llm_analyze_artifact`.
Prompt: `_ANALYZE_ARTIFACT_PROMPT` (conversation.py:111) — given persona,
intent, artifact type, and per-field context (raw section title + up to 200
chars of body text), asks the model to assign a JSON Schema type and a 1-2
sentence extraction instruction to each field. Returns `{}` gracefully if no
LLM is wired or on any failure.

**External I/O.** Reads the artifact file from the local filesystem (or
ArtifactStore path). For uploaded artifacts: calls `ArtifactStore.resolve()`
which maps an `artifact_id` to a local path.

**ADR-026 note — image-only PPTX.** `_analyze_pptx` (analyze_artifact.py:41)
now counts all text shapes across all slides. If `total_text_shapes == 0`, it
raises `ValueError` with an actionable message (line 95). No keyword-heuristic
fallback. Vision-LLM for image-only slides is deferred to ADR-027.

**Output / next state.** Sets `_data.fields`, `_data.slide_mapping`,
`_data.llm_suggested_specs`. Transitions to REVIEW_FIELDS by calling
`_handle_review_fields_prompt`.

---

## REVIEW_FIELDS

**Trigger.** ANALYZE_ARTIFACT completes and presents a field list to the user.

**What runs.** `_handle_review_fields_response` (conversation.py:720). Parses
single-line commands (`add <field>`, `remove <field>`, `rename <old> to <new>`)
via `_parse_field_edits` (module-level helper, regex-based). `ok` or equivalent
affirms the list and calls `_advance_to_review_schema`.

**LLM involvement.** No LLM — fully heuristic/deterministic. All edits are
string parsing.

**External I/O.** None.

**Output / next state.** Mutates `_data.fields` in-place. On confirmation,
transitions to REVIEW_SCHEMA via `_advance_to_review_schema`.

---

## REVIEW_SCHEMA

**Trigger.** User types `ok` at REVIEW_FIELDS. The session calls
`_advance_to_review_schema` immediately (before returning the REVIEW_SCHEMA
prompt to the user).

**What runs** (three-pass algorithm, conversation.py:753):

Pass 1 (no extra LLM call). Fields already covered by `llm_suggested_specs`
(populated during ANALYZE_ARTIFACT) get their `type` and `description` applied
directly from the cached LLM output via `_infer_field_spec`
(synthesize_schema.py:167) for the base spec shape.

Pass 2 (LLM call, conditional). Delta fields — fields not in `llm_suggested_specs`
(user-added after artifact analysis, or the LLM missed them) — are sent to
`synthesize_field_descriptions` (synthesize_schema.py:36). When `self._llm is
not None`, this calls `_llm_synthesize_descriptions` (synthesize_schema.py:75)
which fires one LLM call (model=`synthesis`, prompt=`_DESCRIPTION_SYNTHESIS_PROMPT`)
with field-name + raw_title/body_text context from the slide mapping. Falls back
to heuristic on failure or when no LLM is wired.

Pass 3 (ADR-026 Fix 4 — LLM call, conditional). `_source_grounded_review`
(conversation.py:853) is called with the fully assembled `field_specs`:
- Finds Confluence sources in `_data.sources` that have a `page_id`, `page_url`,
  or `pages` list.
- For up to 2 source entries (up to 2 pages each), calls `sampler.fetch_samples`
  (sampler.py:34) with `adapter_name="confluence"` and the page identifier.
  `fetch_samples` dispatches to `_fetch_confluence_live` (sampler.py:105) which
  builds the Confluence adapter via `_build_confluence_adapter` and calls
  `adapter.fetch(RawItemRef(...))` directly — live Confluence content, not
  fixtures.
- Combines up to 8k chars of page content, then fires one LLM call
  (model=`synthesis`, prompt=`_SOURCE_GROUNDED_REVIEW_PROMPT`, conversation.py:815)
  asking the model to return a JSON object with `unsupportable_fields`,
  `suggested_additions`, `enum_corrections`, and `summary`.
- Attaches the result to `data["source_review"]` in the ConversationTurn so
  the user sees the findings alongside the schema.
- Returns `None` silently on any failure (advisory, never blocking).
- Skipped entirely when no LLM is wired or when no Confluence sources with
  page IDs/URLs are configured.

**LLM involvement.**
- Pass 2: LLM (model=`synthesis`) called once in `_llm_synthesize_descriptions`
  for delta fields only. Prompt purpose: synthesize precise extraction
  instructions from artifact section context.
- Pass 3: LLM (model=`synthesis`) called once in `_source_grounded_review`.
  Prompt purpose: cross-check the candidate schema against live Confluence page
  content, surfacing unsupportable fields and missing fields.

**External I/O.**
- Pass 3 live Confluence fetch: `sampler._fetch_confluence_live` calls the
  configured Confluence adapter (`emcp_direct`, `codex_proxy`, `codex_cli`, or
  `mcp`). This is a real network call to Confluence when the adapter is
  configured.

**Output / next state.** Populates `_data.field_specs` for all fields. Sets
`_state = "REVIEW_SCHEMA"`. Renders a ConversationTurn with the field specs
and, if source review ran, a `source_review` section. The user can edit specs
with `describe <field> as <text>`, `set type of <field> to <type>`,
`set maxLength/enum` commands, or multi-line bulk edits, all handled by
`_apply_single_schema_command` (conversation.py:1159) — deterministic string
parsing. `ok` transitions to CHECK_REUSE.

---

## CHECK_REUSE

**Trigger.** User types `ok` at REVIEW_SCHEMA.

**What runs.** `_advance_to_check_reuse` (conversation.py:1239) instantiates
`ShimKb` from the `framework/persona_builders/` directory, then calls
`reuse_detector.detect_reuse` (reuse_detector.py:5). `detect_reuse` calls
`shim.cards_visible_to(persona)` and for each required field checks whether
any existing KB's `provides_fields` list covers it. Returns `{covered: {},
gaps: []}`.

**LLM involvement.** No LLM — deterministic field-set membership check against
in-memory KB cards.

**External I/O.** Reads `framework/persona_builders/*.yaml` (via ShimKb) and
the ADB `KBF_PERSONA_BUILDERS` table (if skill_store is wired, ShimKb Pass 2
merges ADB production entries on top of YAML seeds per ADR-015 Option B).

**Output / next state.** Sets `_data.reuse_result`. Presents covered/gap
summary. If no covered and no gaps (empty KB set), skips directly to
CONFIGURE_SOURCES. `yes` transitions to CONFIGURE_SOURCES; `no` returns to
REVIEW_FIELDS.

---

## CONFIGURE_SOURCES

**Trigger.** CHECK_REUSE confirmed by user.

**What runs.** `_advance_to_configure_sources` (conversation.py:1293).
Auto-extracts Confluence URLs and `pageId=N` references from
`_data.intent_description` via `_extract_confluence_sources_from_text`
(module-level regex helper) if no sources have been configured yet.
`_handle_configure_sources_response` (conversation.py:1343) parses each
user-supplied source descriptor via `_parse_source_descriptor` (module-level
parser) into a structured dict (`{kind, space, labels, page_id, page_url,
pages, ...}`). The user types `done` when finished.

**LLM involvement.** No LLM — regex-based URL/page-id extraction and
structured parsing.

**External I/O.** None. Sources are parsed and stored in memory only; actual
live fetches happen later at REVIEW_SCHEMA (ADR-026, for source-grounded review)
and INGEST.

**Output / next state.** Appends to `_data.sources`. On `done`, transitions to
CONFIGURE_TRIGGERS.

---

## CONFIGURE_TRIGGERS

**Trigger.** User types `done` at CONFIGURE_SOURCES.

**What runs.** `_advance_to_configure_triggers` (conversation.py:1364) presents
three trigger options (on-request only, scheduled only, both) plus output format
selection. `_handle_configure_triggers_response` (conversation.py:1384) calls
`_parse_trigger_input` (module-level helper, regex/split-based) to parse the
user's reply into a trigger dict and output format string.

**LLM involvement.** No LLM — regex parsing.

**External I/O.** None.

**Output / next state.** Sets `_data.trigger` and `_data.output_format`.
Transitions to PREVIEW via `_advance_to_preview`.

---

## PREVIEW

**Trigger.** CONFIGURE_TRIGGERS completes.

**What runs.** `_advance_to_preview` (conversation.py:1390) calls
`_synthesize_preview` (conversation.py:2140) which assembles all artifacts:
1. `synthesize_extraction_schema` (synthesize_schema.py:139) — deterministic
   JSON Schema construction from `_data.field_specs` and `_data.fields` via
   `_infer_field_spec` heuristics. No LLM call.
2. `synthesize_persona_builder_diff` (synthesize_builder.py) — deterministic
   YAML structure.
3. `seed_gold_set` + `seed_workflow_gold` (gold_seed.py) — deterministic
   template-based JSONL stubs.
4. `synthesize_workflow_skill` (synthesize_workflow.py:13) — deterministic YAML
   structure from intent + fields + trigger config.

The extraction preview for the PREVIEW state is *not* where `review_extractions`
is called in the current code. `review_extractions` (review.py:21) is a
standalone module; the PREVIEW state surfaces a text summary of the artifact
paths, not a live extraction demo. `review_extractions` is available to the
route handler as a separate action but is not wired into this state's
`_advance_to_preview` path.

**LLM involvement.** No LLM in the current PREVIEW state path. `_synthesize_preview`
is fully deterministic. `review_extractions` (which does call the LLM per
ADR-026 Fix 3) is available as a module-level utility and can be called by the
REST route handler, but it is not called from `_advance_to_preview` or
`_handle_preview_response`.

**Note on ADR-026 Fix 3 wiring.** `review.py::review_extractions` (review.py:21)
now calls `_llm_extract` (model=`synthesis`) instead of `_extract_stub` when
`llm` is passed. The prompt (`_REVIEW_EXTRACT_PROMPT`, review.py:110) gives the
model the schema property list and raw sample text, asking for a JSON extraction
that mirrors what the ingest-time parser will produce. Passing `llm=None` without
`stub_mode=True` raises `RuntimeError` (hard-fail, ADR-026 no-stub-mode policy).
However: the route handler must explicitly call `review_extractions` and pass the
`llm` argument — the conversation state machine does not call it automatically.

**Layout-aware PPTX note.** When the workflow YAML carries `synthesis.layout:
weekly_exec_review_v1`, `PptxRenderer.render` (pptx_renderer.py:46) dispatches
to `_render_weekly_exec_review_v1` (ADR-026 Fix 5). This builds a single-slide
two-column Oracle-style layout programmatically. This renderer is invoked at
query time (WorkflowExecutor `_synthesize` step), not during the authorSkill
conversation itself.

**External I/O.** Writes artifact files to filesystem under `REPO_ROOT` (all
artifact paths relative to project root). Does not write to ADB at this stage.

**Output / next state.** Sets `_data.synthesized_artifacts`. Presents a list
of artifact paths and content summaries to the user. `yes/commit` transitions
to CONFIRM (which immediately calls `_handle_commit`). Any other input loops
in PREVIEW.

---

## CONFIRM

**Trigger.** User types `yes` or equivalent at PREVIEW.

**What runs.** `_handle_confirm_response` (conversation.py:1424) delegates
directly to `_handle_commit` (conversation.py:1427). No intermediate state
prompt.

**LLM involvement.** No LLM.

**External I/O.** See COMMITTED below — all I/O is in `_write_artifacts`.

**Output / next state.** Delegates entirely to `_handle_commit`; transitions
to COMMITTED on success.

---

## COMMITTED

**Trigger.** User confirmed commit at PREVIEW/CONFIRM. `_handle_commit` calls
`_write_artifacts` (conversation.py:2204).

**What runs.** `_write_artifacts` (conversation.py:2204):
1. Iterates `_data.synthesized_artifacts`.
2. Serialises each artifact to text (YAML via `yaml.safe_dump` or JSON via
   `json.dumps`).
3. Infers `artifact_type` from the relative path (regex matching on
   `workflow_skills`, `.yaml.new_kb`, `-extraction.jsonl`, `-workflow.jsonl`,
   `parsers/schemas`).
4. Writes to filesystem (`REPO_ROOT / rel_path`) unconditionally.
5. Calls `skill_store.write_artifacts(synth_id, persona, skill_name, artifacts)`
   to write all typed artifacts to ADB. Hard-fails on any exception (never
   advances past PREVIEW on an ADB write failure — BUG synth-tpm-14a54555 fix).

**LLM involvement.** No LLM — file serialisation and ADB write.

**External I/O.**
- Writes 3-5 files to filesystem: extraction schema JSON, workflow skill YAML,
  persona builder delta YAML, two eval gold set JSONL files.
- Writes all typed artifacts to ADB (`KBF_SKILL_ARTIFACTS` table) via
  `AdbSkillStore.write_artifacts`.

**Output / next state.** Sets `_data.committed_paths`. Transitions to
COMMITTED state. Presents the committed path list and offers `yes, run full
pipeline` / `just validate` / `stop here`. `stop` transitions to DONE.

---

## VALIDATE

**Trigger.** User responds at COMMITTED with `yes` or `just validate`.

**What runs.** `_run_validate` (conversation.py:1483):
1. Loads the `workflow_skill` artifact from ADB via
   `skill_store.read_artifact(persona, skill_name, "workflow_skill")` and writes
   to a named tempfile (falls back to filesystem path when ADB read fails).
2. Loads the `persona_builder_delta` from ADB; if present, creates a merged
   temp persona-builders directory that includes the in-session KB alongside the
   disk YAMLs (BUG-queue-6c173 fix). Falls back to reading
   `{persona}.yaml.new_kb` from disk if ADB delta is absent.
3. Calls `validate_workflow_links(wf_path, merged_pb_dir)` (validate_links.py:19)
   which: parses the workflow YAML, builds a KB index from all persona builder
   YAMLs, and checks that every `requires_extractions[].required_fields` entry is
   covered by the linked KB's `provides_fields` (ADR-017 link check).
4. Cleans up temp files/dirs.

**LLM involvement.** No LLM — deterministic YAML-structure validation.
`validate_workflow_links` is a pure graph-traversal function.

**External I/O.** Reads from ADB (`skill_store.read_artifact`). Reads YAML files
from `framework/persona_builders/`. Writes/reads temp files (deleted after validation).

**Output / next state.** Sets `_data.validation_result`. On failure, offers
`retry` / `skip` / `stop here`. On pass, offers `yes, ingest` / `skip to eval` /
`stop here`. All paths leading forward transition to INGEST.

---

## INGEST

**Trigger.** User types `yes, ingest` at VALIDATE (or skips validation).

**What runs.** `_run_ingest` (conversation.py:1667):
1. Builds a Confluence adapter via `_build_confluence_adapter(kbf_env, REPO_ROOT)`
   (conversation.py:37). Mode is determined by `KBF_ENV` env var and the
   merged `confluence.yaml` + env-specific override config.
2. Instantiates `WikiMetadataStore` (stores.wiki_metadata_store) and
   `ConfluenceWikiIngestor(adapter, wiki_store)`.
3. For each Confluence source in `_data.sources`:
   - If `pages` list is present: calls `ingestor.ingest_pages(pages)` (fetches
     each page directly by URL or page-id).
   - Otherwise: calls `ingestor.ingest_space(space, labels)`.
4. Counts `pages_new + pages_updated + pages_unchanged`. If any source returns
   0 pages, it is classified as a failure (hard-fail policy — BUG synth-tpm-
   14a54555 fix: zero-page result from codex is no longer treated as success).
5. Jira and Git sources are logged as "Phase 2, not yet wired" — no ingest
   happens for them.

**LLM involvement.** No LLM in the ingest state machine. The
`ConfluenceWikiIngestor` uses deterministic Confluence-to-Markdown conversion.

**External I/O.** Real network calls to Confluence via the configured adapter.
Writes ingested pages to `WikiMetadataStore` (filesystem index at
`~/.kbf/store/wiki_metadata/`).

**Output / next state.** Sets `_data.ingest_result` with status, page counts,
and mode. On failure, stays at INGEST and blocks PROMOTE. On success, transitions
to EVAL.

---

## EVAL

**Trigger.** User types `yes, run eval` at INGEST (or skips ingest).

**What runs.** `_run_eval` (conversation.py:1874):
1. Calls `gold_set_feeder.count_entries(persona)` (eval module) to get a count
   of persona-level gold set entries.
2. If `skill_store` is available, attempts to load `eval_extraction` and
   `eval_workflow` artifacts from ADB and writes them to tempfiles (so a future
   eval harness can find them by path). Tempfiles are immediately deleted after
   creation (the cleanup loop at line 1921 runs before the harness would use
   them — this is a gap; the tempfiles are written then deleted before being
   consumed).
3. Sets `_data.eval_result` with status=`"stub"`, gold set counts, gold set
   paths, and null metrics.

**LLM involvement.** No LLM. The eval harness is not executed.

**STUB WARNING.** The eval state is currently a stub. It does not run the eval
harness, does not call any retrievers, and does not compute `recall@k`,
`faithfulness`, latency, or cost. `metrics.recall_at_k` and
`metrics.faithfulness` are both `null`. The `exit_criteria.passed` field is
`null`. The message to the user explicitly says "In production this would run
queries against the KB and measure recall@5, faithfulness, latency, and cost."

**External I/O.** Reads ADB for eval artifacts (optional, falls back to
filesystem paths). Writes then immediately deletes tempfiles.

**Output / next state.** Sets `_data.eval_result`. Transitions to PROMOTE on
`yes, promote`. Transitions to DONE on `stop`.

---

## PROMOTE

**Trigger.** User types `yes, promote` at EVAL.

**What runs.** `_run_promote` (conversation.py:1969):
1. Guards against promoting when `ingest_result.status == "failed"` (hard-fail,
   returns to INGEST state).
2. Returns a ConversationTurn asking the user to confirm promotion.
   `_handle_promote_response` (conversation.py:2005) handles the confirmation.

On `yes`:
1. Calls `skill_store.promote(persona, skill_name)` — updates the skill's
   status to `production` in ADB. Hard-fails on any exception (session stays at
   PROMOTE — BUG synth-tpm-14a54555 fix).
2. Reads the `persona_builder_delta` artifact from ADB via
   `skill_store.read_artifact(...)`.
3. If found, calls `skill_store.upsert_persona_builder_kb(persona, kb_name,
   content_yaml, status="production")` to write the KB entry to the
   `KBF_PERSONA_BUILDERS` ADB table (ADR-015 Option B).
4. Deletes any stray `{persona}.yaml.new_kb` from disk.

**LLM involvement.** No LLM — ADB writes and filesystem cleanup.

**External I/O.** ADB writes: `skill_store.promote` (updates
`KBF_SKILL_SESSIONS.status`), `skill_store.upsert_persona_builder_kb` (writes
to `KBF_PERSONA_BUILDERS`). Filesystem: optionally deletes stray `.new_kb` file.

**Note on layout-aware rendering.** The `weekly_exec_review_v1` PPTX layout
(ADR-026 Fix 5) takes effect at this point onward. When the promoted skill is
invoked via `POST /api/v1/ask`, the WorkflowExecutor passes `layout:
weekly_exec_review_v1` (from the workflow YAML `synthesis.layout` field) to
`PptxRenderer.render`, which dispatches to `_render_weekly_exec_review_v1`
(pptx_renderer.py:55). This builds the single-slide two-column Oracle-style
layout programmatically at query time.

**Output / next state.** Transitions to DONE. Reports KB population status
(pages ingested count, or a warning if KB is empty).

---

## DONE

**Trigger.** Any state where the user types `stop` / `no` / `exit`, or PROMOTE
completes (either direction).

**What runs.** Returns a `ConversationTurn(state="DONE", done=True)`. The DONE
handler in `respond()` (conversation.py:308) is a lambda that returns the
terminal turn immediately.

**LLM involvement.** No LLM.

**External I/O.** None at this point. All writes have been done in prior states.

**Output / next state.** Terminal. Session ID is surfaced for resume via
`from_dict`.

---

## Known gaps

1. **Eval is a stub.** `_run_eval` does not execute the eval harness. Recall@k,
   faithfulness, latency, and cost are all null. The tempfile write-then-
   immediate-delete pattern (lines 1900-1926) means eval gold set artifacts read
   from ADB are written to disk and deleted before any harness could consume
   them. The eval harness wiring against live KB retrieval is the largest open
   item in the full skill lifecycle.

2. **Vision-LLM for image-only PPTX.** `_analyze_pptx` hard-fails on
   image-only slides (ADR-026 Fix 1). There is no fallback analysis path for
   artifacts where all structure is embedded in images (e.g. scanned slides or
   screenshot decks). ADR-027 is the planned vehicle for multimodal model
   support here.

3. **`review_extractions` not wired into the PREVIEW state.** ADR-026 Fix 3
   connected `review_extractions` to a real LLM extraction call (`_llm_extract`,
   review.py:128). However, the PREVIEW state handler (`_advance_to_preview`,
   conversation.py:1390) does not call `review_extractions`. The extraction
   preview must be triggered explicitly by the route handler. There is no
   extraction preview shown to the user during the standard authorSkill flow.

4. **`_fetch_from_adapter` is a stub for non-Confluence adapters.** In
   `sampler.py:257`, `_fetch_from_adapter` logs "production path not yet
   implemented" and returns synthetic stubs for any adapter that is not
   Confluence with a page_id/page_url. Jira, Git, and label-only Confluence
   queries fall through to fixtures (Phase 2).

5. **No intent-to-skill_card consistency check.** The skill_card generated by
   `synthesize_workflow.py:_build_skill_card` (line 95) uses only the first 200
   chars of the intent description. There is no LLM review step that checks
   whether the finished schema + workflow structure is consistent with the
   original intent. A skill could be promoted with a schema that partially
   drifts from what the user described.

6. **Source-grounded review skipped when sources are configured after
   REVIEW_SCHEMA.** `_source_grounded_review` reads `_data.sources` at the
   point `_advance_to_review_schema` is called. If the user has not yet entered
   sources at that point (which is the normal flow — sources are configured
   at CONFIGURE_SOURCES, which is *after* REVIEW_SCHEMA), the Confluence source
   list is empty and the review is skipped with a log message. The source-
   grounded review only fires on session *resume* when sources were entered in a
   previous session, or when sources are extracted from the intent text at
   CONFIGURE_SOURCES auto-population (which only writes to `_data.sources` after
   REVIEW_SCHEMA has already fired). This is the most significant functional gap
   in ADR-026 Fix 4 as deployed.

7. **Jira and Git ingest not wired.** `_run_ingest` logs "Phase 2, not yet
   wired" for `kind == "jira"` and `kind == "git"` sources and advances without
   ingesting them.
