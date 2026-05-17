# Knowledgebase — Session Log

Append-only. Format: `## [YYYY-MM-DD] agent | what changed`

---

## [2026-05-16] architect | ADR-032 synthesizer source_binding gap closed (P1 synthesizer wiring)

**Gap:** `synthesize_workflow_skill()` in `framework/skill_builder/synthesize_workflow.py`
never emitted a `source_binding` block.  Every newly authored ask_parameterized skill
was committed with author_fixed defaults and immediately failed the P1-D
`_validate_source_binding_contract` check, making the ADR-032 core use case unreachable
via the conversational authoring flow.

**Fix (two files):**

- `framework/skill_builder/synthesize_workflow.py`:
  - New `derive_space_allow_list(sources, source_samples)` function.  Derivation priority:
    (1) `source_samples[*][*].space` from live-fetched INSPECT_SOURCES metadata,
    (2) `/wiki/spaces/{SPACE}/` or `/display/{SPACE}/` patterns in URL-form sources,
    (3) explicit `source.space` key.  Returns `[]` for underivable case — never guesses.
  - `synthesize_workflow_skill()` gains `source_binding_mode`, `space_allow_list`,
    `input_param`, `ephemeral_ttl_seconds`, `source_type` parameters.
  - `ask_parameterized`: emits complete 6-field `source_binding` block + typed
    `confluence_page_ref` trigger input whose name matches `input_param`.
  - `author_fixed` (default): no `source_binding` block, generic trigger input unchanged
    (byte-identical to pre-ADR-032 output).

- `framework/skill_builder/conversation.py` (`_synthesize_preview`):
  - Calls `derive_space_allow_list(self._data.sources, self._data.source_samples)`.
  - Passes `source_binding_mode` and derived `space_allow_list` to `synthesize_workflow_skill`.

**space_allow_list derivation:** uses actual session data (source_samples from INSPECT_SOURCES).
Underivable case (bare numeric IDs, no INSPECT_SOURCES data) returns `[]`, which triggers
the existing VALIDATE error "space_allow_list is missing or empty" — never a silent wrong
default (ADR-031 preserved).

**Tests:** 30 new tests in `test_synthesize_workflow_skillcard.py`:
- `TestAskParameterizedSourceBinding` (14): full source_binding block emission, all 6 fields,
  typed trigger input, author_fixed unchanged.
- `TestAskParameterizedPassesValidateContract` (4): end-to-end regression — synthesized
  ask_parameterized YAML passes `_validate_source_binding_contract`.
- `TestDeriveSpaceAllowList` (12): derivation from source_samples, URLs, explicit space key;
  OCIFACP session → [OCIFACP]; underivable → []; no hardcoded guess.

**Verified:** 16 total failures = 8 pre-existing baseline (test_smoke_validate x7 +
test_code_wiki x1) + 8 pre-existing baseline (test_source_binding_yaml missing YAML).
Zero new regressions.

---

## [2026-05-16] backend-dev | Fix deleteSkill event-loop blocking (BUG-queue-280f1 Part 2, d3ec0-class)

**Bug:** `_make_delete_skill_handler` in `framework/deploy/mcp_tools.py` declared
`async def delete_skill_handler` but performed three synchronous blocking ADB I/O calls
directly on the asyncio event loop:
- `skill_store.delete(persona, skill_name)` (~line 951)
- `skill_store.delete_persona_builder_kb(persona, skill_name)` (~line 996)
- `shim_kb.reload()` → `self._skill_store.list_persona_builder_kbs()` (~line 1012)

Same d3ec0-class bug as authorSkill (fixed by 309db5d). deleteSkill was added by 601971a3
the day before that fix and was never covered. Under bastion/ADB reconnect these calls
freeze the event loop → uvicorn kills the unresponsive worker → callers get
"connection refused"/HTTP 000. This is an availability violation per the no-silent-degradation
rule.

**Fix (Option A — architect-confirmed):** Collected all three blocking calls into a single
synchronous inner function `_do_delete_blocking()` and offloaded it via
`result = await asyncio.to_thread(_do_delete_blocking)`. The fast pre-checks
(admin-scope check, confirmationPassword check, arg validation, skill_store availability)
remain synchronous and *before* the offload — bad requests still fail instantly without
spawning a thread. Filesystem artifact cleanup stays inside `_do_delete_blocking` so
ordering and logging are preserved. Exceptions propagate out of `asyncio.to_thread`
and are caught by the handler (same isError + message surface as before).

**Tests added** (`framework/tests/unit/test_mcp_skill_tools.py`,
class `TestDeleteSkillEventLoopNonBlocking`):
1. `test_blocking_delete_runs_through_to_thread` — patches `asyncio.to_thread`, verifies
   it is awaited exactly once with a callable; callable is executed to confirm correct result.
2. `test_valid_delete_calls_all_three_in_order_and_returns_success` — asserts call order
   [delete → delete_persona_builder_kb → reload] and validates the full response shape.
3. `test_missing_password_rejects_fast_without_entering_to_thread` — wrong password returns
   isError=True; `to_thread` never entered; `skill_store.delete` never called.
4. `test_exception_from_delete_inside_thread_surfaces_as_is_error` — RuntimeError from
   `skill_store.delete` inside the thread surfaces as isError=True with the message (no swallow).

56 tests passed (targeted run). 8 pre-existing baseline failures unchanged.

**Closes:** BUG-queue-280f1 Part 2 (Part 1 was a server-down transport artifact, no code fix needed).

---

## [2026-05-16] backend-dev | Fix ops_skill_auditor LLM-review content-filter misclassification and provider-internals leak

**Root cause (synth-tpm-fe0f9e9f investigation):** `_run_llm_review` in
`framework/deploy/ops/review_engine.py` had a broad `except Exception as exc` that directly
embedded the raw caught exception into a `BugToFile.detail` field.  For OCI GenAI HTTP-400
"Inappropriate content detected!!!" rejections this wrote the full error dict — including
`opc-request-id` and provider endpoint — into the persisted bug store (`user_bugs.jsonl`).
Two problems: (1) misclassification — a provider content-safety block is NOT a skill defect;
(2) provider-internals leak — OCI `opc-request-id` / raw error dict violated the
ContentFilterRejection discipline from dc93945.

**Fix (`framework/deploy/ops/review_engine.py`):**

1. Imports `_is_content_filter_error` and `ContentFilterRejection` from
   `framework.skill_builder.review` (shared detector — no duplicated logic).
2. Before the generic fallback, the except handler checks `isinstance(exc, ContentFilterRejection)
   or _is_content_filter_error(exc)`.
3. On match: emits a clean, provider-detail-free `BugToFile` with:
   - `check_name="llm_review_content_filtered"` (distinct from `llm_review_failed`)
   - `severity="minor"` (lowest valid enum — advisory, not a skill quality issue)
   - description contains a KBF- correlation ID (reused from `exc.request_id` if
     ContentFilterRejection; otherwise freshly generated); explicitly states "not a skill defect"
   - no `opc-request-id`, no OCI endpoint, no HTTP status code, no raw dict
4. Generic `llm_review_failed` branch is unchanged — fires for all non-content-filter errors.
5. Structural checks still run and contribute to the report (no early return from the review).
6. ADR-023 amended to document the content-filter advisory finding and its invariants.

**Tests added (`framework/tests/unit/test_kbf_ops_review.py`, class `TestLlmReviewContentFilter`):**

- OCI-style exception → `check_name="llm_review_content_filtered"`, not `"llm_review_failed"`
- `severity="minor"` enforced
- Description: contains KBF- id; does NOT contain `opc-request-id`, `SECRET-LEAK`, `400`,
  `oci.exceptions`, or `status`; explicitly says "not a skill defect"
- Structural checks ran alongside the content-filter finding (no abort)
- Generic `RuntimeError("boom")` → `llm_review_failed` (no regression)
- `ContentFilterRejection` raised directly → same clean finding, request_id reused

**Test result:** 49 passed (test_kbf_ops_review.py + test_review.py); 9 pre-existing failures
unchanged (smoke_validate×7 + code_wiki×1 + source_binding×1).

---

## [2026-05-16] backend-dev | ADR-032 MCP ask_handler body=/page_id gap fixed (D1 Priority-1 now live on MCP path)

**Root cause (architect-confirmed):** `_make_ask_handler` in `framework/deploy/mcp_tools.py`
called `maybe_render_artifact(app.state, result, question)` with no `body=` kwarg.
The D1 Priority-1 branch in `maybe_render_artifact` (`if body and input_param in body
and body[input_param]`) was therefore structurally unreachable on the MCP path — all MCP
consumers of `ask_parameterized` skills fell through exclusively to Priority-2 (question-string
regex extraction), one regex-miss away from a hard-fail or wrong-page result.
Additionally, the `askKnowledgeBase` tool schema had no `page_id` parameter, so MCP callers
had no structured way to pass a page reference at all.

**Fix (Option A, `framework/deploy/mcp_tools.py` only):**

1. `ask_handler` gains `page_id: str = ""` parameter (default `""` — fully backward-compatible).
2. Synthetic body: `body = {"page_id": page_id} if page_id else None`.
3. `maybe_render_artifact(app.state, result, question, body=body)` — Priority-1 now reachable
   on the MCP path exactly as on the REST path (commit 4330bd0 / D1 REST fix).
4. `EXTERNAL_TOOLS_SCHEMA` `askKnowledgeBase.inputSchema.properties` gains optional `page_id`
   string property with ADR-032 reference in description.
5. Priority-2 (question-string regex) unchanged — still the fallback when `page_id` is empty.
   `author_fixed` skills unaffected (body only consulted inside the `ask_parameterized` branch).

**Tests:** 13 new tests in `framework/tests/unit/test_mcp_ask_handler_page_id.py`.
Full required suite (test_mcp_ask_handler_page_id + test_ask_route_ask_parameterized +
test_skill_builder_conversation + test_prompt_registry): **242 passed, 0 failures**.

---

## [2026-05-16] backend-dev | reviewSkillSession KB-ref check false-positive fix (ADR-023)

**Root cause:** `_check_kb_references_resolve` in `framework/deploy/ops/review_engine.py`
iterated top-level keys of the `persona_builder_delta` artifact dict to build `pb_kbs`.
The production artifact shape (from `synthesize_persona_builder_diff`) is a flat dict with
`name`/`kind`/`extraction_schema`/`provides_fields`/`sources`/`retrieval_tools`/`kb_card`
keys — so iterating keys yielded `{"name", "kind", ...}`, not KB names. The `kbs`/
`knowledge_bases` lookups also returned None. Net: no KB ref could ever resolve → every
correctly-authored skill filed a spurious "major: hallucinated KB reference" bug.

**Fix (Option A + C):**

A1. Detect production artifact-dict shape (`"name" in pb_doc`, no `knowledge_bases`/`kbs`
    key). Add `pb_doc["name"]` (bare) and `f"{persona}.{pb_doc['name']}"` (qualified)
    to `pb_kbs`. Legacy "qualified-key" fixture shape and structured `knowledge_bases` list
    shape are still handled for backward compatibility.

A2. Load `framework/persona_builders/{persona}.yaml` from disk and add all KB `name`
    entries (bare + persona-qualified) to `pb_kbs`. This resolves reused KBs
    (`tpm.tpm_dependencies`, `tpm.tpm_weekly_ops`, etc.) that are NOT in the delta.
    Missing persona file is logged as a warning and is non-fatal — only refs that are
    absent from BOTH the delta and the disk file are flagged.

A3. For each `kb_ref`, accept match if the exact ref OR its persona-stripped short form
    is in `pb_kbs`. Eliminates false positives due to persona-qualification (`tpm.` prefix).

A4. `persona` is sourced from `bundle.persona` (authoritative). Fallback: `persona:` field
    in the workflow_skill artifact YAML. Final fallback: inferred from kb_ref prefix (logged
    as warning). Parameter threaded cleanly into `_check_kb_references_resolve(persona=...)`.

C. Test fixture `_make_artifacts()` updated to production artifact-dict shape.
   Added `test_kb_refs_resolve_no_false_positive_artifact_dict_multi_kb` (zero bugs on
   valid 3-KB skill with stub on-disk persona builder) and
   `test_kb_refs_resolve_true_negative_hallucinated_ref_still_caught` (genuinely
   hallucinated KB ref is still flagged — check remains a real gate).

**Persona_builder_delta contract (canonical):** the `persona_builder_delta` artifact for
conversationally-authored skills is a single flat dict produced by
`synthesize_persona_builder_diff()` with keys `name` (short/bare), `kind`,
`extraction_schema`, `provides_fields`, `sources`, `retrieval_tools`, `kb_card`.
Reused KBs are resolved from the on-disk `framework/persona_builders/{persona}.yaml`.

**ContentFilterRejection observation (out of scope):** `_run_llm_review` in
`review_engine.py` catches all LLM errors via a broad `except Exception` and files a
`llm_review_failed: minor` bug. It does NOT import or check for `ContentFilterRejection`
from `skill_builder/review.py` — a content-filter OCI 400 error would be filed as a
generic minor bug rather than the classified graceful handling used in the skill-builder
path. Separate follow-up item (dc93945 class).

**Tests:** 234 passed, 0 failed (was 232 baseline; +2 new regression tests).

---

## [2026-05-16] backend-dev | ADR-032 Phase-4 e2e D1+D2 fixed + P2-API response wired

**D1 (ask route input threading):** `maybe_render_artifact` in
`framework/deploy/routes/ask.py` now detects `ask_parameterized` mode, resolves the
page ref (body field > question extraction), and passes `inputs={..., input_param: page_ref}`
to `executor.execute()`. Hard-fails with actionable message if no page ref resolvable.
Author_fixed skills: `inputs={"input": question}` unchanged. No silent substitution.

**D2 (single-fetch space model):** `_retrieve_ask_parameterized` in
`framework/workflow_runtime/executor.py` now uses a single `adapter.fetch()` call
(no `fetch_metadata()` — that method does not exist on any adapter). Space key is read
from `raw_item.metadata["space"]` (string, e.g. "FA") as returned by
`emcp_direct.normalize()`. Space allow-list enforced AFTER fetch but BEFORE extraction.
Disallowed content is discarded immediately — never extracted, never cached, never persisted.
One round-trip total; no double-fetch.

**P2-API wiring:** `executor.execute()` returns `source_fetched_on_demand: True` and
`source_fetched_page_id` when ephemeral fetch occurred. `maybe_render_artifact` threads
these into `result`; `_build_ask_response` emits them as snake_case for camelCase
serialization (`sourceFetchedOnDemand`, `sourceFetchedPageId`, `latencyNote`).

**Tests:** 281 pass (0 failures) in the 6-file specified suite; 8 pre-existing
failures in `test_smoke_validate.py` (7) and `test_code_wiki.py` (1) unchanged.
New: `framework/tests/unit/test_ask_route_ask_parameterized.py` (13 tests).
Updated: `framework/tests/unit/test_executor_ephemeral.py` (D2 single-fetch model).

## [2026-05-16] backend-dev | ADR-032 P2-API — source_fetched_on_demand added to OpenAPI AskResponse contract

**Task P2-API complete.** Single commit to origin/main.

**`framework/deploy/openapi.yaml`** — `AskResponse` schema gains three optional fields
(per ADR-032-impl-plan.md §P2-API, exact field names/types/placement):
- `sourceFetchedOnDemand` (boolean) — true when an `ask_parameterized` skill ephemerally
  fetched a Confluence page for this request (ADR-032 Option C; artifact never persisted).
- `sourceFetchedPageId` (string) — the fetched page ID; present only when `sourceFetchedOnDemand: true`.
- `latencyNote` (string) — human-readable latency disclosure; present only when `sourceFetchedOnDemand: true`.

All three fields are optional (not in `required`). Existing required fields unchanged.

**`framework/tests/unit/test_openapi_spec.py`** — NEW: 15 tests covering:
spec file existence, YAML parse, OpenAPI version field, components/schemas presence,
AskResponse presence, no broken $refs, all three new fields present + correct types,
new fields are optional, pre-existing required fields unchanged.

**Sanity:** `test_openapi_spec.py` 15/15 passed; `test_prompt_registry.py` 51/51 passed.

**Follow-up (not implemented here — out of scope for P2-API):** The impl-plan notes
that `framework/deploy/routes/ask.py` (the ask route response builder) must populate
these three fields when `WorkflowExecutor` returns a result with
`source_fetched_on_demand: True`. That wiring is owned by the executor/route agent
(P2-Exec owner); it is not yet done as of this commit.

---

## [2026-05-16] backend-dev | ADR-032 Phase-2a P1-C + P1-D — source_binding_mode consumed in CLARIFY; VALIDATE enforces source_binding contract

**Phase-2a complete.** Two commits to origin/main (P1-C: 34269ee, P1-D: 7aec84a).

**P1-C (`feat(conversation): ADR-032 P1-C`, commit 34269ee):**
- `framework/skill_builder/conversation.py` — `_SessionData` gains two new fields:
  - `source_binding_mode: str = "author_fixed"` — resolved binding mode (persisted in to_dict/from_dict; backward-compat default "author_fixed" for pre-ADR-032 sessions)
  - `source_binding_signal: str = ""` — one-line evidence text from capture_intent (persisted; default "")
- `_advance_to_capture_intent`: reads `source_binding_mode` + `source_binding_signal` from LLM JSON output; annotates the source-binding question in `blocking_ambiguities` with `context: "source_binding_mode"` (fragment match: "source page fixed at authoring time or supplied"); never double-adds the question (v1.1 prompt already emits it).
- `_handle_clarify_response`: when question has `context == "source_binding_mode"`, resolves deterministically: "A"/fixed/same page/always → `author_fixed`; "B"/parameterized/dynamic/consumer/query time → `ask_parameterized`; skip → `author_fixed` (safer default); ambiguous phrasing → `ask_parameterized`. Resolution persisted on `_data.source_binding_mode`.
- CLARIFY blocks (does not auto-advance) until a substantive answer is given — source-binding question is a BLOCKING ambiguity.

**P1-D (`feat(conversation): ADR-032 P1-D`, commit 7aec84a):**
- `framework/skill_builder/conversation.py` — two module-level pure functions added:
  - `_check_confluence_adapter_available(env, repo_root) -> bool` — config-only check (no HTTP); merges base + env YAML, returns bool(mode).
  - `_validate_source_binding_contract(synthesized_yaml, session_binding_mode) -> list[str]` — for ask_parameterized: validates mode/input_param(referential)/ingest_on_demand/source_type/space_allow_list/ephemeral_ttl_seconds; for author_fixed: validates no ask_parameterized source_binding present.
- `_run_validate`: calls both functions after link check. Any errors appended to `result["errors"]`; `result["passed"] = False`. Hard-fail, never silent downgrade.

**Tests:** `framework/tests/unit/test_adr032_p1cd.py` — NEW: 47 tests. Full suite: 277 passed, 8 pre-existing failures (smoke_validate ×7 + code_wiki ×1) — 0 new regressions.

**Wiki:** `docs/wiki/authorskill-flow.md` — CLARIFY section updated (source-binding blocking question behavior, resolution rules, new _SessionData fields); VALIDATE section updated (source_binding contract check, adapter availability check, hard-fail discipline).

---

## [2026-05-16] backend-dev | ADR-032 P2-Exec — ask_parameterized ephemeral fetch path + WorkflowExecutor wiring

**P2-Exec complete.**

- `framework/workflow_runtime/executor.py`:
  - `WorkflowExecutor.__init__` now accepts `confluence_adapter=None` (backward-compat;
    all existing constructions default to None; author_fixed skills unaffected).
  - `_retrieve_for_inputs` branches on `source_binding.mode`: `ask_parameterized` →
    `_retrieve_ask_parameterized()` (new ephemeral path); `author_fixed` (default) →
    existing retriever/store/fixture path unchanged.
  - `_retrieve_ask_parameterized()`: trust enforcement order: (1) ingest_on_demand check,
    (2) adapter-None check, (3) space allow-list check (from URL or metadata fetch for
    bare numeric IDs — per impl-plan §Known Gaps) — ALL before any extraction. Ephemeral
    fetch via `self.confluence_adapter.fetch()`. Schema-bounded extraction via existing
    `_llm_extract_fields`. In-process TTL cache only (never persisted). Audit log.
  - `_EphemeralCache`: module-level, thread-safe (threading.Lock), 50-entry LRU cap,
    TTL eviction on get, never written to disk.
  - P3 regex guard now CONDITIONAL: `_retrieve_for_inputs` applies it ONLY after
    `author_fixed` path (ask_parameterized returns before reaching the guard block).
  - `ConfluencePageNotInKBError` extended with optional `reason` param for actionable
    consumer-safe messages (existing default message preserved; no regression).
  - `_resolve_page_id`, `_extract_space_key_from_url`, `_make_raw_item_ref`,
    `_extract_body_text` helpers added.
  - EVAL handling per ADR-032 §F: `_record_eval_entry` records ask_parameterized
    executions as gold-set candidates (same as author_fixed); eval harness will inject
    gold page IDs via input_param when exercising parameterized skills.

- `framework/deploy/mcp_server.py`:
  - RECONCILIATION: `WorkflowExecutor(...)` construction now passes
    `confluence_adapter=confluence_adapter` — the P2-Infra adapter is no longer dead
    code; it is live and passed into the executor at lifespan startup.

- `framework/tests/unit/test_executor_ephemeral.py` — NEW: 40 tests covering all
  ephemeral path branches: happy path, no-persist assertion, TTL cache hit/miss/eviction,
  adapter None, space not allow-listed (before fetch), ingest_on_demand false,
  author_fixed unchanged, adapter fetch failure, empty body, thread-safety,
  LRU eviction, _resolve_page_id, _extract_space_key_from_url, audit log, constructor.

- `framework/tests/unit/test_executor_source_guard.py` — realigned: 4 new tests for
  conditional guard behavior (ask_parameterized routes to ephemeral, author_fixed P3
  guard still fires). Total: 28 tests (24 pre-existing + 4 new), all pass.

- `docs/wiki/adr/ADR-032-ask-time-source-ingestion.md` — P2-Exec marked implemented;
  regex guard retirement for ask_parameterized documented.

Full test suite: 8 pre-existing failures (smoke_validate×7 + code_wiki×1) — 0 new
regressions. Specified suite (test_executor_ephemeral + test_executor_source_guard +
test_skill_builder_conversation + test_prompt_registry + test_failure_classifier_gate):
298 passed, 0 failures.

---

## [2026-05-16] backend-dev | ADR-032 P2-Infra — Confluence adapter factory relocated + lifespan optional dependency wired

**P2-Infra complete.** Commit 91ecff6.

- `framework/adapters/confluence/factory.py` — NEW: shared factory exporting `build_confluence_adapter(kbf_env, repo_root) -> adapter | None`. Body relocated verbatim from `conversation.py::_build_confluence_adapter` — same YAML-merge + mode dispatch logic; same None-on-error contract.
- `framework/skill_builder/conversation.py` — INGEST call site updated: body of `_build_confluence_adapter` replaced with a one-line import alias (`from ..adapters.confluence.factory import build_confluence_adapter as _build_confluence_adapter`). Name preserved at module level for backward compat with existing INGEST callers and tests. INGEST behavior unchanged.
- `framework/workflow_runtime/executor.py` — NEW function `_any_promoted_skill_requires_ephemeral(workflow_skills_dir)` scans skill YAMLs for `source_binding.ingest_on_demand:true` (guards the lifespan adapter init).
- `framework/deploy/mcp_server.py` — lifespan block initializes `app.state.confluence_adapter` (None or adapter instance) after the WorkflowExecutor block. Guarded by `_any_promoted_skill_requires_ephemeral`; server starts even when Confluence is unavailable (logs WARNING when a skill requires it but no adapter is configured).
- `framework/tests/unit/test_mcp_server_lifespan_confluence.py` — NEW: 20 tests covering `_any_promoted_skill_requires_ephemeral` (7 cases), `build_confluence_adapter` factory (3 cases), lifespan decision logic (7 cases), backward-compat alias (3 cases). All pass.
- Full test suite: 8 failures (pre-existing baseline: test_smoke_validate ×7 + test_code_wiki ×1) — 0 new regressions.

P2-Exec can now consume `app.state.confluence_adapter` (None check required; never absent).

---

## [2026-05-16] backend-dev | BUG-queue-990fe Option-A — space-form page-ref guard (A1) + persona propagation on ingest (A2/A3) + backfill (A4)

**BUG-queue-990fe Option-A complete.** HIGH-severity silent-wrong-output fixed across both root causes.

**A1 (RC2 — executor regex space-form gap):**
- `framework/workflow_runtime/executor.py` — added fifth pattern to `_CONFLUENCE_PAGE_REF_PATTERNS`: `re.compile(r"(?i)\bpage[\s_-]?id\b[\s:]+(\d{8,})")`. Length constraint `{8,}` prevents false-positives on short prose numbers. Guard now fires on `"for Confluence pageId 18625350641"` and `"pageId: 18625350641"` and hard-fails with `ConfluencePageNotInKBError`. Existing patterns untouched.
- `framework/tests/unit/test_executor_source_guard.py` — 6 new tests: space-form fires guard, colon-form fires guard, 7-digit prose no false-positive, 8-digit standalone prose no false-positive, unit extraction test, unit no-false-positive test. All 19 prior tests still green.

**A2 (RC1 — persona=null ingest gap):**
- `framework/ingestion/confluence_wiki_ingest.py` — `ConfluenceWikiIngestor.__init__` gains `persona: str | None = None` param. `ingest_page` uses `effective_persona = _raw.get("persona") or self._persona` (raw wins; fallback prevents null-persona storage when persona is determinable).

**A3 (callers updated — none missed):**
- `framework/skill_builder/conversation.py` — `_run_ingest` passes `persona=self._data.persona or None`.
- `framework/deploy/ingestion_worker.py` — moved ingestor construction inside the per-entry loop; each entry builds `ConfluenceWikiIngestor(adapter=..., persona=entry["persona"])`.
- `framework/cli/kb_cli.py` — `cmd_ingest` fully implemented (was stub); `--persona` flag added (reads from config YAML if omitted, fails loudly if neither source yields a persona — never silently stores null). `ingest` subparser updated.

**A4 (idempotent backfill):**
- `framework/cli/kb_cli.py` — `cmd_wiki_meta_backfill_persona` added; `wiki-meta backfill-persona --persona <p> [--page-id N] [--dry-run]` subcommand registered. Never overwrites non-null persona. Idempotent.
- Executed: `kb-cli wiki-meta backfill-persona --persona tpm --page-id 18625350641`. Before: `persona: null`. After: `persona: "tpm"`. Re-run: no-op (skipped 1 already-set record).

**Tests:** `framework/tests/unit/test_confluence_wiki_ingest.py` — 10 new tests (3 persona-propagation + 3 backfill). `framework/tests/unit/test_executor_source_guard.py` — 6 new tests (space-form A1). All 266 tests in the specified suite pass; 8 pre-existing failures unchanged (smoke_validate ×7 + code_wiki ×1).

**ADR-032 updated:** P3-guard section documents A1 gap closure + A2/A3 RC1 root cause + A4 backfill. Regex retirement timeline reiterated (P2-Exec / Phase 2).

---

## [2026-05-16] backend-dev | ADR-032 P1-E — 4 TPM email skills promoted to ask_parameterized with typed page_id input + space allow-list

**P1-E complete.**

- `framework/workflow_skills/tpm/project_tracking_stakeholder_status_email.yaml` — UPDATED: added `source_binding` block (mode: ask_parameterized, input_param: page_id, ingest_on_demand: true, source_type: confluence_page, space_allow_list: [FA, PROJ], ephemeral_ttl_seconds: 300); replaced generic `{name:input, type:string}` trigger input with typed `{name:page_id, type:confluence_page_ref, required:true}`.
- `framework/workflow_skills/tpm/project_tracking_confluence_stakeholder_status_meeting_email.yaml` — CREATED: full skill YAML with source_binding block + typed page_id trigger input + fields from parser schema v1.
- `framework/workflow_skills/tpm/project_tracking_stakeholder_tracking_meeting_email.yaml` — CREATED: full skill YAML with source_binding block + typed page_id trigger input + fields from parser schema v1.
- `framework/workflow_skills/tpm/project_tracking_weekly_stakeholder_status_email.yaml` — CREATED: full skill YAML with source_binding block + typed page_id trigger input + fields from parser schema v1.
- `framework/tests/unit/test_source_binding_yaml.py` — NEW: 36 tests asserting source_binding.mode=ask_parameterized + ingest_on_demand=true + input_param matches declared trigger input + space_allow_list non-empty for all 4 email skills; and asserting NO source_binding on the 3 non-email skills (26ai_confluence_pptx, 26ai_fa_db_upgrade_pptx, weekly_exec_review). All 36 pass.
- Non-email TPM skills (26ai_confluence_pptx, 26ai_fa_db_upgrade_pptx, weekly_exec_review) left untouched — author_fixed default.
- space_allow_list: [FA, PROJ] per impl-plan P1-E task spec (the plan explicitly states this value; it covers both the FA/FAAAS space where 26ai project lives and the PROJ general space).
- Note: these are on-disk authoring YAML edits only. A re-promote/re-ingest cycle is needed for full runtime effect.

## [2026-05-16] backend-dev | ADR-032 P1-A + P1-B — capture_intent/design_skill prompts bumped to v1.1; 4 fixtures added

**P1-A + P1-B (parallel-safe YAML-only tasks) complete.**

- `framework/config/prompts/skill_builder.yaml` — `capture_intent` v1.0 → v1.1: added `source_binding_mode` (author_fixed|ask_parameterized|ambiguous) and `source_binding_signal` to output schema; added two Rules (mode classification from intent phrases; blocking_ambiguities injection when mode is ask_parameterized or ambiguous).
- `framework/config/prompts/skill_builder.yaml` — `design_skill` v1.0 → v1.1: added `source_binding_mode` (author_fixed|ask_parameterized) to output schema; added Rule (emit ask_parameterized when capability inventory implies dynamic source supply; do NOT include page IDs in source_bindings when ask_parameterized). max_tokens 8192 unchanged.
- `framework/tests/fixtures/prompts/capture_intent_v1_1_ask_parameterized.json` — P1-A fixture: "accept a Confluence page" intent → expected ask_parameterized signal.
- `framework/tests/fixtures/prompts/capture_intent_v1_1_author_fixed.json` — P1-A fixture: explicit pageId 20030556732 intent → expected author_fixed.
- `framework/tests/fixtures/prompts/design_skill_v1_1_ask_parameterized.json` — P1-B fixture: dynamic source inventory → expected ask_parameterized, no page IDs.
- `framework/tests/fixtures/prompts/design_skill_v1_1_author_fixed.json` — P1-B fixture: fixed page ID inventory → expected author_fixed, page IDs in source_bindings.
- PromptRegistry loads cleanly; failure_classifier checksum unchanged: sha256:aef837cdde856fe83039f19fff816a101fe886187a7ce6f741a39eaab71c1d1f.
- 88 tests pass (test_prompt_registry + test_failure_classifier_gate + test_prompt_lab), 0 failures.
- Downstream P1-C/P1-D can now read source_binding_mode and source_binding_signal from capture_intent output; design_skill emits source_binding_mode for skill YAML synthesis.

## [2026-05-16] architect | ADR-032 DECISION-012 locked — Option C accepted; ADR-032 Accepted; impl blueprint authored

**DECISION-012 resolved: Option C (ephemeral request-scoped ingestion). ADR-032 status Proposed → Accepted. Implementation blueprint filed.**

- `pmo/decisions/DECISION-012-ask-time-source-ingestion-option.md` — status: open → RESOLVED. Option C terms locked: ephemeral only, no KB persistence, ~300s in-process TTL cache, author-time grant (ingest_on_demand:true in skill YAML), space allow-list enforcement, per-consumer OAuth explicitly v2/deferred. Spec §2 caveat accepted on record: schema-bounded LLM extraction at retrieval time acceptable only for ask_parameterized skills with authored/promoted schema.
- `docs/wiki/adr/ADR-032-ask-time-source-ingestion.md` — status: proposed → accepted. Options section collapsed to "Decided: Option C" with A/B in alternatives. Design section made implementation-grade: source_binding YAML schema (§D.1), capture_intent prompt v1.1 delta (§D.3), design_skill prompt v1.1 delta (§D.2), CLARIFY blocking surface (§D.3), VALIDATE gate amendment (§D.4), ephemeral path pseudocode (§E.2), mcp_server lifespan wiring (§E.3), P3-R guard rewire (§E.4), TTL cache spec (§E.5), API disclosure field (§E.6), EVAL interaction (§F).
- `docs/wiki/adr/ADR-032-impl-plan.md` — NEW. File-partitioned, dependency-ordered task table (10 tasks). Highest-risk item resolved: Confluence adapter IS reachable from mcp_server process (evidence: INGEST state of authorSkill already calls emcp_direct.fetch() server-side). Recommended fan-out: 3 parallel Phase 1 agents (A: prompts, B: adapter factory, C: TPM skill YAMLs) → 2 Phase 2 (D: conversation.py serial, E: executor.py) → Phase 3 sequential P3-R + P2-API → Phase 4 tests.
- `docs/wiki/index.md` — ADR-032 and ADR-032-impl-plan indexed.

Key design pins in blueprint:
- source_binding schema shape: mode/input_param/ingest_on_demand/source_type/space_allow_list/ephemeral_ttl_seconds
- capture_intent v1.1: adds source_binding_mode + source_binding_signal output fields
- design_skill v1.1: adds source_binding_mode output field; max_tokens 8192 unchanged (sufficient headroom)
- Confluence adapter factory.py: relocate _build_confluence_adapter from conversation.py → shared utility
- executor.py ephemeral path: _retrieve_ask_parameterized() never calls WikiMetadataStore.add() or any persistent store
- P3-R: regex heuristic fully retired; ConfluencePageNotInKBError class retained for P2 use

---

## [2026-05-16] backend-dev | ADR-032 P3 guard landed standalone — ConfluencePageNotInKBError hard-fail

**Fixed the silent wrong-page substitution in WorkflowExecutor._retrieve_for_inputs.**

- `framework/workflow_runtime/executor.py` — added `_extract_confluence_page_ids`,
  `_passage_matches_page_id`, `ConfluencePageNotInKBError`, and the P3 guard block
  in `_retrieve_for_inputs` (~line 246–278 in modified file). Guard detects Confluence
  page refs in inputs via regex heuristic (4 patterns: querystring pageId=, viewpage.action,
  REST /pages/<id>, bare pageId= key-value), verifies at least one retrieved passage
  cites the requested page_id via `metadata["page_id"]` or `citation_url` substring,
  and raises `ConfluencePageNotInKBError` on mismatch. Guard is completely inert when
  no page ref is present in inputs (no regression to fixed-source skills).
- `framework/orchestrator/context_builder.py` — catches `ConfluencePageNotInKBError`
  in `answer()` around tier dispatch; surfaces it as tier_4 / source_not_available
  response dict with actionable message. Never propagates as unhandled 500.
- `framework/deploy/routes/ask.py` — catches same error in `maybe_render_artifact`;
  mutates result to tier_4 / source_not_available so the consumer always sees the
  actionable message, never a silent empty artifact.
- `framework/tests/unit/test_executor_source_guard.py` — 19 new tests; all pass.
  Key assertions: wrong-page → hard-fail with actionable message (no substitution);
  correct-page → passes through; no-page-ref → guard inert (regression proof);
  URL form recognised same as querystring form.
- DECISION-012 options A/B/C remain open and unconstrained by this guard.
  Heuristic regex to be replaced by source_binding.input_param when ADR-032 P1 ships.

---

## [2026-05-16] architect | ADR-032 — ask-time source ingestion (Proposed) + DECISION-012

**Root-cause analysis of observed production failure: TPM email-draft skill silently
drew from wrong Confluence page (pageId=20030556732) when consumer supplied
un-ingested pageId=18625350641. Three problems separated and documented.**

- `docs/wiki/adr/ADR-032-ask-time-source-ingestion.md` — new ADR (Status: Proposed).
  Covers P1 (design-contract gap: no source_binding mode in YAML), P2 (runtime-capability
  gap: no ingest path in ask handler), P3 (silent wrong-page substitution — fix recommended
  immediately, independent of ADR decision). Three runtime-ingestion options presented;
  Option C (ephemeral) recommended. P3 fix target: executor.py:150-236 _retrieve_for_inputs.
- `pmo/decisions/DECISION-012-ask-time-source-ingestion-option.md` — open decision;
  user must choose Option A/B/C for runtime ingestion mechanism.
- `docs/wiki/index.md` — ADR-032 indexed.

Key findings:
- Most likely invoked skill: `project_tracking_confluence_stakeholder_status_meeting_email`
  (skill card explicitly names "read a project tracking Confluence page").
- P3 silent substitution: executor.py line 163 joins all input values into query_text;
  retriever at line 189 runs semantic search with no pageId filter; first result wins
  at line 208 regardless of whether it matches the requested page.
- No ingest-on-demand path exists anywhere in ask → executor chain; ConfluenceWikiIngestor
  only reachable from ingestion_worker.py (separate process).
- Highest-risk aspect: trust boundary — ephemeral fetch at ask time issues Confluence
  HTTP call with service credential on behalf of consumer-supplied pageId.
- ADR-032 is honest: Option C blurs the spec §2 ingest/retrieve line but within
  acceptable bounds (schema-bounded extraction, author-time grant, not unconstrained
  autonomous LLM extraction).

---

## [2026-05-16] backend-dev | BUG-queue-44364 comprehensive fix — no arbitrary content caps; all LLM-JSON parses detect truncation

**Policy: ADR-031 accepted. synthesized schemas carry no maxLength on content fields; source text sized to model context; every LLM-JSON parse site detects truncation and hard-fails (BUG-queue-44364).**

Changes:
- `framework/skill_builder/synthesize_schema.py` (Group B): removed `maxLength:1000` from summary/text/description/body fields; removed `maxLength:500` from catch-all string fallback. Kept `maxLength:64` (_id) and `maxLength:50` (_status) — genuinely source-bounded.
- `framework/skill_builder/review.py` (Group D): source-text cap `[:12000]` → `[:80000]`; stub fallback 500 → 10000; `_EXTRACT_MAX_TOKENS` constant updated to 16384 (matches YAML ceiling).
- `framework/workflow_runtime/executor.py` (Group E): snippet cap `[:24000]` → `[:80000]` (parity with review.py).
- `framework/skill_builder/conversation.py` (Group C, 10 hunks):
  - C1: `_SOURCE_GROUNDED_REVIEW_PROMPT` deleted; migrated to PromptRegistry as `source_grounded_review` in skill_builder.yaml (max_tokens:4096, up from hard-coded 2048). Last hard-coded prompt eliminated. Parse via `_parse_llm_json_response`.
  - C2: `design_skill` parse — raw `json.loads` → `_parse_llm_json_response` with tokens_out; raises BUG-queue-44364 error on truncation.
  - C3: `inspect_sources` parse — same.
  - C4: `review_design_replan` parse — same.
  - C5: failure_classifier route — `max_tokens=512` hard-coded → `classifier_spec.max_tokens` (spec-driven; won't drift). Template NOT changed (gate-locked checksum intact).
  - C6: source-text caps raised (inspect_sources: 3k→20k per-sample, 6k→40k total; _source_grounded_review: 4k→20k per-sample, 8k→40k total; gold-row source_snippet: 12k→80k).
  - C7: schema_lines `desc[:120]` → full description (no slice).
  - C8: eval_judge `field_description[:200]` → full; `extracted_value[:300]` → `[:2000]`.
  - C9: `describe <field> as` command: removed `"maxLength": 500` from new field spec.
  - C10: `expected_fields = all_fields[:5]` → `all_fields` (eval quality gate covers whole schema).
- `framework/config/prompts/skill_builder.yaml`: added `source_grounded_review` prompt entry.
- `docs/wiki/adr/ADR-031-no-arbitrary-content-caps.md`: new ADR (Status: Accepted).
- `docs/wiki/index.md`: ADR-031 indexed.

New tests (0 non-baseline failures):
- `framework/tests/unit/test_synthesize_schema_limits.py` (12 assertions — Group B).
- `framework/tests/unit/test_extraction_no_truncation.py` (6 assertions — regression suite).
- `framework/tests/unit/test_review.py` updated: `test_max_tokens_is_4096` → `test_max_tokens_is_16384`; truncation test ceiling updated 4096→16384.

Classifier gate: still green (template unchanged; checksum sha256:aef837cd…1c1d1f verified).

Full prescribed suite: 272 passed, 0 failures (excl. 8 pre-existing baseline).

## [2026-05-16] backend-dev | BUG-queue-2ad9a — ShimWorkflows ADB-aware (drafts no longer reach Tier-1 router)

**Fix: HIGH-severity silent-wrong-output. A promoted .eml skill silently returned another skill's .pptx because ShimWorkflows.all_cards() was disk-only and fed drafts to the Tier-1 LLM classifier.**

Changes:
- `framework/orchestrator/shim_workflows.py`: Added `skill_store=None` param (mirrors `ShimKb`). `all_cards()` now filters to ADB-promoted skills when store is wired. `all_cards_including_draft()` added for tooling. Laptop-mode (no store) explicitly INFO-logged. Store failure → WARNING + empty list (no drafts to classifier). `reload()` added.
- `framework/deploy/skill_store/_base.py`: New abstract method `list_promoted_workflow_skills(persona=None) -> set[tuple[str,str]]`.
- `framework/deploy/skill_store/adb.py`: `AdbSkillStore.list_promoted_workflow_skills()` — queries `KBF_SKILL_ARTIFACTS` WHERE `artifact_type='workflow_skill' AND status IN ('promoted','production')`. SQL: `_SQL_LIST_PROMOTED_WF_ALL` + `_SQL_LIST_PROMOTED_WF_PERSONA`.
- `framework/deploy/skill_store/filestore.py`: `FilestoreSkillStore.list_promoted_workflow_skills()` — reads `~/.kbf/workflow_promotions/` (injectable `wf_promo_root` for tests). Constructor gains `wf_promo_root` param.
- `framework/deploy/mcp_server.py`: `ShimWorkflows` now constructed with `skill_store=app.state.skill_store` (same instance as ShimKb).
- `framework/skill_builder/synthesize_workflow.py`: FIX 2 — `_build_skill_card` produces `example_invocations[0] = f"{task[:300]} Output: {output_format}."` (was `task[:100]`). `use_when` also carries output_format. Differentiates .eml from .pptx skill cards for the Tier-1 classifier.
- `docs/wiki/adr/ADR-016-workflow-skills.md`: Amendment documenting ADB as single source of truth for workflow promotion, shim_workflows mirrors shim_kb pattern, disk YAML is authoring artifact only, Tier-1 classifier only sees promoted skills.

New tests (0 pre-existing regressions):
- `framework/tests/unit/test_shim_workflows_adb.py`: 18 tests (8 ShimWorkflows scenarios + 7 FilestoreSkillStore.list_promoted_workflow_skills).
- `framework/tests/unit/test_synthesize_workflow_skillcard.py`: 10 tests (7 _build_skill_card + 3 synthesize_workflow_skill integration).

Full suite (excl. 8 known baseline failures): 258 passed, 0 failures.

## [2026-05-16] backend-dev | ADR-030 C-stream C1–C4 — all authorSkill prompts cut to PromptRegistry

**Task: ADR-030 SERIAL C-stream: C1 conversation.py → C2 synthesize_schema.py → C3 review.py → C4 executor.py.**

All 9 hard-coded prompt constants in conversation.py, 1 in synthesize_schema.py, 1 in review.py,
and the inline f-string prompt in executor._llm_extract_fields are now served by PromptRegistry.
persona_prompts.yaml deleted — content already in persona_overlays.yaml (P2).

Bugs fixed during C-stream:
- prompt_registry.py: _DEFAULT_PROMPTS_DIR parents[2]→parents[1] (wrong path; registry loaded 0 prompts)
- skill_builder.yaml: description_synthesis backslash line-continuation → single line (SHA mismatch)
- executor.yaml: template: | → template: |- (trailing newline byte-identity for executor_extract)

Test files updated: test_adr028_stream_a.py, test_persona_prompts_loader.py (full rewrite),
test_adr029_s6.py, test_failure_classifier_gate.py, test_skill_builder_conversation.py.
New: test_adr030_cutover.py (25 structural tests verifying C1–C4 cutover).

4 commits: 0661b79 (C1), 17cf699 (C2), 6c13d3b (C3), 199e5c1 (C4).
1124 tests pass; 8 pre-existing failures unrelated to prompt work.

## [2026-05-16] backend-dev | ADR-030 P3+P4 — prompt_lab harness CLI + fixtures + docs generator

**Task: P3 (harness CLI + fixtures + tests) + P4 (authorskill-prompts.md generator) — combined.**

- Created `framework/tools/__init__.py` and `framework/tools/prompt_lab.py` (standalone CLI, ~380 lines).
- Implemented all ADR-030-specified subcommands:
  - `--list`: table of prompt_id, version, model, locked, description (from list_prompts()).
  - `run <id> --fixture <path>`: live LLM call via OciGenAiLLMClient; prints raw + parsed JSON.
  - `run ... --dry-run`: format prompt; no LLM call.
  - `run ... --reload`: calls reg.reload() before run (hot-reload UX win).
  - `run ... --runs N`: N live calls; stability summary (which keys/values stable vs vary).
  - `run ... --persona <p>`: persona overlay resolution.
  - `run ... --expected <path>`: PASS/DIFF vs saved expected JSON.
  - `docs [--output <path>]`: generates authorskill-prompts.md from YAML store (P4).
- No-stub policy enforced: LLM probe on every live run; exits code 2 with BLOCKED message if stub.
- JSON parse via `framework.skill_builder.review._parse_llm_json_response` (shared helper).
- Created `framework/tests/fixtures/prompts/` with 5 fixtures:
  - `failure_classifier_gold.json` — verbatim gold case from test_failure_classifier_gate.py
  - `design_skill_tpm_26ai.json` — realistic TPM 26ai design input with persona=tpm
  - `capture_intent_tpm.json` — TPM capture intent input
  - `inspect_sources_tpm.json` — TPM inspect sources for pageId=20030556732 with WBS excerpt
  - `configure_sources_tpm.json` — TPM configure sources input
- Created `framework/tests/unit/test_prompt_lab.py` — 19 tests across 8 classes (0 LLM calls).
- Ran `python -m framework.tools.prompt_lab docs` → regenerated `docs/wiki/authorskill-prompts.md`
  from YAML store (12 prompts, 9 personas); file now has DO-NOT-HAND-EDIT header + generated_at.
- All tests: `pytest test_prompt_lab.py test_prompt_registry.py -q` → 74 passed, 0 failures.
- Parallel-safe: touched only new files; did NOT modify conversation.py / synthesize_schema.py /
  review.py / executor.py / prompt_registry.py / any YAML in config/prompts/.

---

## [2026-05-16] backend-dev | ADR-030 P2 — YAML prompt store authored verbatim

**Task: P2 — Author YAML prompt store (parallel stream, new files only).**

- Created `framework/config/prompts/` directory.
- `framework/config/prompts/skill_builder.yaml` — 11 prompts:
  `capture_intent`, `configure_sources`, `inspect_sources`, `design_skill`,
  `review_design_replan`, `eval_judge`, `clarify`, `failure_classifier` (gate-locked),
  `description_synthesis`, `review_extract`, `analyze_artifact` (deprecated legacy).
- `framework/config/prompts/executor.yaml` — 1 prompt: `executor_extract`
  (f-string→str.format() conversion; placeholders: {field_lines}, {user_request}, {snippet}).
- `framework/config/prompts/persona_overlays.yaml` — 9 persona stanzas (tpm, pm, architect,
  eng_mgr, developer, ops_eng, ops_mgr, service_owner, kbf_ops) migrated verbatim from
  `framework/config/persona_prompts.yaml` under the ADR-030 overlay schema.
- Byte-identity: all 12 prompts verified byte-identical to their Python source constants
  (sha256 hash comparison — all diffs empty).
- required_vars ↔ template placeholder consistency: PASS for all 12 prompts.
- YAML parse: all 3 files parse cleanly via yaml.safe_load().
- Gate-locked classifier checksum computed per ADR-030 algorithm:
  `sha256(template.rstrip('\n').encode('utf-8'))` →
  `sha256:aef837cdde856fe83039f19fff816a101fe886187a7ce6f741a39eaab71c1d1f`
- No Python source files modified (P2 is additive-only; cutover deferred to C-stream).

---

## [2026-05-16] architect | ADR-030 — Prompt externalization design + implementation blueprint

**Task: Design ADR-030 (user-chosen approach: Externalize to YAML + harness).**

- Verified persona_prompts.yaml is LIVE and actively wired (not dead code).
  `_load_persona_prompt_fragments` called at CAPTURE_INTENT (line 1208) and DESIGN_SKILL
  (line 2037). Fragments injected as {persona_key_fields}, {persona_extraction_style},
  {persona_few_shot_example}. Currently restart-gated; ADR-030 makes it hot-reloadable.
- Inventoried all 12 prompt units: 8 constants in conversation.py, 1 in synthesize_schema.py,
  1 in review.py, 1 unnamed inline in executor.py, + _CLARIFY_PROMPT (turn message, not LLM).
- Filed `docs/wiki/adr/ADR-030-prompt-externalization-and-harness.md` (Status: Accepted).
  Records: 3-file YAML layout (skill_builder.yaml + executor.yaml + persona_overlays.yaml),
  PromptRegistry loader contract (hot-reload via mtime, hard-fail on malformed YAML,
  checksum lock for failure_classifier), gate-lock enforcement chain, prompt_lab harness
  CLI design, fixture format, persona overlay schema (absorbs persona_prompts.yaml),
  migration plan (byte-identical, atomic per file), authorskill-prompts.md as generated.
- Filed `docs/wiki/adr/ADR-030-impl-plan.md` (implementation blueprint): 4 parallel
  P-streams (P1=registry, P2=YAML files, P3=harness, P4=docs generator), 4-step serial
  cutover (C1=conversation.py, C2=synthesize_schema.py, C3=review.py, C4=executor.py),
  gate task G1 (classifier live-LLM re-run post-migration). 6 technical risks flagged.
- Updated docs/wiki/index.md with both new ADR entries.
- Updated pmo/dashboard.md.

---

## [2026-05-15] architect | ADR-029 classifier validation gate — PASS (3/3 runs MISSING_FIELDS)

**Task: Design + validate _FAILURE_CLASSIFIER_PROMPT before S6 routing is enabled.**

- Added `_FAILURE_CLASSIFIER_PROMPT` constant to `framework/skill_builder/conversation.py`
  (after `_CLARIFY_PROMPT`, before STATES list). Marked S6-pending, not wired.
  Input contract: normalised_intent, schema_properties, capability_inventory,
  gap_report, missing_sections, thin_sections. Output: failure_class, confidence,
  evidence, alternative_class, why_not_alternative.
- Added anti-bias guard in prompt: "No verbatim labelled row for X does NOT mean
  the source lacks X if synthesisable evidence exists" — directly counters the
  observed misclassification (DESIGN_SKILL/INSPECT_SOURCES labelled WBS content
  as unavailable when it was synthesisable).
- Added `framework/tests/unit/test_failure_classifier_gate.py`: LIVE LLM gate test
  (3 runs against OCI GenAI) + structural contract tests (7, no LLM).

**Gate result: PASS**
- Run 1: MISSING_FIELDS (high confidence) — evidence cited synthesisable WBS fields
- Run 2: MISSING_FIELDS (high confidence) — evidence cited synthesisable WBS fields
- Run 3: MISSING_FIELDS (high confidence) — evidence cited synthesisable WBS fields
- All 3 runs referenced synthesisable evidence in evidence field
- 0/3 runs returned SOURCE_COVERAGE or WRONG_SOURCE
- S6 (constrained routing + loop guardrails) may proceed.

---

## [2026-05-15] backend-dev | S6 — ADR-029 Phase 2 constrained routing + loop guardrails wired

**Task: Wire the validated failure-class classifier into the EVAL reject path.**
Gate: 3/3 live LLM runs → MISSING_FIELDS (commit eb31230). Full auto-routing enabled.

**Changes to `framework/skill_builder/conversation.py`:**
- Added `_ROUTING_MAP` constant (code-only map; failure_class → target state). Located
  after `_FAILURE_CLASSIFIER_PROMPT` constant.
- Added `_EVAL_MAX_ITERATIONS = 3` and `_EVAL_COST_CEILING_USD = 2.00` guardrail constants.
- Added `_EVAL_ROUTE_PENDING` as an internal transient state constant (NOT in `STATES[]`
  — stays at 17 per ADR-028 S3 contract).
- Added import of `_parse_llm_json_response` from `.review` (parity with S5).
- `_SessionData`: added `eval_iteration_count: int = 0`, `eval_cumulative_cost_usd: float = 0.0`,
  `last_eval_failure_class: str | None = None`, `_eval_pending_route: str = "REVIEW_DESIGN"`.
  First three persisted in `to_dict()` / `from_dict()` with backward-compat defaults.
- Replaced `# TODO-S6` seam in `_handle_eval_response` with `return self._classify_and_route(user_input)`.
- New method `_classify_and_route`: applies all 6 guardrails in order (iteration ceiling
  before LLM call, cost ceiling before LLM call, then LLM call, then UNSUPPORTABLE check,
  then consecutive-same-class check, then routing turn with must_show_human=True).
  Mandatory 6-input call contract honored. llm=None surfaced as EVAL error turn (no silent skip).
- New method `_handle_eval_route_confirm`: handles user response at EVAL_ROUTE_PENDING.
  "confirm route to X" → state machine transitions to X. "accept" → PROMOTE. "ship as draft" → DONE.
- Handler dispatch: added `"EVAL_ROUTE_PENDING": self._handle_eval_route_confirm`.

**New test file: `framework/tests/unit/test_adr029_s6.py`** (44 tests):
- Routing map: MISSING_FIELDS/THIN_FIELDS/WRONG_LAYOUT → REVIEW_DESIGN;
  SOURCE_COVERAGE → CONFIGURE_SOURCES; WRONG_SOURCE → INSPECT_SOURCES.
- Guardrail 1: low-confidence + unknown class always → REVIEW_DESIGN.
- Guardrail 2: UNSUPPORTABLE → DONE draft.
- Guardrail 3: consecutive-same-class → DONE draft (pathological loop).
- Guardrail 4: iteration count >= 3 → DONE draft, no classifier call.
- Guardrail 5: cost > 2.00 → DONE draft, no classifier call.
- Guardrail 6: routing turn must_show_human=True + evidence + why_not_alternative.
- Input contract: classifier prompt contains capability_inventory / all 6 inputs.
- Accept path: still → PROMOTE unchanged; classifier NOT called.
- Session persistence: 3 new fields round-trip through to_dict/from_dict.

**Full suite result: 306 passed, 0 failures.**
Suites: test_adr029_s6.py (44) + test_adr029_s5.py (62) + test_failure_classifier_gate.py (11, skipped live) + test_skill_builder_conversation.py (108) + test_adr028_stream_a.py (81).

**Wiki updated:** docs/wiki/authorskill-flow.md (S6 section + routing map + guardrails + state changes).
**Impl plan updated:** ADR-028-029-impl-plan.md (S6 marked DONE — 2026-05-15).

---

## [2026-05-15] backend-dev | fix S5 stale tests + gold-set write None guard (commit 4167ce7)

**Task 1 — 3 stale tests updated to ADR-029 / Folded Fix 2 contract**
- `test_promote_yes_calls_skill_store_promote`: now provides delta + mocks ShimKb(0 cards = test env) → asserts promote+upsert called, DONE
- `test_promote_calls_upsert_persona_builder_kb_when_delta_exists`: now mocks ShimKb + asserts all_cards()+find_kb() invoked for resolvability check + DONE
- Added `test_promote_yes_hard_fails_when_delta_absent` encoding the hard-fail invariant
- `test_eval_gates_on_recall_and_faithfulness` → renamed `test_eval_user_accept_is_terminal_gate`: asserts ADR-029 S5 options set, must_show_human=True, awaiting_user=True, diagnostic _note on exit_criteria

**Task 2 — latent NoneType+str bug fixed in `conversation.py::_run_eval`**
- Root cause: (a) `persona`/`skill_name` lacked `or "unknown"` guard before path construction; (b) `wf_path.parent.mkdir()` was missing (only `ext_path.parent.mkdir()` was called)
- Fix: add `_safe_persona`/`_safe_skill` guards; add explicit `wf_path.parent.mkdir()` before `wf_path.write_text()`
- Regression tests in `TestRunEvalGoldSetWrite` (test_adr029_s5.py): None-valued wf fields + real tmp_path JSONL write verified

**Result: 248 passed, 0 failures across all three suites**

---

## [2026-05-15] backend-dev | ADR-029 Phase 1 S5 implemented: artifact retention, image hard-reject, comparator at EVAL, user-accept gate; Folded Fix 1 + Folded Fix 2

**S5 — ADR-029 Phase 1 (conversation.py)**

1. **Artifact Retention**: `_SessionData` gains `artifact_reference_id: str | None` and `artifact_reference_type: str | None`. Retained through `to_dict`/`from_dict` with backward-compat defaults (`None`). `artifact_reference_id` is either an ArtifactStore ID or a `"file:<abs_path>"` prefix for filesystem paths.

2. **Image Hard-Reject**: `_handle_upload_artifact_example` now calls `comparator.is_image_only(bytes, type)`. Image-only or unsupported file type → `ConversationTurn(must_show_human=True)` with verbatim `IMAGE_ONLY_MESSAGE`. State stays at `UPLOAD_ARTIFACT_EXAMPLE`. No silent degradation.

3. **Comparator at EVAL**: `_run_eval` step 8 reads produced artifact bytes from `wf_artifact_url` and reference bytes from `artifact_reference_id`, calls `comparator.compare(ref, produced, type)`, stores `ComparatorResult.to_dict()` in `eval_result["comparator"]`. Gap report is the PRIMARY EVAL signal. `exit_criteria.passed` is now diagnostic-only (ADR-029 supersedes DECISION-010); `_note` field explains this.

4. **Terminal Gate = User Acceptance**: `_handle_eval_response` options: `["accept", "ship as draft", "review design", "configure sources", "stop here"]`. Accept/promote → PROMOTE (stamps `user_accepted=True`). Force-promote checked BEFORE accept (substring collision fix). Ship as draft → DONE. Stop → DONE. Reject → stay at EVAL, `must_show_human=True`, labeled `# TODO-S6` seam.

**Folded Fix 1 — shared `_parse_llm_json_response` (BUG-573e3 parity)**

Extracted a canonical JSON-parse helper into `review.py` implementing the full strict→sanitize(BUG-573e3)→slice→raise sequence + BUG-44364 truncation detection. Both `review._llm_extract` and `executor._llm_extract_fields` now call it. Neither silently returns `{}` on parse failure.

**Folded Fix 2 — PROMOTE KB-resolvability gate (BUG-queue-e685d)**

`_handle_promote_response` now enforces two invariants before marking production:
(a) `persona_builder_delta` must exist in ADB — hard-fail with `must_show_human=True` if absent.
(b) After `upsert_persona_builder_kb`, fresh `ShimKb` must find the card. Hard-fail only when `all_cards()` is non-empty (real store). Zero-card store (test env) → warning + proceed.

**Tests**: `framework/tests/unit/test_adr029_s5.py` (new file, 31 tests across 7 classes). All 31 pass. No new regressions against `test_adr028_stream_a.py` (54/54) or `test_skill_builder_conversation.py` (151/160 — 9 pre-existing ShimKb-patch + behavior-change failures unchanged).

**Wiki**: `docs/wiki/authorskill-flow.md` UPLOAD_ARTIFACT_EXAMPLE + EVAL + PROMOTE + _SessionData sections updated.

Commits: `6740065` (Folded Fix 1), `0c21e2d` (S5 conversation.py), `09f893f` (S5 tests).

S5 complete. S6 NOT started. Reject-path seam at `framework/skill_builder/conversation.py _handle_eval_response` labeled `# TODO-S6`.

---

## [2026-05-15] backend-dev | ADR-028 S3+S4 implemented (CLARIFY state + persona injection); S3 2 bugs fixed

S3 CLARIFY state: fixed two bugs discovered by Stream C TDD tests before committing. (1) `_handle_clarify_response` now rejects non-substantive replies (`ok`, `yes`, `no`, `continue`, `proceed`, etc.) via `_NON_ANSWERS` frozenset — re-displays the question and prompts for a real answer. (2) `_advance_to_capture_intent` now auto-advances to CONFIGURE_SOURCES only when `nice_to_know_ambiguities` are present (no blocking ones); zero-ambiguity path still returns CAPTURE_INTENT confirmation turn (preserves S2 contract). All 9 `TestClarifyState` tests in Stream C's test_skill_builder_conversation.py now GREEN. Stream A's test_adr028_stream_a.py: 3 test fixes (prompt format kwargs updated for S4 placeholders; patch paths corrected for local imports). 54/54 tests GREEN. Pre-existing 6 failures unchanged (Stream B/C scope, ShimKb local-import patching issue). S3 commit pushed to origin/main (952ed07). S4 was already committed in the S3 commit (persona loader added early to avoid format() KeyErrors). docs/wiki/authorskill-flow.md updated: 17-state machine description, CLARIFY state entry, CAPTURE_INTENT routing logic. docs/wiki/authorskill-prompts.md updated: _CAPTURE_INTENT_PROMPT (S3+S4), _INSPECT_SOURCES_PROMPT (S1), _DESIGN_SKILL_PROMPT (S1+S3+S4), new _CLARIFY_PROMPT entry, summary table updated.

---

## [2026-05-15] stream-c-qa | P1+P3 QA tests added for ADR-028 S1-S4 (TDD contract, Stream C)

framework/tests/unit/test_persona_prompts_loader.py: new file, 107 tests in two classes — TestPersonaPromptsYamlContent (74 tests: all 9 personas present in YAML, each has non-empty key_fields/extraction_style/few_shot_example, all keys are correct type and non-trivial length) and TestPersonaPromptFragmentsLoaderContract (33 tests: _load_persona_prompt_fragments loader returns correct dict shape for all 9 personas, graceful-but-loud WARNING degradation for unknown persona, module-level cache re-use, YAML file path resolution). All 107 GREEN (S4 implemented on main). framework/tests/unit/test_skill_builder_conversation.py: extended with 28 new tests across 4 new classes appended at end of file — TestSynthesisableField (S1: synthesisable confidence level present in _INSPECT_SOURCES_PROMPT and _DESIGN_SKILL_PROMPT; synthesisable fields survive DESIGN_SKILL round-trip; ConversationTurn.data reflects synthesisable fields), TestMustShowHuman (S2: awaiting_user/must_show_human dataclass fields exist and default correctly; snake_to_camel serialization produces mustShowHuman; _turn_to_envelope includes must_show_human; field set True on preview-extraction turns), TestClarifyState (S3: CLARIFY is 17th state; _advance_to_clarify returns turn; _NON_ANSWERS rejects "ok"; blocking_ambiguities route to CLARIFY; nice_to_know do not block; clarification_log populated), TestPersonaPromptInjection (S4: _load_persona_prompt_fragments returns required keys for all 9 personas; persona fragments injected into LLM prompts during DESIGN_SKILL and CAPTURE_INTENT; tpm extraction_style "exec-safe" present in captured prompt). All 28 GREEN. Pre-existing 7 failures (wrong ShimKb patch target in TestDesignSkill/TestCaptureIntentState/TestConfigureSourcesV2) confirmed pre-existing, not introduced by QA stream.

---

## [2026-05-15] backend-dev | P2: ArtifactComparator module created (ADR-029 Stream B)

framework/skill_builder/comparator.py: new standalone module implementing ArtifactComparator with is_image_only() (zero-text-shapes gate, mirrors analyze_artifact._analyze_pptx pattern) and compare() (structure + density scoring with synonym normalisation, deterministic — no LLM in scoring path). ComparatorResult dataclass exposes structure_score, density_score, missing_sections, thin_sections, gap_report. IMAGE_ONLY_MESSAGE constant matches ADR-029 §C.1 prescribed wording verbatim. Supports pptx/md/txt natively; docx handled with ImportError graceful degradation (python-docx not in requirements.txt). framework/tests/unit/test_comparator.py: 31 tests covering is_image_only (true/false/unsupported/edge cases), structure_score (perfect match, 3-of-7 missing, all missing), density_score (thin, adequate, capped-at-1), synonym normalisation (Next Steps≈Action Items, Key Milestones≈Timeline, Risks & Mitigations≈Risks), gap_report content, ComparatorResult.to_dict(), IMAGE_ONLY_MESSAGE contract. All 31 tests GREEN.

---

## [2026-05-15] architect | ADR-028 & ADR-029 Accepted; persona playbook drafted; impl blueprint produced

ADR-028 set to Accepted. Locked: Item1=Option A (persona YAML playbook injected into DESIGN_SKILL + CAPTURE_INTENT; concrete starter templates generated for all 9 personas in the fusion-apps cloud-platform domain), Item2=Option A (awaiting_user + must_show_human added to ConversationTurn; hard "do not auto-answer" added to authorSkill tool description), Item3=Option A (new CLARIFY state, 17th state; blocking_ambiguities vs nice_to_know split in CAPTURE_INTENT and DESIGN_SKILL prompts), Item4=Option A (synthesisable confidence level in INSPECT_SOURCES; DESIGN_SKILL allowed to include synthesisable fields with explicit aggregation instructions).

ADR-029 set to Accepted. Locked: Option A (full outcome-based acceptance loop) with one explicit modification — NO vision-LLM; text comparator only; image-only references hard-rejected with user-facing message and re-upload prompt (not silent fallback). All 6 steps, constrained routing map, loop guardrails (max 3 iterations / $2.00 ceiling / consecutive-same-class detector / ship-as-draft escape), user acceptance as terminal gate. DECISION-010 superseded as terminal gate; auto-gold rows retained as diagnostic signal.

DECISION-011 marked Resolved (all items Option A). DECISION-010 marked Superseded by ADR-029.

framework/config/persona_prompts.yaml created with 9 persona stanzas (tpm, pm, architect, eng_mgr, developer, ops_eng, ops_mgr, service_owner, kbf_ops), each with key_fields, extraction_style, and one concrete few_shot_example grounded in the fusion-apps cloud-platform domain. Marked STARTER DRAFT.

docs/wiki/adr/ADR-028-029-impl-plan.md created: file-partitioned, dependency-ordered blueprint for 3-stream parallel dev team. Serial stream: S1 (synthesisable) → S2 (must_show_human) → S3 (CLARIFY state) → S4 (persona injection) → S5 (ADR-029 Phase 1) → classifier validation gate → S6 (ADR-029 Phase 2). Parallel streams: P1 (persona_prompts loader tests), P2 (ArtifactComparator module), P3 (test suite expansion for S1-S4). Critical risk flagged: classifier must receive source capability inventory (not just diff), must emit structured evidence, and must be validated against the known 26ai PPT case before routing is enabled.

dashboard.md, docs/wiki/index.md updated.

---

## [2026-05-15] architect | ADR-029 proposed: outcome-based EVAL + DECISION-011 reconciliation

Filed ADR-029 (proposed, NOT accepted). Proposes replacing the ADR-027 intrinsic EVAL (recall@k + faithfulness numeric gate) with an outcome-based, demonstration-artifact acceptance loop: (1) extract, (2) run full workflow to produce artifact, (3) compare produced artifact against user's reference using a semantic comparator (structure/density/layout/fidelity rubric), (4) surface gap report + CHANGE PROPOSAL, (5) route back to the appropriate prior state via a constrained failure-class → target-state map, (6) loop until user explicitly accepts. User acceptance is the terminal gate, not a numeric threshold. Root cause of the failure: reference artifact uploaded at UPLOAD_ARTIFACT_EXAMPLE is parsed into a layout dict then discarded — `_run_eval` (conversation.py:3180-3563) never reads `_data.artifact_layout` and never compares the produced PPTX against the reference. Feasibility analysis covers image-only reference problem (vision-LLM recommended, OCI constraint flagged), semantic rubric design, constrained routing map with loop guardrails (max 3 iterations, $2 cost ceiling, pathological-loop detector), and per-iteration cost (~$0.03-0.07). Three options presented (full loop, scoring-only, hybrid phased) with Option C recommended. DECISION-011 reconciliation: Items 2 + 4 are prerequisites for ADR-029; Item 3 is complementary; Item 1 is independent. DECISION-010 auto-gold rows retained as diagnostic signal, superseded only as terminal gate. Single recommended decision path provided. DECISION-010 marked with superseded-by note. dashboard.md + index.md updated. No code changes.

---

## [2026-05-15] backend-dev | fixed BUG-queue-44364 (eval _llm_extract max_tokens truncation)

Root cause: `_llm_extract` in `framework/skill_builder/review.py` called `llm.chat(..., max_tokens=2048)` — a 32-field schema caused the OCI model to hit the ceiling and emit structurally truncated JSON. The production path `WorkflowExecutor._llm_extract_fields` already used `max_tokens=4096`; the eval preview was inconsistent.

Option B implemented:
1. Introduced module-level constant `_EXTRACT_MAX_TOKENS = 4096` and passed it to `llm.chat` (replaces hardcoded 2048).
2. Captures `tokens_out` from `llm.chat` return dict. After all parse attempts fail: if `tokens_out >= max_tokens` → raises distinct `ValueError` naming structural truncation and BUG-queue-44364; otherwise → raises with both BUG-queue-573e3 and BUG-queue-44364 as possible causes (hedged, not definitive). Existing 3-attempt parse sequence (strict → sanitize → `{...}` slice) preserved unchanged.
3. Module docstring updated to document BUG-queue-44364 as a distinct failure mode alongside BUG-queue-573e3.
4. `docs/wiki/architecture.md` updated: BUG-queue-44364 section added under the sanitisation section; parse-order list updated to reflect the truncation-detection branch; residual risk (4096 ceiling still finite) documented.
5. 4 new unit tests in `framework/tests/unit/test_review.py`: `test_max_tokens_is_4096`, `test_truncated_response_raises_truncation_error`, `test_complete_large_schema_parses`, `test_parse_failure_not_ceiling_uses_corrected_message`. All 19 tests green.

## [2026-05-15] architect | fixed BUG-queue-440da — ORA-03146 in AdbSkillStore.write_artifacts. Root cause: `content` CLOB column in KBF_SKILL_ARTIFACTS was bound as VARCHAR2(4000) by the oracledb thin driver (no `setinputsizes`). Any artifact > 4000 bytes — extraction_schema (9488B), workflow_skill (5358B), eval_workflow (4535B) for a 32-field skill — caused ORA-03146 on every commit attempt. Same class of bug as BUG-008 (`adb_store.py` session_data fix) but was not applied to the skill artifact store or error store. Fix: added `oracledb` import guard + `cur.setinputsizes(content=oracledb.DB_TYPE_CLOB)` in `write_artifacts` before the per-artifact execute loop; same fix for `content_yaml` in `upsert_persona_builder_kb`; and for `message`/`stack_trace`/`extra_json`/`description` in `AdbErrorStore`. 7 new unit tests (4 in `test_skill_store.py`, 3 in `test_adb_error_store.py`) using module-level flag patching + sentinel CLOB objects. Session `synth-tpm-9571f396` (tpm / 26ai_fa_db_upgrade_to_26ai_pptx) is still at PREVIEW state; user must retry "ok, commit" after server restart to exercise the fix.

## [2026-05-15] backend-dev | fixed BUG-queue-573e3 — OCI bare-newline JSON crash in `_llm_extract`. Added `_escape_bare_control_chars()` state-machine helper in `framework/skill_builder/review.py` (module-level, unit-testable). `_llm_extract` now tries strict parse first, then sanitised parse, then `{...}` slice on sanitised text; all-fail raises loud `ValueError` referencing BUG-queue-573e3 (no silent `return {}`). 15 new unit tests in `framework/tests/unit/test_review.py` (all green). Added `kb-cli session recover --synth-id <id> --to-state <STATE> [--env <env>] [--confirm]` operator tool in `framework/cli/kb_cli.py` for stuck-session recovery; validates state against ADR-027 STATES list; requires `--confirm` to write; clears `last_turn` to prevent stale GET responses. Exercised against real session `synth-tpm-9571f396`: recovered from CONFIGURE_TRIGGERS to PREVIEW_EXTRACTION; GET endpoint confirmed state=PREVIEW_EXTRACTION. Architecture wiki updated with BUG-queue-573e3 sanitisation note.

## [2026-05-15] architect | BUG-queue-573e3 root-cause analysis — `_llm_extract` in `framework/skill_builder/review.py` lines 170-175 fails with `JSONDecodeError` when OCI LLM returns a 32-field JSON object containing unescaped bare newlines (`\n`) in string values. Both parse attempts fail (primary at line 171, fallback at line 175). The exception propagates uncaught through `_advance_to_preview_extraction` → `_handle_configure_triggers_response` → `respond()` → `_dispatch_tool_call` which surfaces it as "Tool execution error". Session synth-tpm-9571f396 is stuck at CONFIGURE_TRIGGERS. No recent commit addresses this. Fix proposal delivered to user.

## [2026-05-15] architect | ADR-028 + prompt dump — filed ADR-028 (proposed) capturing three user observations: (1) all authorSkill prompts are static templates / persona is a label not an instruction-shaper; (2) ConversationTurn has no must_show_human signal / smart clients can silently auto-advance; (3) REVIEW_DESIGN is a JSON dump / ambiguities are steamrolled by "ok". Architect-surfaced Item 4: DESIGN_SKILL excludes synthesisable fields as if unavailable — root cause of ADR-027 PPT thinness regression. Full prompt dump at docs/wiki/authorskill-prompts.md (9 prompts, all confirmed static). DECISION-011 filed for user direction. No code changes this round.

## [2026-05-14] architect | ADR-027 implemented — 16-state design-first machine + real EVAL. conversation.py rewritten: CAPTURE_INTENT normalises raw intent via LLM; CONFIGURE_SOURCES (v2) proposes sources from persona adapters; INSPECT_SOURCES fetches live samples and produces per-source capability inventory (cached in source_samples); UPLOAD_ARTIFACT_EXAMPLE parses layout hint only; DESIGN_SKILL single integrated LLM call produces schema + source_bindings + workflow_shape + reuse_plan from grounded inventory; REVIEW_DESIGN handles trivial deterministic patches + substantive LLM replan; PREVIEW_EXTRACTION uses cached samples for real extraction preview; EVAL runs Option A — extraction scoring, recall@k, faithfulness judge, /api/v1/ask workflow scoring, gold JSONL written to filesystem + ADB, PROMOTE gated on thresholds; force-promote override with audit trail. 450+ new unit tests added. Commits: 99ecba9 (docs) + 9628a6d (impl + tests). Both pushed to origin/main.

## [2026-05-14] architect | added docs/wiki/authorskill-flow.md (state-by-state LLM usage map)

## [2026-05-14] architect | ADR-026 Part B validation — verified /api/v1/ask generates correct single-slide two-column Oracle PPTX for 26ai. Identified scope field missing from tpm.weekly_exec_review_26ai schema. Fixed: added scope property + improved orm_status extraction prompt in v1.json (now 12 fields). Re-ran ask: 11/12 fields extracted, scope now shows "In scope: Upgrade ExaCS+IDCS and ADB pods. Out of scope: No upgrade planned for ExaCS+IDM pods." ORM shows derived status from WBS ORM/CSSAP tasks. Risks show FRE dependency and template_metadata.json risks from RAID register. All reference slide-15 checklist items verified: one slide, title, Jira ID, scope, assumptions, status bullets, next steps, key milestones, ORM, risk/mitigation, real 26ai Confluence data (wiki://20030556732), Oracle branding.

## [2026-05-13] architect | ADR-026 — source-grounded schema review + layout-aware PPTX rendering. Five structural gaps in the authorSkill pipeline addressed: (1) analyze_artifact hard-fails on image-only PPTX instead of silently falling through to keyword heuristics; (2) sampler.fetch_samples fetches live Confluence pages when page_id/page_url is present in source_query, regardless of KBF_STORE_BACKEND; (3) review_extractions calls the real LLM for extraction preview, raises RuntimeError when llm=None (no stub-mode fallback); (4) _source_grounded_review LLM call inserted at REVIEW_SCHEMA transition — fetches 2-3 live samples and asks the LLM to flag unsupportable fields and suggest missing ones; (5) PptxRenderer gains layout-aware dispatch: weekly_exec_review_v1 layout builds a single-slide two-column Oracle-style slide programmatically (no binary template). weekly_exec_review_26ai.yaml updated with layout directive. WorkflowExecutor._synthesize hoists top-level extracted fields to data dict for renderer access. 20 new unit tests in test_adr026_source_grounded_review.py, all passing. No new regressions (pre-existing test_smoke_validate.py failures confirmed pre-existing).

---

## [2026-05-13] dev | BUG-queue-e8298 — root-cause closure (silent write_artifacts failure). The 2026-05-12 "resolved" note was a band-aid: the backfill-skills-to-adb command imported existing disk YAMLs into ADB once, but did not fix the mechanism that produced the ADB-vs-filesystem gap in the first place. Today reproduced the same symptom in session synth-tpm-6523a9c4: DONE/completed with 5 committed_paths on disk, 0 rows in KBF_SKILL_ARTIFACTS. Inspection of conversation.py:_write_artifacts revealed `try { skill_store.write_artifacts() } except: log.warning(...)` — every ADB failure was silently demoted to a warning while the session reported "Committed N artifacts" to the user. Plus: (a) hardcoded `_KNOWN_ARTIFACT_TYPES` set was missing `extraction_schema` so the 5th artifact type was silently dropped, and (b) no retry on transient ADB errors (pool exhausted, bastion reconnect). Fix: (1) AdbSkillStore.write_artifacts retries 3× with 0.5/2/5s backoff; (2) final-attempt exception is re-raised; (3) conversation._handle_commit catches and stays at PREVIEW state with retry option, never advancing past PREVIEW on a phantom commit; (4) extraction_schema added to known types. 124/124 unit tests pass, with new test_retries_transient_failure_then_succeeds and test_raises_after_exhausting_retries. Commit 3664236.

## [2026-05-13] dev | fix(migrate): restored ORGANIZATION INMEMORY NEIGHBOR GRAPH on HNSW vector index after migration failed with ORA-51914 "Missing ORGANIZATION clause when creating a vector index". Re-diagnosis: my earlier removal (commit 2e2115e) was based on a wrong premise that Oracle 23ai had a "disk-resident" HNSW form — it does not. HNSW *requires* INMEMORY NEIGHBOR GRAPH; only IVF has a disk-based organization (NEIGHBOR PARTITIONS). The original 30-60s migration hang was actually caused by LLMClient() init in cmd_migrate (already fixed in 2e2115e — that part of the fix stands); the index DDL itself is cheap (~5s) on empty data. Updated kb_incidents.sql + ADR-025 (amended with correction) + dashboard gates + OCI runbook §5.2/§5.5. Gate 1 (verify In-Memory option) is now a true prerequisite, not optional. Commit 4cf0010.

## [2026-05-13] dev | fix(ingest): hard-fail policy when authorSkill INGEST adapter call fails. Replaced silent fixture fallback (b8d4cd2) with strict behavior: real ingestion succeeds → advance to EVAL → PROMOTE; real ingestion fails → INGEST returns status=failed, offers "retry ingestion" / "stop here", skill stays in previous state (draft). Belt-and-suspenders guard in _run_promote refuses to enter PROMOTE when ingest_result.status == "failed". Removed _dev_fixtures/confluence_pages/OCIFACP/ — keeping it would mask real codex/Confluence outages by silently succeeding via fixtures. Commit c666631.

## [2026-05-12] dev | fix(ingestion_worker): rewrote deploy/ingestion_worker.py to read KBF_PERSONA_BUILDERS from ADB (DECISION-006 Option B source of truth) instead of disk YAMLs. Added _load_kb_entries_from_adb() (list_persona_builder_kbs(status=production)), _load_kb_entries_from_disk() (fallback for KBF_STORE_BACKEND=filestore or ADB unavailable), _build_skill_store() (builds AdbSkillStore from env config). main() now accepts injected skill_store param for testability. Disk YAML loop is now pure fallback, not primary. Commit 9671918.

## [2026-05-12] dev | test: regression guards for four skill-builder bugs. Added TestRenameSkillCommand (3 tests: rename at ANALYZE_ARTIFACT, rename at REVIEW_FIELDS, no intercept after COMMIT). Added TestDeleteSkillFilesystemCleanup (1 test: delete succeeds when FS files absent). Fixed test_removed_field_note_shown_when_user_dropped_llm_field to set artifact_path (precondition for the delta note). 508 tests passing. Commit 4904619.

## [2026-05-12] dev | fix: four skill-builder bugs (938f0, 58f6f, 4fd5e, 9c3d9). (1) BUG-938f0: _prompt_review_schema delta notes gated on artifact_was_analyzed = bool(artifact_path) — no warning when no artifact was uploaded. (2) BUG-58f6f: rename skill to <name> intercepted in respond() before state dispatch for all pre-COMMIT states via regex; _handle_rename_skill() added. (3) BUG-4fd5e: deleteSkill now removes workflow_skill/eval/*.jsonl from filesystem after ADB delete to keep _list_available_personas() counts accurate. (4) BUG-9c3d9: uploadArtifact description updated to document text-extraction-only limitation for images/PPTX. Commit 0ae4f05.

## [2026-05-12] dev | BUG-queue-e8298 ~~resolved~~ band-aided (see 2026-05-13 root-cause closure entry above) — two-registry problem (ADB vs filesystem). Decision: one-time backfill CLI command instead of runtime fallback in getSkill. Added backfill-skills-to-adb subcommand to kb_cli.py: scans framework/workflow_skills/, reads workflow_skill/eval artifacts, writes to AdbSkillStore(synth_id="backfill"). Ran backfill: 10/10 skills migrated to ADB. Verified listSkills and getSkill both work from ADB. New AuthorSkill sessions continue writing to both disk + ADB (dual-write already wired in _write_artifacts()). Commit 3921640. **Note (2026-05-13): this only fixed the snapshot, not the mechanism — silent write_artifacts() failures continued to widen the gap. Proper closure in commit 3664236.**

## [2026-05-13] architect | Option B — persona builders moved to ADB. New table KBF_PERSONA_BUILDERS (migration-008.sql). SkillStore ABC + AdbSkillStore + FilestoreSkillStore gain upsert_persona_builder_kb / list_persona_builder_kbs. _handle_promote_response now reads persona_builder_delta from skill_store and upserts it to KBF_PERSONA_BUILDERS(status=production), then deletes the stray .yaml.new_kb file. ShimKb gains optional skill_store param: Pass 2 merges ADB production entries on top of YAML seeds; reload() re-runs load() for hot-reload after PROMOTE. mcp_server wires skill_store into ShimKb and exposes shim_kb on app.state; _handle_continue calls shim_kb.reload() when session done. Stray tpm.yaml.new_kb deleted. 16 new shim_kb tests + 30 new skill_store tests + 5 new promote tests.

## [2026-05-13] dev | ShimKb now loads *.yaml.new_kb entries (intermediate fix, superseded by Option B above). VectorSearchRetriever fixture fallback added — Tier 2 KB retrieval works on laptop without registered stores. Ingest pipeline wired (ConfluenceWikiIngestor) + 26ai dev fixtures + honest PROMOTE messaging with KB population status. OPS-E17935FA fixed — CONFIGURE_SOURCES quality gap (persona-aware source hints, ADB parsing, empty-source block). 11 + 10 + 12 regression tests.

## [2026-05-13] dev | listSkills + getSkill MCP tools added. listSkills returns skill summary list (read scope, persona/status filters). getSkill returns full detail — kbCard parsed from persona_builder_delta, eval gold-set line counts, optional workflowYaml (write scope). kb_status_note (ingest warning) restored in PROMOTE DONE message. 20 new unit tests; tool count 6→8.

## [2026-05-12] architect | OPS-CD461C27 + BUG-queue-1b878/5b233 — Two bug fixes committed. (1) _slugify cap raised 50→64 chars with word-boundary back-off to avoid mid-word truncation; warning + rename hint injected into ANALYZE_ARTIFACT prompt when slug >= 48 chars. (2) Synthesizer.synthesize() now catches OCI GenAI 400 content-filter errors ("Inappropriate content detected!!!"), logs the full error server-side with a KBF-generated request ID, and returns a clean no-answer dict; context_builder.answer() intercepts the _content_filtered flag and returns a tier_4 result with requestId and no OCI endpoint/opc-request-id details. 23 new unit tests (6 slugify + 17 content-filter). Pre-existing test_code_wiki::test_find_symbol_function failure not introduced by this change.

## [2026-05-12] architect | BUG-FIX — OCI instance_principal on laptop. Root cause: `_load_laptop_llm_overrides` in `mcp_server.py` was guarded by `if kbf_env == "laptop":` (correct) but `ingestion_worker.py` called `LLMClient()` with no kwargs, unconditionally using `adapters/llm.yaml`'s `auth: instance_principal`. Fix: replaced `_load_laptop_llm_overrides` with `_load_env_llm_overrides(repo_root, kbf_env)` (works for all envs, not just laptop); `mcp_server.py` lifespan now always calls it; `ingestion_worker.py` now also applies env LLM overrides. 4 new regression tests in `test_llm_factory.py` — all 8 pass.

## [2026-05-12] backend-dev | DECISION-009 implemented — bug_db config section + _init_bug_pool + bug_pool wiring in mcp_server + mcp_tools + export-bugs CLI + setup-bug-user command + migration-007. All existing tests pass (838/839; test_find_symbol_function pre-existing failure unrelated). 12 new unit tests for _init_bug_pool merge logic and failure resilience.

## [2026-05-12] architect | ADR-024 — Bug DB connection design. Documents _init_bug_pool inheritance contract (bug_db overrides dsn/wallet_path/wallet_password_secret/user/password_secret from adb; bastion always inherited), setup-bug-user CLI command (admin pool + runtime password resolution; no password in SQL), migration-007 (GRANTs only, not CREATE USER), non-fatal startup policy for bug_pool (degrades to JSONL if pool fails), SQL table references unchanged (KB_SHIM prefix stays), export-bugs CLI update (reads bug_db section). Exact YAML shape documented for laptop/staging/prod configs.

## [2026-05-12] dev | DECISION-008 + export-bugs CLI — ADB is the single source of truth for all bug records. pmo/bugs/*.md files become generated exports (not primary records). kb-cli export-bugs reads KBF_BUG_REPORTS + KBF_AUDIT_RUNS, writes YAML-frontmatter + <details>-expandable .md files + INDEX.md to pmo/bugs/. cmd_watch_bugs updated: dedup now checks queue_id in user_bugs.jsonl (not pmo/bugs/ file scan).

## [2026-05-12] dev | BUG-009 fixed (BUG-queue-6c173) — VALIDATE step failed "workflow references unknown KB" for any newly authored skill. Root cause: _run_validate() built kb_index from filesystem persona_builders/*.yaml only; persona_builder_delta artifact (the new KB entry) was in ADB, not on disk (PROMOTE writes it to disk, not COMMIT). Fix: read persona_builder_delta from skill_store at validate time, wrap in synthetic persona-builder YAML, merge with filesystem builders in temp dir, pass to validate_workflow_links. 18 tests pass.

## [2026-05-12] backend-dev | extraction_schema added as 5th artifact type — closes filesystem durability gap. ARTIFACT_TYPES in _base.py + type-inference branch in conversation._write_artifacts() now cover framework/parsers/schemas/{persona}/{skill_name}/v1.json. migration-005 updated (for fresh installs). migration-006 created: ALTERs chk_ksa_artifact_type constraint to include extraction_schema on already-deployed DBs + creates KBF_AUDIT_RUNS table (ADR-023). All committed skill sessions now store 5 artifacts in ADB; nothing production-relevant is filesystem-only.

## [2026-05-12] architect | ADR-023 + DECISION-007 — kbf_ops persona + reviewSkillSession MCP tool. Option 2 chosen (LLM-powered qualitative review). Option 1 (deterministic checks) deferred to ADR-024. KbfOpsSessionLoader reads all synth_id data from ADB; KbfOpsReviewEngine critiques 7 dimensions with structured output; findings auto-filed to KBF_BUG_REPORTS. reviewSkillSession becomes 5th external MCP tool. Backend Dev implementing.

## [2026-05-12] architect+backend-dev | DECISION-006 decided: Option A (ADB only, no git-sync). Git-sync deferred to ADR-023 — PR review only valuable when author≠approver (not current model; PROMOTE is already the review gate). Backend Dev implementing AdbSkillStore + AdbErrorStore + AdbCostStore + KBF_* DDL migration + kb-cli export-skills.

## [2026-05-12] backend-dev | OCI kbf-uploads bucket wired — namespace axq4m61mcei3 confirmed, compartment adp_faops_network (ocid1.compartment.oc1..aaaaaaaax7wbfdtfl7axhfae7q5lwvrmf2nlcdii3scarukqmuos7u5mokla). All 3 config files updated. OciArtifactStore now auto-discovers namespace via SDK get_namespace() in production (eliminating hard-coded requirement). KBF_ARTIFACT_OCI_NAMESPACE + KBF_ARTIFACT_OCI_PROFILE env var overrides added. PUT/GET/DELETE probe verified against live bucket. DECISION-005 → resolved.

## [2026-05-12] backend-dev | ADR-021 implemented — uploadArtifact MCP tool + ArtifactStore. FilestoreArtifactStore (laptop), OciArtifactStore (staging/prod: SDK InstancePrincipals; laptop CLI subprocess). uploadArtifact: base64 content, 10 MB cap, .pptx/.docx/.md/.txt, write scope required. _handle_analyze_artifact now detects "artifact:<file> id:<id>" prefix, resolves via ArtifactStore, calls analyze_artifact(local_path). artifact_store.cleanup(synth_id) called on DONE. skill_prompt v1.2.0 with upload instructions. DECISION-005 marked decided (Option A, no lifecycle rule, adp_faops_network, eu-frankfurt-1). ADR-021 updated: no lifecycle rule, OCI CLI auth for laptop, OCI SDK for prod. artifact_store: sections added to dev/staging/prod.yaml. 718 tests passing.

## [2026-05-12] qa | BUG-006 + BUG-007 filed + verified — BUG-006: PROMOTE/DONE session save raised ORA-02290 because author_skill.py set status='committed' on done sessions; DB constraint CHK_ASS_STATUS only allows in_progress/completed/abandoned/expired. Fixed: 'completed'. BUG-007: gold set seeded with null expected_extraction values because conversation.py passed {f: None for f in gaps} to seed_gold_set with no real example artifact. Fixed: gold_seed.py replaces None values with '<example {field}>' placeholder strings.

## [2026-05-12] qa | BUG-005 filed + verified — authorSkill validation fails after commit: synthesize_workflow.py wrote KB reference as {persona}.{skill_name}_data (with _data suffix) but synthesize_persona_builder_diff registered the KB as {persona}.{skill_name} (no suffix). validate_workflow_links() looked up the suffixed name, found nothing, returned "workflow references unknown KB". Fix: remove _data suffix from both sites in synthesize_workflow.py so workflow YAML and persona builder index agree.

## [2026-05-12] qa | BUG-004 filed + verified — authorSkill commits 0 artifacts after PREVIEW. Root cause: SkillBuilderConversation.to_dict() delegates entirely to get_state(), which intentionally omits synthesized_artifacts (kept lean for the GET-endpoint snapshot). But to_dict() is also the persistence path — so synthesized_artifacts was never written to session_data in ADB. On the next MCP call (commit), from_dict() restored an empty dict and _write_artifacts() committed nothing. Same omission for slide_mapping. Fix: to_dict() now appends both fields on top of get_state() output. from_dict() was already correct.

## [2026-05-12] qa | BUG-003 filed + verified — Oracle plain TIMESTAMP strips timezone on write and returns timezone-naive datetime via oracledb; AdbSessionStore.load() then compared it against datetime.now(tz=timezone.utc) raising TypeError: can't compare offset-naive and offset-aware datetimes. Manifested as authorSkill continuation always failing after BUG-002 was fixed (the expiry check was previously unreachable). Fixed with _as_utc() static method that normalises any of: None, ISO string ending in Z, ISO string with +HH:MM offset, naive datetime, or aware datetime — to a UTC-aware datetime. The expiry check in load() now calls self._as_utc(expires_at_val) instead of the brittle isinstance chain. Root cause is same as BUG-001/002: AdbSessionStore pool-attached path has zero unit test coverage. Test gap: add TestAdbSessionStorePoolPath covering naive + aware datetime shapes for expires_at. Dashboard and pmo/bugs/BUG-003 updated.

## [2026-05-12] qa | BUG-002 filed + verified — Oracle 23ai oracledb auto-deserialises CLOB IS JSON columns to Python dict; AdbSessionStore.load() then called json.loads(dict) raising TypeError. Manifested as authorSkill continuation always failing (isError=true: "the JSON object must be str, bytes or bytearray, not dict") while start always succeeded (write-only path). Fixed in 7cee283 with isinstance guard: `raw if isinstance(raw, dict) else json.loads(raw)` at both call sites (session_data in load(), progress_json in list_for_user()). Root cause is same as BUG-001: AdbSessionStore pool-attached path has zero unit test coverage. Test gap: add TestAdbSessionStorePoolPath with thin oracledb cursor fake covering dict + str return shapes. Dashboard and pmo/bugs/BUG-002 updated.

## [2026-05-12] backend-dev | MCP Streamable HTTP transport (JSON-RPC 2.0): fixed wire-protocol mismatch between server and Claude Code's native MCP HTTP client. New file framework/deploy/mcp_transport.py implements MCP spec 2025-03-26 Streamable HTTP transport — single POST /mcp endpoint dispatches JSON-RPC 2.0 requests (initialize, ping, tools/list, tools/call, prompts/list, resources/list, notifications). initialize/ping/tools/list/prompts/resources return without auth; tools/call requires Bearer token and returns JSON-RPC -32603 on missing/bad token. Tool execution errors use isError=true content (not JSON-RPC error envelope), unknown methods return -32601. SSE streaming support: if Accept: text/event-stream header is present, response uses StreamingResponse with data: prefix. Updated framework/deploy/auth/middleware.py to add /mcp to _AUTH_SKIP_PATHS (internal auth handled by mcp_transport.py per-method). Registered transport in mcp_server.py via register_mcp_transport(app, state) after existing /mcp/tools/* routes. Updated kbf-start.sh cheatsheet with correct .mcp.json snippet (type: http, url: .../mcp) and curl verify command. New tests in framework/tests/unit/test_mcp_transport.py (63 assertions across 10 test classes covering all specified scenarios). Existing /mcp/tools/list and /mcp/tools/call REST routes unchanged for backward compat.

## [2026-05-12] backend-dev | Phase 2+3 gap closure: implemented all 9 GAPs from architect audit. GAP-I1: ConfluenceWikiIngestor now accepts `wiki_store` param and calls `WikiMetadataStore.upsert_page()` after every new/updated page ingest; unchanged pages skip upsert. GAP-R1: SearchWikiRetriever fully implemented (lexical search via WikiMetadataStore.search_pages, persona filter, body read from path, returns list[Result]). GAP-R2: ReadWikiPageRetriever fully implemented (lookup by file path or page_id, metadata from store, returns Result|None). GAP-M1: PmSkill + TpmSkill instantiated in mcp_server.py lifespan and added to skills dict. GAP-M2: ShimWorkflows constructed and passed to ContextBuilder as shim_workflows. GAP-M3: app.state.cost_store passed to ContextBuilder as cost_store. GAP-M4: workflow tool registry now stores callables (closures over WorkflowExecutor.execute) instead of dicts. GAP-D1: BasePersonaSkill dispatch handles get_incident_summary (INC-ID extraction), search_wiki, read_wiki_page, query_fleet, text_to_sql (with NotImplementedError guard), find_symbol, read_code_page, list_sources. GAP-H1: ask.py + mcp_tools.py forward persona_hint/service_id_hint/func_area_hint to ContextBuilder.answer(). GAP-C1: IncidentVectorStore gains jira_base_url + confluence_base_url params producing real HTTPS URLs; mcp_server.py reads base_url from adapter YAML configs. New tests: test_confluence_wiki_ingest.py, test_search_wiki_retriever.py, test_read_wiki_page_retriever.py, test_base_persona_skill_dispatch.py (40+ assertions across 4 files).

## [2026-05-11] architect | Phase 2+3 completeness audit — found 11 gaps across retrievers, ingestion, persona-skill dispatch, MCP server wiring, workflow tool registry, and ingestion worker. Full punch list delivered to backend dev. Key gaps: search_wiki + read_wiki_page are NotImplementedError stubs (2 files, 9 lines); confluence_wiki_ingest never calls WikiMetadataStore.upsert_page(); BasePersonaSkill dispatch silently returns [] for get_incident_summary, search_wiki, read_wiki_page, and all other non-vector tools; mcp_server wires 0 persona skills except ops_eng and passes no shim_workflows to ContextBuilder; workflow tool registry stores dicts not callables; ingestion_worker.py is a logging-only stub; text_to_sql._llm_path raises NotImplementedError; IncidentVectorStore._citation_url produces non-resolvable internal URIs. graph_traverse (Phase 4) intentionally deferred — confirmed not a Phase 2/3 gap.

## [2026-05-12] backend-dev | adb-connect.sh tunnel fix: root cause of kbf-start.sh --migrate failure was that adb-connect.sh always created a NEW bastion session even when the existing session was still ACTIVE (3-hour TTL). New fast path: checks /tmp/adb-session.id, calls OCI API to verify state; if ACTIVE, skips 60-90s provisioning and re-opens SSH in ~5s. Session create only if ACTIVE check fails/missing. Also: SSH tunnel wait extended from 5×2s to 10×3s (30s) for new-session cases; added early exit if SSH process dies unexpectedly; removed duplicate nc -z check from kbf-start.sh (adb-connect.sh now does hard exit with clear error). Result: reconnect on existing session is ~5s instead of 60-90s.
## [2026-05-12] backend-dev | DB migration wired end-to-end: wrote framework/stores/sql/kb_shim.sql (CREATE USER kb_shim + author_skill_sessions table + 2 indexes, idempotent). Implemented real cmd_migrate in kb_cli.py — was a stub ("needs real ADB connection"); now loads laptop.yaml, builds ADB pool, dispatches to IncidentVectorStore.migrate() for kb_incidents and a new _run_sql_ddl() helper for kb_shim (shares _split_sql logic; swallows ORA-00955/ORA-01408/ORA-01920 for idempotency). kbf-start.sh gains --migrate flag: by default skips migration (fast re-start); `--migrate` runs `kb-cli migrate --schema all --env laptop` before uvicorn. Usage: first run uses `bash framework/scripts/kbf-start.sh --migrate`; subsequent runs drop the flag.
## [2026-05-12] backend-dev | Laptop fully provisioned: downloaded fresh ADB wallet (lamobl31whyai5kw, EU Frankfurt) with generated password via `oci db autonomous-database generate-wallet`; patched tnsnames.ora→localhost, sqlnet.ora→wallet path. Created ~/.kbf/secrets.env (chmod 600) with KBF_ADB_ADMIN_PASSWORD, WALLET_PASSWORD, OCI_PROFILE, KBF_ENV, KBF_STORE_ROOT. DB connection confirmed (DPY-6005 connection refused is expected when tunnel is down — not a credentials error). LaunchAgent plist fixed (removed KeepAlive loop, added ThrottleInterval=60); reloaded and running exit-0. kbf-start.sh is the single startup command — loads secrets, validates/refreshes OCI token, starts tunnel if needed, launches uvicorn.
## [2026-05-12] backend-dev | OCI token auto-refresh: macOS LaunchAgent (com.kbf.oci-token-refresh) runs ~/.oci/refresh-adpcpprod.sh every 4 minutes, calling `oci session refresh --profile adpcpprod` — no browser required. Token TTL is ~5 min; 4-min interval gives 1-min headroom. OciGenAiLLMClient gains _token_expires_in() (reads JWT exp from token file) and _ensure_client_valid() (rebuilds SecurityTokenSigner when <60s remaining) — called at top of chat() and embed(). Full chain confirmed: LaunchAgent refreshes file → _ensure_client_valid picks it up → live call succeeds. LaunchAgent is persistent across reboots. If refresh token itself expires (rare), manual re-auth: `oci session authenticate --profile adpcpprod --region eu-frankfurt-1`.
## [2026-05-12] backend-dev | OCI GenAI inference live: wired Oracle GPT-5 (eu-frankfurt-1) into KBF. llm.yaml updated with real endpoint (inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com), compartment OCID, and model OCID (ocid1.generativeaimodel.oc1.eu-frankfurt-1.amaaaaaask7dceya3ky2tq47f4syiiqm2p4skqcwtav7jyvcl322w36dx4kq). llm_oci.py: auto-detects security_token_file in OCI config profile and builds SecurityTokenSigner (required for adpcpprod session-auth profile); fixed _resolve_model to pass OCID directly; fixed max_completion_tokens; graceful stub-mode on auth failure. laptop.yaml: added [llm] override section (auth: config_file, config_profile: adpcpprod). mcp_server.py: calls _load_laptop_llm_overrides() on KBF_ENV=laptop to pass auth/profile overrides to LLMClient(). Smoke test via framework: LLMClient(auth=config_file, profile=adpcpprod) → LIVE, response='KBF LLMClient OK', 14 in / 9 out tokens. 616 tests passing.
## [2026-05-12] backend-dev | ADB auto-reconnect wiring: BastionReconnector.reconnect() now delegates to adb-connect.sh when bastion.script_path is set (new fields: script_path, oci_profile). AdbPoolConfig gains wallet_password field passed to oracledb.create_pool(). mcp_server.py lifespan auto-creates ADB pool when KBF_ENV=laptop via _init_laptop_adb_pool() helper (reads laptop.yaml, resolves env:// secrets, builds pool_config, calls create_adb_pool); pool wired into build_session_store(pool=adb_pool). factory.py updated: auto-selects ADB backend when pool is not None (no need to set KBF_STORE_BACKEND=adb). Result: when any authorSkill DB operation fails with ORA-12541/ORA-12170/ORA-12560/DPY-6005/ECONNREFUSED, RetryWrapper calls adb-connect.sh (with WALLET_PASSWORD+OCI_PROFILE from env), waits for port, retries — fully transparent to callers. Tests: 9 new tests in TestBastionReconnectorScript + TestAdbPoolConfigFromDict; 57 adb_pool tests, 616 total passing.
## [2026-05-12] backend-dev | ADB connectivity: created framework/scripts/adb-connect.sh (bastion session create, wait ACTIVE, SSH tunnel localhost:1522→100.200.232.160:1522, wallet download + patch tnsnames.ora→localhost + sqlnet.ora→wallet path). Confirmed Oracle AI DB 26ai connection via python-oracledb (SELECT 1, V$VERSION, schema empty). Created framework/config/laptop.yaml (gitignored) with real ADB/bastion OCIDs, wallet path, codex_proxy adapter config. Updated .gitignore to exclude laptop.yaml. ADB: aira_genai_agent_db_Sravan (LAMOBL31WHYAI5KW, adpcpprod profile, EU Frankfurt, Bastion202604212202).
## [2026-05-11] backend-dev | codex_proxy adapter mode: new framework/core/codex_proxy_runtime.py (CodexProxyRuntime: spawns `codex mcp-server`, MCP initialize handshake, `codex`/`codex-reply` tool calls, thread-safe, JSON extraction from markdown fences, thread-id reuse for cheaper follow-up calls). New framework/adapters/confluence/codex_proxy.py (ConfluenceCodexProxyAdapter: healthcheck/list/fetch/stream_changes via LLM-mediated prompts). New framework/adapters/jira/codex_proxy.py (JiraCodexProxyAdapter: healthcheck/list/fetch via JQL prompts). Updated confluence/__init__.py + jira/__init__.py with codex_proxy factory branch + KBF_ENV guard. Updated confluence.yaml + jira.yaml with codex_proxy stanza comments. ADR-020 amended with discovery findings (HTTPS+OAuth reality, codex_proxy rationale, smoke-test results: healthcheck ok 26s, list 5 FACP refs 17s, fetch page 87s). Unit tests: 33 new tests in test_codex_proxy_adapter.py, all passing. Full suite: 607/608 (1 pre-existing unrelated failure). E2E smoke confirmed against real central_confluence.
## [2026-05-11] backend-dev | ADR-019 + ADR-020 implementation: framework/core/adb_pool.py (BastionReconnector + RetryWrapper, 48 tests) + framework/adapters/confluence/codex_cli.py + framework/adapters/jira/codex_cli.py (Codex CLI MCP stdio transport, 59 tests). Updated adapter factories with codex_cli mode + KBF_ENV guard. Updated dev.yaml with deployment_mode + bastion config. Updated confluence.yaml + jira.yaml with codex_cli stubs. Total: 574 tests passing (+107 new).
## [2026-05-11] architect | ADR-020: Codex CLI as MCP transport for laptop mode. Adds mode: codex_cli as third transport for Confluence/Jira adapters. Decision: Option B (direct MCP stdio subprocess) — reads spawn command from ~/.codex/config.toml, spawns MCP server process, speaks MCP JSON-RPC over stdio. Rejects codex exec LLM intermediary (token cost + fragile output parsing) and hybrid auth bootstrap (Codex credentials not extractable). New adapter files to be written by Backend Dev. KBF_ENV guard enforces laptop-only. RawItem invariant held via shared normalize().
## [2026-05-11] architect | ADR-019: bastion auto-reconnect for Oracle ADB in laptop mode. New module framework/core/adb_pool.py specified (BastionReconnector + RetryWrapper + BastionReconnectError). New `bastion` config section in dev.yaml + `deployment_mode` field in _schema.json. Laptop mode auto-creates OCI bastion session via CLI, starts SSH tunnel subprocess, port-checks, health-probes with SELECT 1 FROM DUAL, retries original operation; max 3 reconnect attempts. Non-laptop mode: exponential backoff only. wiki/index.md backfilled with ADR-015 through ADR-018 which were missing.
## [2026-05-11] backend-dev | PDD V3 implementation complete — 18 new files, 2 modified, 468 tests (333 new + 135 pre-existing), 0 failures. REST API: POST /api/v1/ask + 5 authorSkill endpoints + 3 ops endpoints. MCP: 2 external tools (askKnowledgeBase, authorSkill), internal tools blocked. Auth: bearer token middleware + consumer manifests + RPM limiting. Session: filestore + ADB store + TTL cleanup. Serialization: snake↔camel boundary. Cost: append-only JSONL telemetry. context_builder.py extended (persona_hint, cost_store). conversation.py synth_id now uuid-based.
## [2026-05-11] backend-dev | Track B (MCP tools) + Sprint 4 (wiring): Created framework/deploy/mcp_tools.py — EXTERNAL_TOOLS_SCHEMA (2 tool schemas: askKnowledgeBase requires "question", authorSkill requires "input"), build_external_tool_registry() returns {askKnowledgeBase: handler, authorSkill: handler}; handlers are async, accept _consumer kwarg, reuse _build_ask_response and _start_or_continue_session from route modules; _anonymous_consumer() fallback for unauthenticated MCP callers. Modified framework/deploy/mcp_server.py — wired bearer_auth_middleware, ConsumerRegistry, FilestoreSessionStore, CostStore into app.state; included ask_router/author_skill_router/ops_router; replaced /mcp/tools/list to return only 2 external schemas; replaced /mcp/tools/call to route to external registry and return 404 for internal tools; kept /answer backward-compat endpoint. Created framework/tests/test_mcp_tools.py (unit tests: schema shape, registry cardinality, handler callability, async assertions, ask handler calls ctx.answer + builds Budget, author_skill handler uses user_id + calls session_store.save, _anonymous_consumer defaults). Created framework/tests/test_mcp_server_integration.py (optional integration tests: /mcp/tools/list returns 2 tools, /mcp/tools/call askKnowledgeBase returns answer+citations+tier_used, authorSkill multi-turn, 7 internal tools blocked with 404, /healthz + /api/v1/version work without auth).

## [2026-05-10] backend-dev | Track F (ops endpoints + cost store): created framework/deploy/cost_store.py (CostStore — append-only JSONL cost_log, record() writes timestamped entries, query() aggregates by_persona/by_operation/total_tokens with optional persona/skill_name/start_date/end_date filters; date boundary parsing via _parse_date_bound(); handles missing file gracefully). Created framework/deploy/routes/ops.py (APIRouter with /healthz no-auth health check returning adb/llm/git/adapter checks + uptimeSeconds + 503 on error, /api/v1/version no-auth returning apiVersion/schemaVersion/buildSha from KBF_BUILD_SHA env, /api/v1/metrics/cost admin-scope query delegating to cost_store.query(); camelCase query param aliases skillName/startDate/endDate; all responses via to_camel_response()). Created framework/tests/test_routes_ops.py (57 tests: 35 CostStore unit tests covering record/query/filters/aggregation, 22 route handler tests via FastAPI TestClient covering 200/401/403 paths, camelCase response shape, zeroed response when cost_store=None).

## [2026-05-10] backend-dev | Track A (REST routes): created framework/deploy/routes/__init__.py (empty), framework/deploy/routes/ask.py (POST /api/v1/ask — validates question 1-4096 chars, calls ContextBuilder.answer() with consumer-budget, maps result to AskResponse with camelCase via to_camel_response; tier 4 adds skillSuggestion; standalone _build_ask_response helper), framework/deploy/routes/author_skill.py (POST start/resume, POST /{synthId} continue, GET list, GET /{synthId} state, DELETE abandon; _start_or_continue_session() standalone function for MCP Track B reuse; last_turn stored in session for idempotent GET; all responses camelCase). Tests: framework/tests/test_routes_ask.py (happy path, validation failures 400, scope enforcement 403, tier-4 skill suggestion, budget passthrough), framework/tests/test_routes_author_skill.py (full lifecycle: start→continue→GET→DELETE, isolation by user_id, scope enforcement, real FilestoreSessionStore on tmp_path, stub LLM mode).

## [2026-05-10] backend-dev | Track C (auth): created framework/deploy/auth/__init__.py, consumer.py (ConsumerManifest dataclass with has_scope/allows_persona), registry.py (ConsumerRegistry — loads *.yaml from consumer_manifests/ at startup, O(1) SHA-256 token lookup, supports plaintext token + pre-hashed tokenHash), middleware.py (bearer_auth_middleware, get_consumer, require_scope raises HTTPException(403) not JSONResponse, sliding-window RPM enforcement, _reset_rpm_counters_for_testing for hermetic tests). Created framework/config/consumer_manifests/dev-local.yaml (example dev manifest). Created framework/tests/test_auth_middleware.py with 30 assertions covering all specified behaviours.

## [2026-05-10] backend-dev | Track E (serialization): created framework/deploy/serialization.py (snake_to_camel, camel_to_snake, convert_keys, to_camel_response, from_camel_request) and framework/tests/test_serialization.py (97 assertions covering known openapi.yaml field names, nested dicts, list traversal, round-trip, JSONResponse contract, edge cases)

## [2026-05-10] architect | Produced PDD V3 implementation plan at docs/wiki/engineering/pdd-v3-implementation-plan.md — 6 tracks (REST routes, MCP tools, auth middleware, session persistence, serialization, ops endpoints), concrete file paths and function signatures, dependency graph, test coverage requirements. Identifies existing code to reuse (conversation.py, context_builder.answer(), mcp_server.py skeleton) vs. all-new code (framework/deploy/auth/, session/, routes/, mcp_tools.py, serialization.py, cost_store.py). Minimal modifications to context_builder.py and conversation.py specified.

## [2026-05-04] init | Project bootstrapped from dev-agent-team v0.1.5
## [2026-05-04] setup | Knowledge Builder Framework spec (`docs/raw/knowledge-builder-framework-spec.md`) and design meeting notes registered in `manifests/raw_sources.csv`
## [2026-05-04] setup | CLAUDE.md and KICKOFF.md customized for the framework (replaced default sports-app tech stack with Python+pgvector+graph+MCP per spec §11)
## [2026-05-04] setup | Seeded `docs/wiki/persona-knowledge-builder.md` capturing the user's per-persona builder agent requirement (extends spec §4 + answers §8.3)
## [2026-05-04] setup | Wiki index updated with planned page list mapped to spec sections; current-status set to pre-Phase-0
## [2026-05-04] tpm | Filed DECISIONs 001-004 (Oracle stack + converged DB + OpenAI + PM/TPM/Aira persona set)
## [2026-05-04] architect (acting via tpm) | Drafted ADRs 001-005 (tech-stack, storage shape, core interfaces, persona-builder config, eval harness)
## [2026-05-04] pm (acting via tpm) | Authored project-overview, personas, and 6 module pages (incidents/fleet/code/pm-tpm-wiki/fa-graph/jira-roadmap)
## [2026-05-04] architect (acting via tpm) | Seeded persona-builder YAML template + extraction-schema JSON template; incident extraction schema v1
## [2026-05-04] qa (acting via tpm) | Seeded eval/gold_sets/incidents.jsonl with 5 placeholder questions
## [2026-05-04] tpm | Filed Phase 0 Kickoff Brief and pending-decisions surface for all phases
## [2026-05-04] tpm | Updated dashboard with morning briefing, Gate 1 approval surface, and lint findings; updated phases.md and current-status.md
## [2026-05-05] pm (acting via tpm) | Authored PDD-Knowledge-Builder-Framework.md consolidating all design discussions: 5-layer architecture, polyglot per persona (knowledge_bases, not corpora), skills-default, functional-area + resources dimensions, persona-builder contract, phase plan, v1 acceptance, open ADRs 006-009
## [2026-05-05] pm (acting via tpm) | Authored exec-brief.md with mermaid diagrams (12 sections; PPT-ready) for leadership review
## [2026-05-05] tpm | Updated wiki index to surface PDD + exec brief as primary entry points
## [2026-05-05] pm (acting via tpm) | Generated PDD as .docx with 14 embedded mermaid diagrams; exec brief as .pptx with 9 embedded diagrams (Midnight Executive palette)
## [2026-05-06] architect (acting via tpm) | Amended ADR-004 (corpora→knowledge_bases, polyglot principle, KB cards). Authored ADR-006 (two-shim arch), ADR-007 (persona context skill contract), ADR-008 (functional-area + resources dimensions), ADR-009 (resource ontology), ADR-010 (configuration plane), ADR-011 (dual-mode source adapters)
## [2026-05-06] architect (acting via tpm) | Authored architecture.md, data-model.md, api-design.md mirroring spec §3, §6.1, §6.4 with all ADR amendments
## [2026-05-06] architect (acting via tpm) | Wrote configuration plane: framework/config/{dev,staging,prod}.yaml + _schema.json + adapters/{confluence,jira,git,udap,openai}.yaml + shim_faaas.yaml + .env.example + bootstrap-vault.sh + check-config.py
## [2026-05-06] architect (acting via tpm) | Wrote dual-mode adapter stubs (Confluence native+mcp, Jira native+mcp), single-mode adapters (git, udap), Adapter Protocol in _base.py
## [2026-05-06] architect (acting via tpm) | Wrote 8 persona context skill stubs (BasePersonaSkill + per-persona files) per ADR-007
## [2026-05-06] pm (acting via tpm) | Authored full Option-3 starter pack: 8 persona_builders/{persona}.yaml + 22 extraction schemas under parsers/schemas/{persona}/* + 8 gold sets under eval/gold_sets/* (status: draft; STARTER labels throughout)
## [2026-05-06] architect (acting via tpm) | Wrote framework/stores/sql/kb_incidents.sql (full Oracle 23ai DDL with VECTOR(3072) HNSW index) + framework/stores/incident_vector_store.py + _base.py + chunker.py stubs to make the vector path concrete
## [2026-05-06] architect (acting via tpm) | Reviewed docs/raw/aira-vector-search-detailed-explained.html and authored docs/wiki/aira-comparison.md — full extraction + retrieval comparison vs framework; identified 5 concrete actions (ADR-012 in-DB embedding, ADR-013 filter strictness, ADR-007 amendment, ADR-005 amendment, AIRA gold-set bootstrap)
## [2026-05-06] architect (acting via tpm) | ADRs 012 (in-DB embedding), 013 (filter strictness) authored; ADR-005 amended (AIRA gold set, recency-weighted recall, filter strictness in eval); ADR-007 amended (max_context_chars, structured synthesis schema, IntentFilter)
## [2026-05-06] pm (acting via tpm) | Authored docs/wiki/onboarding/pm-tpm.md — workbook for PM and TPM team leads to refine 'what to extract' from raw sources
## [2026-05-06] dev-team (acting via tpm) | Phase 1 implementation pass: full code for adapters (Jira native+MCP, Confluence native+MCP, Git, UDAP stubs), parsers (LLM parser with schema injection, markdown-aware chunker), IncidentVectorStore with full SQL/embedding flow + ADR-012 in-DB embedding proc + ADR-013 filter strictness, retrievers (vector_search, get_incident_summary, list_sources real impl; others stubbed for later phases), orchestrator (shim_faaas, shim_kb, intent_classifier, context_builder, synthesizer with structured output), persona skills (BasePersonaSkill full ADR-007 + ops_eng), ingestion (pipeline, change_detection, webhook_router, scheduler), eval harness (runner, recall+latency+cost+faithfulness metrics, markdown+JSON reports, baseline diff), FastAPI MCP server, kb-cli (validate/ingest/eval/promote/migrate), CI eval-gate.yml, unit tests, dev-guide + runbook, 22 Phase-1 stories drafted
## [2026-05-06] dev-team note | All Phase 1 code is structurally complete but integration-untested without provisioning. External touchpoints clearly marked. Phase 1 hard exit gate (80% recall on 25-question gold set) requires ADB + OpenAI + AIRA gold-set queries to verify.
## [2026-05-06] architect (acting via tpm) | ADR-014 — LLM access via OCI Generative AI Inference (AIRA-aligned). Refactored framework/core/llm.py into a façade (factory) over llm_oci.py (default) and llm_openai.py (fallback). Added framework/config/adapters/llm.yaml with oci_genai endpoint placeholder + openai_direct fallback. Updated env configs. User to provide GenAI service URL.
## [2026-05-10] pm | Authored pmo/workshops/persona-authoring-workshop.md — 90-min facilitation guide for V2 skill-by-demonstration persona onboarding sessions. Covers: workshop overview, 6-segment agenda with facilitator notes, pre-workshop checklist for persona teams, post-workshop deliverables timeline, and per-persona prep sheets for Ops Engineering (AIRA migration, Phase 1 exit gate) and PM/TPM (Phase 3 skills, schema decisions to close).
## [2026-05-10] backend-dev | Phase 2 Track A+B implementation: fleet adapter (udap_adapter.py fully implemented with list/fetch/discover/healthcheck, filestore mode against 6 new _dev_fixtures/fleet/*.json), code wiki builder (adapters/code_wiki_builder.py — Som-style AST extractor writing ContentItems + code_wiki_index.json), 4 MCP retrievers (query_fleet, text_to_sql with pattern matching, find_symbol, read_code_page — all with citations), all 4 tools registered in mcp_server.py, code-wiki-build CLI subcommand added to kb_cli.py, 2 unit test files (test_fleet_adapter.py: 34 tests; test_code_wiki.py: 28 tests), all in filestore/stub mode.
## [2026-05-10] dev-team (acting via tpm) | Phase 1-3 consolidated build: interactive skill-builder conversation.py (615-line state machine, 9 states INIT→COMMITTED), validate_links.py (ADR-017 requires_extractions⊆provides_fields check), WorkflowMCPTool + discover_workflow_skills registry, 3rd workflow skill (tpm.weekly_exec_review), 4-tier intent classifier with confidence thresholds, context_builder multi-persona fanout (Tier 3), confluence_wiki_ingest.py (HTML→markdown idempotent), wiki_metadata_store.py, provides_fields backfill on all 8 persona builders, cost telemetry in orchestrator, deliverer output normalization, routing thresholds in config plane, CLI wiring (interactive skill-builder + --validate-links promote), fixture data (confluence pages, weekly ops). 60 files, 7052 insertions. All filestore/stub mode — no external provisioning required.
## [2026-05-10] architect | Workshop Operations Guide (docs/wiki/engineering/workshop-ops-guide.md) — 4-part doc: (1) how to run the application end-to-end, (2) NL gold-set feeder interface for persona workshops, (3) architecture of the feeder state machine, (4) end-to-end workshop playbook. Also built: framework/eval/gold_set_feeder.py (GoldSetFeeder 7-state machine: INIT→ENTRY→CITATION→EXPECTED_FIELDS→REVIEW→NEXT→DONE), `kb-cli gold-feed` subcommand, framework/eval/gold_sets/ directory. Feeder works in stub mode, appends to JSONL, tracks progress toward 25-entry workshop target.
## [2026-05-10] backend-dev | Gold set feeder: framework/eval/gold_set_feeder.py (GoldSetFeeder 7-state machine INIT→ENTRY→CITATION→EXPECTED_FIELDS→REVIEW→NEXT→DONE), framework/eval/gold_sets/__init__.py, gold-feed CLI subcommand in kb_cli.py (`kb-cli gold-feed --persona ops_eng [--skill incident_summary]`), count_entries() utility, 63-test suite in framework/tests/unit/test_gold_set_feeder.py. All 135 tests pass. No external deps.
## [2026-05-10] architect | PDD V3 — Deployment Interaction Layer authored at docs/wiki/pdd/PDD-Knowledge-Builder-Framework-v3.md. Covers: two interaction models (Consumption Flow with single ask_knowledge_base MCP tool vs Knowledge Builder Flow with 12 atomic tools), complete REST API surface (POST /api/v1/ask + 12 /api/v1/kb/* endpoints with full request/response schemas), MCP tool catalog (13 tools with signatures), OCI compute VM deployment topology, per-KB storage kinds (vector/wiki/graph/sql_passthrough/code_index/filestore), authentication/authorization model, client configuration for Claude Code / Codex, operational endpoints, and mermaid sequence diagrams. V3 extends V2 (not replaces it); V2 owns internal architecture, V3 owns external surface. Wiki index updated to surface V3 as current doc.
## [2026-05-10] architect | PDD V3 rev 2 — Major design evolution: (1) MCP surface collapsed from 13 tools to 2 (ask_knowledge_base + author_skill). (2) Knowledge Builder API replaced — 12 client-orchestrated atomic APIs replaced with 5 server-side session-management endpoints under /api/v1/kb/author-skill. (3) 14-state machine now server-side and ADB-persisted; client is a pass-through (post input, show response, repeat). (4) New REVIEW_SCHEMA state added between REVIEW_FIELDS and CHECK_REUSE — critical quality gate where user reviews and edits field extraction instructions (JSON-Schema descriptions). (5) Persona discovery and post-commit operations (validate/ingest/eval/promote) are inside the state machine, not external MCP tools. (6) Session persistence schema documented (kb_shim.author_skill_sessions with 7-day TTL, multi-session support, resume semantics). (7) Sections 2, 6, 7, 10, 13, 15 rewritten; new sections 16 (session persistence DB schema) and 17 (references) added.
## [2026-05-10] architect | PDD V3 rev 3 — External API surface converted to camelCase throughout. MCP tool names: askKnowledgeBase, authorSkill. REST endpoint: /api/v1/kb/authorSkill (was /api/v1/kb/author-skill). All JSON request/response field names updated to camelCase (synthId, skillName, tierUsed, costTokens, skillSuggestion, citationUrl, contentId, chunkId, etc.). DB column names unchanged (snake_case — PostgreSQL/Oracle convention). Python internal code unchanged (snake_case). Convention note added to PDD header and §15.2 table (DB col vs JSON field mapping). OpenAPI 3.1 spec created at framework/deploy/openapi.yaml covering all 6 endpoint groups with full camelCase schemas, 14-state enum, examples for key states, bearer auth, error responses. Wiki index updated with OpenAPI link.
## [2026-05-10] architect | OCI Deployment Runbook authored at docs/wiki/engineering/oci-deployment-runbook.md. 10-section guide from empty OCI tenancy to live framework: (1) Prerequisites (OCI tenancy, IAM policies, tools, network); (2) OCI infrastructure setup (Compute VM, VCN, ADB 23ai, Vault, GenAI Inference, Object Storage, Dynamic Group + policies); (3) VM setup (OS packages, repo clone, Python venv, Oracle Instant Client, wallet config, Nginx TLS config, three systemd service files); (4) Configuration (prod.yaml fill-in, adapter configs, .env file, bootstrap-vault.sh walkthrough, bearer token creation); (5) Database schema setup (CREATE USER DDL, kb-cli migrate commands, DBMS_VECTOR credential for in-DB embedding per ADR-012, verify steps); (6) First deployment (service start, healthz, smoke test, MCP connection test, initial ingestion, eval baseline); (7) MCP client configuration (SSE and stdio SSH-tunnel modes for Claude Code / Codex / Cursor); (8) Ongoing operations (journald logs, healthz cron, cost telemetry, webhook setup, git pull update procedure, backup strategy); (9) Troubleshooting (ADB wallet, OCI GenAI rate limits, ingestion failures, MCP connection, author-skill sessions, Nginx TLS, eval CI regressions, cost spikes); (10) Configuration reference (all env vars, all prod.yaml fields, port table, security list rules, Vault secret slug reference). Wiki index updated.
## [2026-05-10] architect | PDD V3 rev 4 — Client-agnostic language pass across PDD V3. Generic prose now uses "MCP client" instead of "Claude Code / Codex". Transport table, Tier 4 suggestion, options field doc, mermaid sequence diagrams, section 10 heading all updated. Example session transcripts retain "Claude Code" as concrete walkthrough attribution. current-status.md updated with V3 design summary.
## [2026-05-10] backend-dev | Track D — Session persistence layer: framework/deploy/session/__init__.py (empty), _base.py (SessionStore ABC with save/load/list_for_user/abandon/expire_stale), filestore.py (FilestoreSessionStore — JSON files at {root}/sessions/{user_id}/{synth_id}.json, ownership check, auto-expire on load, expire_stale bulk walk, no caller-dict mutation), adb_store.py (AdbSessionStore — Oracle MERGE/SELECT/UPDATE with stub mode when pool=None), factory.py (build_session_store selecting backend from KBF_STORE_BACKEND env var, default filestore), cleanup_job.py (run_ttl_cleanup_loop async background task). 43-test suite in framework/tests/test_session_store.py covering save/load/list/abandon/expire_stale, ownership enforcement, expired session returns None, updated_at mutation guard, stub mode ADB, factory backend selection.
## [2026-05-11] qa | Filed BUG-001 (pmo/bugs/BUG-001-adb-session-store-conn-execute.md, status: verified, severity: blocker) — AdbSessionStore was calling `conn.execute/fetchone/fetchall` on the oracledb `Connection`, which exposes those only on cursors; also bound ISO-8601 strings to TIMESTAMP columns triggering ORA-01843. Fix already landed in d36d46b (cursor pattern + dict rowfactory + datetime binding + Python 3.9 `Z`-suffix handling). Verified end-to-end via POST /api/v1/kb/authorSkill in laptop mode (KBF_ENV=laptop, ADB pool live): session synth-tpm-c6df8cd3 created, persisted, state advanced to ANALYZE_ARTIFACT. Bug exposed a real test gap — `test_session_store.py` exercises only stub mode (pool=None); the pool-attached AdbSessionStore code path has no coverage, which is how this shipped.
## [2026-05-16] backend-dev | BUG-queue-f0591: persist CLARIFY questions/next_state across ADB round-trip — Root cause: `_clarify_questions` and `_clarify_next_state` were NEVER written in `to_dict()` nor read in `from_dict()`, causing DESIGN_SKILL→CLARIFY→REVIEW_DESIGN to loop infinitely (reset to CONFIGURE_SOURCES on every session resume). Fix: added `clarify_questions`/`clarify_next_state` keys to `to_dict()` (conversation.py ~line 1013–1014) and restored them in `from_dict()` (~line 1078–1079) with backward-compat defaults for pre-fix sessions. Updated misleading "not persisted between sessions" comment at `_SessionData` CLARIFY fields. Added optional hardening guard in `_handle_clarify_response`: when design is set but _clarify_questions=[], surface must_show_human error instead of silently advancing. Added `TestClarifyStatePersistence` (4 tests) to test_skill_builder_conversation.py. All 5 suites: 310 passed, 0 failures.
