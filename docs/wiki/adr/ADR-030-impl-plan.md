---
title: "ADR-030 — Implementation Blueprint: Prompt Externalization + Harness"
status: active
created: 2026-05-16
owner: architect
deciders: dev-manager, dev-team
tags: [impl-plan, adr-030, skill-builder, prompts, tooling]
related: [ADR-030-prompt-externalization-and-harness.md, ADR-028-029-impl-plan.md]
---

# ADR-030 — Implementation Blueprint

This document is the executable work breakdown for the ADR-030 deliverable. It is
file-partitioned, dependency-ordered, and serial-aware. A dev agent receiving this
document plus the accepted ADR-030 should be able to execute their assigned tasks
without further architect input.

Raise a DECISION file for anything this blueprint does not answer. Do NOT guess.

---

## 0. Serialization constraint — same rule as ADR-028/029

`conversation.py` contains 8 prompts and is the serial-stream file. Every change to
it is ONE agent, ONE serial stream, in dependency order.

Everything else — the loader module, YAML files, the harness CLI, fixtures, tests, the
generated-docs tool — is NEW files or files other than `conversation.py`. These are
fully parallel-safe and can be built by separate agents before the serial stream starts.

The serial stream (cutover) starts ONLY after the parallel side-streams are complete
and the loader + YAML files pass their tests. This is the key dependency gate.

```
PARALLEL SIDE-STREAMS (no collision — new files only)
  P1: PromptRegistry loader + unit tests
  P2: YAML prompt files (verbatim copies of constants)
  P3: Prompt-test harness CLI + fixtures
  P4: authorskill-prompts.md generator + CI check

  (All P-streams depend on nothing from each other — fully parallel)
  (P1 and P2 must both be done before the SERIAL stream starts)

SERIAL STREAM (one agent, in order — touches existing .py files)
  C1: conversation.py cutover (8 prompts)
  C2: synthesize_schema.py cutover (1 prompt)
  C3: review.py cutover (1 prompt)
  C4: executor.py cutover (1 inline prompt → executor_extract)

GATE TASK (after C1–C4)
  G1: classifier gate + post-migration checksum verification
```

---

## 1. Task Inventory

### P1 — PromptRegistry Loader Module + Unit Tests

**Files touched (NEW):**
- `framework/skill_builder/prompt_registry.py` (new)
- `framework/tests/unit/test_prompt_registry.py` (new)

**Change description (4 sentences):**
Create `PromptRegistry` class that reads all `*.yaml` files from `framework/config/prompts/`, caches them in memory, and hot-reloads when any file's mtime changes. Implement `get_prompt(prompt_id, *, persona=None, **fmt_vars) -> PromptSpec` with hard-fail on unknown prompt_id, missing required vars, and checksum mismatch on locked prompts. Implement `reload()` and `list_prompts()`. Export a module-level `get_registry(prompts_dir=None) -> PromptRegistry` singleton factory.

**Detailed contract (from ADR-030 §Design §4):**
- `PromptSpec` dataclass: `prompt_id`, `version`, `model`, `max_tokens`, `response_format`, `text`
- `PromptMeta` dataclass: `prompt_id`, `version`, `description`, `locked`, `model`
- Exceptions: `PromptStoreError`, `PromptNotFoundError(PromptStoreError)`, `MissingVarsError(PromptStoreError)`, `LockedPromptTamperedError(PromptStoreError)`
- Checksum algorithm: `sha256(template.encode("utf-8").rstrip(b"\n"))` → hex string prefixed `sha256:`
- Hot-reload: `os.stat()` on each YAML file on every `get_prompt()` call; if any mtime changed, call `reload()` atomically before serving
- Malformed YAML at load or reload: `PromptStoreError` raised immediately; no partial load; registry stays on last-good state on reload (implement load-then-swap pattern: load into a temporary dict, validate fully, then swap atomically)
- Persona overlay resolution: load `persona_overlays.yaml` from the same `prompts_dir`; if `persona` arg is provided, check `personas[persona].applies_to` contains `prompt_id`; if yes, merge `overlay_vars` into `fmt_vars` (caller-supplied values win); if persona not found or not applicable, fall through silently with a WARNING log
- Startup validation: enumerate all declared `required_vars` per prompt; verify each appears as `{var_name}` in the template (accounting for `{{` literal brace escapes); log + raise on discrepancy

**Test ships with it (`test_prompt_registry.py`):**
- `test_load_from_directory` — builds registry from a temp dir with a minimal valid YAML; asserts `list_prompts()` returns entries
- `test_get_prompt_formats_correctly` — supplies all required_vars; asserts formatted text contains substituted values
- `test_missing_var_raises` — omits a required var; asserts `MissingVarsError` raised
- `test_unknown_prompt_raises` — asserts `PromptNotFoundError` for a nonexistent id
- `test_locked_checksum_valid` — creates a locked prompt with correct checksum; asserts `get_prompt` succeeds
- `test_locked_checksum_mismatch_raises` — creates a locked prompt with wrong checksum; asserts `LockedPromptTamperedError` at load time
- `test_malformed_yaml_raises` — writes invalid YAML; asserts `PromptStoreError` at construction
- `test_hot_reload_picks_up_changes` — writes YAML, constructs registry, modifies YAML file with new template text and bumped mtime, calls `get_prompt`, asserts new text is returned
- `test_persona_overlay_applied` — creates registry with `persona_overlays.yaml`; calls `get_prompt("capture_intent", persona="tpm", ...)`; asserts `persona_key_fields` is injected from overlay
- `test_persona_overlay_missing_logs_warning` — unknown persona; asserts WARNING logged and call succeeds with empty overlay vars
- `test_reload_keeps_last_good_on_bad_yaml` — loads good YAML, corrupts file, calls `reload()`; asserts `PromptStoreError` raised and registry still serves last-good prompts

**Acceptance check:** All 11 tests pass. No LLM required. No server restart required to observe YAML changes in a `test_hot_reload_picks_up_changes` run.

**Depends on:** Nothing (pure new file, no import from conversation.py or other modified files).
**Blocks:** Serial stream (C1–C4) — cutover cannot start until this passes.

---

### P2 — YAML Prompt Files (Verbatim Migration from Python Constants)

**Files touched (NEW):**
- `framework/config/prompts/skill_builder.yaml` (new)
- `framework/config/prompts/executor.yaml` (new)
- `framework/config/prompts/persona_overlays.yaml` (new — absorbs persona_prompts.yaml content)

Do NOT delete `framework/config/persona_prompts.yaml` yet — that happens in C1 after the
conversation.py cutover is validated.

**Change description (4 sentences):**
Copy every prompt constant verbatim from its Python source file into the appropriate YAML file, using the block-scalar `template: |` field. The persona_overlays.yaml file mirrors the current `persona_prompts.yaml` structure but under the new schema (`personas:` top-level key, `applies_to:`, `overlay_vars:`). The executor inline prompt is named `executor_extract` in `executor.yaml`. For `failure_classifier` only, compute and insert the `checksum` field (see checksum procedure below).

**Prompt-to-YAML mapping:**

| Python constant | Source file | YAML file | YAML id |
|---|---|---|---|
| `_CAPTURE_INTENT_PROMPT` | conversation.py:231 | skill_builder.yaml | `capture_intent` |
| `_CONFIGURE_SOURCES_SUGGEST_PROMPT` | conversation.py:269 | skill_builder.yaml | `configure_sources` |
| `_INSPECT_SOURCES_PROMPT` | conversation.py:301 | skill_builder.yaml | `inspect_sources` |
| `_DESIGN_SKILL_PROMPT` | conversation.py:352 | skill_builder.yaml | `design_skill` |
| `_REVIEW_DESIGN_REPLAN_PROMPT` | conversation.py:432 | skill_builder.yaml | `review_design_replan` |
| `_EVAL_JUDGE_PROMPT` | conversation.py:460 | skill_builder.yaml | `eval_judge` |
| `_CLARIFY_PROMPT` | conversation.py:487 | skill_builder.yaml | `clarify` |
| `_FAILURE_CLASSIFIER_PROMPT` | conversation.py:525 | skill_builder.yaml | `failure_classifier` |
| `_DESCRIPTION_SYNTHESIS_PROMPT` | synthesize_schema.py:14 | skill_builder.yaml | `description_synthesis` |
| `_REVIEW_EXTRACT_PROMPT` | review.py:242 | skill_builder.yaml | `review_extract` |
| (inline) executor.py:493 | executor.py | executor.yaml | `executor_extract` |
| (n/a) | persona_prompts.yaml | persona_overlays.yaml | all 9 persona stanzas |

**required_vars per prompt (to populate the YAML field):**

| id | required_vars |
|---|---|
| `capture_intent` | `[persona, intent, persona_key_fields]` |
| `configure_sources` | `[persona, normalised_intent, adapter_list, intent_text]` |
| `inspect_sources` | `[source_id, persona, normalised_intent, sample_content]` |
| `design_skill` | `[persona, normalised_intent, source_capability, artifact_layout, existing_kb_cards, persona_key_fields, persona_extraction_style, persona_few_shot_example]` |
| `review_design_replan` | `[current_design, edit_request, updated_source_capability]` |
| `eval_judge` | `[field_name, field_description, extracted_value, source_snippet]` |
| `clarify` | `[question]` |
| `failure_classifier` | `[normalised_intent, schema_properties, capability_inventory, gap_report, missing_sections, thin_sections]` |
| `description_synthesis` | `[artifact_type, persona, intent, field_contexts]` |
| `review_extract` | `[field_lines, text]` |
| `executor_extract` | `[field_lines, user_request, snippet]` |

Note: `design_skill` lists `persona_key_fields`, `persona_extraction_style`, `persona_few_shot_example` as required_vars because the template has those placeholders. The persona overlay mechanism will resolve them automatically when `persona` is passed — the registry will merge them from `persona_overlays.yaml` before formatting. For personas not in the overlay file, the registry falls back to empty strings (matching today's behavior). Since the template has these placeholders, the loader must receive them one way or another — either via overlay or via explicit caller arg; the loader raises `MissingVarsError` only if neither path supplies the var.

**Checksum procedure for failure_classifier (MANDATORY):**

```python
import hashlib
# Read the constant text from conversation.py — do NOT include the triple-quotes,
# do NOT include the leading variable name. Only the string content between the
# triple-quote delimiters.
text = _FAILURE_CLASSIFIER_PROMPT   # the Python string object
checksum = "sha256:" + hashlib.sha256(text.rstrip("\n").encode("utf-8")).hexdigest()
# Insert this value as the `checksum:` field in skill_builder.yaml for failure_classifier
```

This procedure must be run against the live Python constant (not a re-typed copy) to ensure the checksum matches the validated text. Print the checksum and paste it into the YAML. The gate test (G1) will verify it.

**model/max_tokens/response_format values per prompt (from current call sites):**

| id | model | max_tokens | response_format |
|---|---|---|---|
| `capture_intent` | synthesis | 1024 | json_object |
| `configure_sources` | synthesis | 1024 | json_object |
| `inspect_sources` | synthesis | 2048 | json_object |
| `design_skill` | synthesis | 4096 | json_object |
| `review_design_replan` | synthesis | 2048 | json_object |
| `eval_judge` | synthesis | 256 | json_object |
| `clarify` | n/a (turn message, not LLM call) | n/a | n/a |
| `failure_classifier` | synthesis | 512 | json_object |
| `description_synthesis` | synthesis | 2048 | json_object |
| `review_extract` | synthesis | 4096 | json_object |
| `executor_extract` | synthesis | 4096 | json_object |

Note on `clarify`: It is a turn message template, not an LLM call. It should be included
in the YAML store with `model: none` and `response_format: none` so the registry can serve
it (and prompt_lab can display it) without treating it as an LLM call. The call site in
`_advance_to_clarify` uses it as a string template only — no `.chat()` call is made.
This is a deliberate inclusion: it makes all user-facing string templates in one place,
which is the goal of the exercise.

**Test ships with it:**
A pytest in `test_prompt_registry.py` (P1 task) that loads the real `framework/config/prompts/`
directory and asserts every expected prompt ID is present. No LLM needed. This test is added to
P1 but depends on the YAML files from P2 being committed — mark as `@pytest.mark.integration`
or guard with a `Path.exists()` check.

Additionally, a standalone checksum-verification test in `test_failure_classifier_gate.py`:
```python
def test_yaml_checksum_matches_stored_value(self):
    """The YAML file's checksum field must match SHA-256 of the template text."""
```
This test does NOT require LLM — it is a pure string comparison.

**Acceptance check:**
- All 3 YAML files are valid YAML, parseable with `yaml.safe_load()` with no errors.
- `PromptRegistry(PROMPTS_DIR)` constructs without error (once P1 is done).
- Every prompt ID listed in the mapping table above is present in the registry.
- The checksum for `failure_classifier` is present and matches the SHA-256 of the
  current Python constant text.

**Depends on:** P1 schema definition (need to know the YAML schema to write valid YAML).
**Blocks:** Serial stream (C1–C4) — the YAML must exist and load cleanly before the
call sites are cut over.

---

### P3 — Prompt-Test Harness CLI + Fixtures

**Files touched (NEW):**
- `framework/tools/prompt_lab.py` (new)
- `framework/tools/__init__.py` (new or add to existing)
- `framework/tests/fixtures/prompts/` (new directory)
- `framework/tests/fixtures/prompts/failure_classifier_gold.json` (new)
- `framework/tests/fixtures/prompts/capture_intent_tpm_26ai.json` (new)
- `framework/tests/fixtures/prompts/configure_sources_tpm.json` (new)
- `framework/tests/fixtures/prompts/inspect_sources_26ai.json` (new)
- `framework/tests/fixtures/prompts/design_skill_tpm_26ai.json` (new)
- `framework/tests/fixtures/prompts/review_design_replan.json` (new)
- `framework/tests/fixtures/prompts/eval_judge_sample.json` (new)
- `framework/tests/fixtures/prompts/review_extract_sample.json` (new)
- `framework/tests/fixtures/prompts/executor_extract_sample.json` (new)
- `framework/tests/unit/test_prompt_lab.py` (new)

**Change description (4 sentences):**
Implement `prompt_lab.py` as a standalone CLI (argparse, not a kb-cli subcommand — it should run independently without the full server dependency chain) that loads `PromptRegistry`, accepts a prompt_id and a JSON fixture file, formats the prompt, calls the live OCI GenAI LLM, and prints the result. Add `--runs N`, `--golden`, `--var`, `--reload`, `--list`, and `docs` subcommands per ADR-030 §Design §6. Populate 9 fixture files with realistic inputs drawn from the 26ai walkthrough session data (the gold-case inputs in `test_failure_classifier_gate.py` are the canonical source for `failure_classifier_gold.json` — copy verbatim).

**Fixture format (from ADR-030 §Design §6):**
```json
{
  "fixture_id": "failure_classifier_gold",
  "prompt_id": "failure_classifier",
  "description": "Gold case: tpm.26ai_fa_db_upgrade — WBS data exists, schema never asked",
  "persona": null,
  "vars": {
    "normalised_intent": { ... },
    "schema_properties": { ... },
    "capability_inventory": { ... },
    "gap_report": "Structure gap: ...",
    "missing_sections": ["Key Milestones", "ORM Status", "Risk Mitigation", "Next Steps"],
    "thin_sections": ["Status"]
  }
}
```

**`failure_classifier_gold.json` must be identical to the gold inputs in
`test_failure_classifier_gate.py`** (the `GOLD_*` constants). Copy them verbatim to make
the fixture the single source of truth. The gate test (G1) should be updated to load from
the fixture file rather than duplicating the data — see G1 task.

**CLI commands to implement:**

```
prompt_lab --list
prompt_lab run <prompt_id> --fixture <path> [--runs N] [--golden <path>] [--var k=v ...] [--reload] [--show-full-prompt]
prompt_lab docs [--output <path>]   # generates authorskill-prompts.md
```

**Blocked mode:** If LLM returns `{"_stub": true}` in the probe response, print
`BLOCKED — LLM unreachable. Token refresh: oci session authenticate --profile adpcpprod --region eu-frankfurt-1`
and exit with code 2. No fallback, no mock. (Same pattern as the gate test's `_make_real_llm`.)

**Test ships with it (`test_prompt_lab.py`):**
- `test_list_command_no_llm` — calls `--list` subcommand; asserts output contains known prompt IDs; no LLM
- `test_fixture_format_validates` — loads each fixture file, asserts schema valid (fixture_id, prompt_id, vars present)
- `test_fixture_vars_satisfy_prompt_required_vars` — for each fixture, asserts that the vars in the fixture satisfy the `required_vars` of the corresponding prompt (using registry, no LLM)
- `test_run_dry_run_format_only` — calls `run` with `--dry-run` flag (add this flag) that prints the formatted prompt but does not call the LLM; asserts no LLM connection made
- `test_docs_command_generates_markdown` — calls `docs` subcommand writing to a temp file; asserts output file contains known prompt IDs and the "DO NOT HAND-EDIT" header

**Acceptance check:**
- `python -m framework.tools.prompt_lab --list` prints a table of prompt IDs
- `python -m framework.tools.prompt_lab run failure_classifier --fixture framework/tests/fixtures/prompts/failure_classifier_gold.json --dry-run` prints the formatted classifier prompt without calling the LLM
- All fixture files are valid JSON satisfying the fixture schema
- All test_prompt_lab tests pass (no LLM required)

**Depends on:** P1 (PromptRegistry must exist), P2 (YAML files must exist for --list and fixture var validation).
**Blocks:** G1 (gate task uses the harness fixture).

---

### P4 — authorskill-prompts.md Generator + CI Check

**Files touched:**
- `framework/tools/prompt_lab.py` — `docs` subcommand (add to P3 work if same agent, or separate task)
- `docs/wiki/authorskill-prompts.md` — regenerated (overwrite)
- `Makefile` or `framework/scripts/generate-prompt-docs.sh` (new)

**Change description (3 sentences):**
The `prompt_lab docs` subcommand reads all YAML files in `framework/config/prompts/`, generates `docs/wiki/authorskill-prompts.md` with a DO-NOT-HAND-EDIT header, one section per prompt with full template and metadata, and a generated-at timestamp. The Makefile (or shell script) target wraps this command for convenience. The existing `docs/wiki/authorskill-prompts.md` is overwritten — its current content is superseded by the generated version.

**Generated file header:**
```markdown
---
title: authorSkill — Full Prompt Dump (GENERATED — DO NOT HAND-EDIT)
source: framework/config/prompts/*.yaml
generator: python -m framework.tools.prompt_lab docs
generated_at: <ISO timestamp>
owner: architect
tags: [skill-builder, prompts, adr-030]
status: generated
---
```

**Test ships with it:**
- In CI (or as a local pre-commit hook): run `python -m framework.tools.prompt_lab docs --output /tmp/authorskill-prompts.md` and assert `git diff --exit-code docs/wiki/authorskill-prompts.md`. If there is a diff, CI fails with message "authorskill-prompts.md is stale. Run: python -m framework.tools.prompt_lab docs".
- A pytest in `test_prompt_lab.py` (already in P3) covers the docs subcommand.

**Acceptance check:** After running the generator, `docs/wiki/authorskill-prompts.md` contains a section for every prompt in the YAML store. The file's `generated_at` field updates on each run.

**Depends on:** P2 (YAML files), P3 (prompt_lab docs subcommand). Can be done after P3.
**Blocks:** Nothing (documentation only).

---

### C1 — conversation.py Cutover (SERIAL — 8 prompts)

**Files touched:**
- `framework/skill_builder/conversation.py` — remove 8 prompt constants; replace 8 `.format(...)` call sites with `get_registry().get_prompt(...)`; remove `_load_persona_prompt_fragments`, `_reload_persona_prompts`, `_PERSONA_PROMPTS_YAML_PATH`, `_PERSONA_PROMPT_FRAGMENTS` (the persona overlay is now handled by the registry); remove `import yaml` if it was only used by the persona loader
- `framework/config/persona_prompts.yaml` — DELETE (content has migrated to persona_overlays.yaml in P2)

**This task is ATOMIC: all 8 constants removed and all 8 call sites switched in ONE commit.**

Do NOT remove some constants and leave others — that creates a half-migrated import that is harder to reason about than the original.

**Change description (4 sentences):**
Import `get_registry` from `framework.skill_builder.prompt_registry` at the top of `conversation.py`. For each of the 8 prompt constants, remove the constant and replace the `_CONST.format(**kwargs)` call with `get_registry().get_prompt(id, **kwargs).text`. The `max_tokens` and `response_format` values are now read from `get_registry().get_prompt(id).max_tokens` and `.response_format` rather than hardcoded at each call site. Remove the persona prompt loader functions (`_load_persona_prompt_fragments`, `_reload_persona_prompts`, `_PERSONA_PROMPT_FRAGMENTS`, `_PERSONA_PROMPTS_YAML_PATH`) — these are replaced by the registry's persona overlay mechanism.

**Call-site transformation pattern:**

Before:
```python
persona_frags = _load_persona_prompt_fragments(persona)
key_fields_text = ", ".join(persona_frags.get("key_fields", [])) or "(none specified)"
prompt = _CAPTURE_INTENT_PROMPT.format(
    persona=persona,
    intent=intent,
    persona_key_fields=key_fields_text,
)
result = self._llm.chat(
    model="synthesis",
    messages=[{"role": "user", "content": prompt}],
    response_format={"type": "json_object"},
    max_tokens=1024,
)
```

After:
```python
spec = get_registry().get_prompt("capture_intent", persona=persona,
                                  persona=persona, intent=intent)
result = self._llm.chat(
    model=spec.model,
    messages=[{"role": "user", "content": spec.text}],
    response_format=spec.response_format,
    max_tokens=spec.max_tokens,
)
```

Note: `persona` is passed both as a keyword arg to `get_prompt` (for overlay resolution) and as a `fmt_var` (for the `{persona}` placeholder in the template). The registry's `get_prompt` signature accepts `persona` as a dedicated keyword — it is consumed for overlay lookup, then also injected into `fmt_vars` automatically so the `{persona}` placeholder is satisfied. This is a registry implementation detail that must be handled in P1.

**Failure_classifier call site:** The `_FAILURE_CLASSIFIER_PROMPT` call in `_classify_and_route` (~line 4743) becomes:
```python
spec = get_registry().get_prompt(
    "failure_classifier",
    normalised_intent=...,
    schema_properties=...,
    capability_inventory=...,
    gap_report=...,
    missing_sections=...,
    thin_sections=...,
)
```
No `persona=` arg (the classifier is persona-agnostic). The registry will verify the checksum before returning `spec.text`. If the checksum mismatches, the registry raises `LockedPromptTamperedError` which propagates to the eval handler and surfaces as a hard-fail operator error (same behavior as if the LLM were unreachable).

**Test ships with it:**
Run the existing test suite. The tests in `test_skill_builder_conversation.py` must pass
unchanged — the behavior is identical, only the loading mechanism changes. Run:
```bash
pytest framework/tests/unit/test_skill_builder_conversation.py -x
```
Additionally, the structural contract tests in `TestClassifierPromptContract` in
`test_failure_classifier_gate.py` must pass — they still validate the prompt shape.
Update those tests to import from the registry rather than from the Python constant:
```python
spec = get_registry().get_prompt("failure_classifier",
    normalised_intent="...", schema_properties="...",
    capability_inventory="...", gap_report="...",
    missing_sections="...", thin_sections="...")
prompt_template = spec.text   # already formatted — but for structural tests use the raw template
```
Actually: the structural tests check the template text for substrings. Update them to load the
raw YAML template text (before format) via `registry._raw_template("failure_classifier")` —
add this helper method to P1 for this purpose.

**Acceptance check:**
- `python -m pytest framework/tests/unit/test_skill_builder_conversation.py` — 0 failures
- `python -m pytest framework/tests/unit/test_failure_classifier_gate.py::TestClassifierPromptContract` — 0 failures (no LLM needed)
- `python -m pytest framework/tests/unit/test_prompt_registry.py` — 0 failures
- `framework/config/persona_prompts.yaml` deleted; import of `yaml` removed from conversation.py (if orphaned)
- Server starts without error (run `python -m framework.cli.kb_cli skill-builder --persona tpm` and confirm it reaches CAPTURE_INTENT)

**Depends on:** P1 (registry must exist), P2 (YAML must load).
**Blocks:** C2, C3, C4 (serial stream — do not start C2 until C1 is verified).

---

### C2 — synthesize_schema.py Cutover (SERIAL — 1 prompt)

**Files touched:**
- `framework/skill_builder/synthesize_schema.py` — remove `_DESCRIPTION_SYNTHESIS_PROMPT` constant; replace call site with `get_registry().get_prompt("description_synthesis", ...)`

**Change description (2 sentences):**
Remove the `_DESCRIPTION_SYNTHESIS_PROMPT` constant at line 14. Replace the `_DESCRIPTION_SYNTHESIS_PROMPT.format(...)` call in `_llm_synthesize_descriptions` with `get_registry().get_prompt("description_synthesis", artifact_type=..., persona=..., intent=..., field_contexts=...).text`.

**Test ships with it:** `test_adr026_source_grounded_review.py` exercises this path. Run it; expect 0 failures.

**Acceptance check:** `pytest framework/tests/unit/test_adr026_source_grounded_review.py` passes.

**Depends on:** C1 complete.
**Blocks:** C3.

---

### C3 — review.py Cutover (SERIAL — 1 prompt)

**Files touched:**
- `framework/skill_builder/review.py` — remove `_REVIEW_EXTRACT_PROMPT` constant at line 242; replace call site in `_llm_extract` (~line 394) with `get_registry().get_prompt("review_extract", field_lines=..., text=...).text`

**Change description (2 sentences):**
Remove the `_REVIEW_EXTRACT_PROMPT` constant. Replace the single `_REVIEW_EXTRACT_PROMPT.format(...)` call with the registry call; `max_tokens` is taken from `spec.max_tokens` (must match the current hardcoded `_EXTRACT_MAX_TOKENS = 4096` — verify this is identical).

**Test ships with it:** `test_review.py` exercises `_llm_extract`. Run it; expect 0 failures.

**Acceptance check:** `pytest framework/tests/unit/test_review.py` passes.

**Depends on:** C2 complete.
**Blocks:** C4.

---

### C4 — executor.py Cutover (SERIAL — 1 inline prompt)

**Files touched:**
- `framework/workflow_runtime/executor.py` — remove the inline prompt construction at lines ~493–504; replace with `get_registry().get_prompt("executor_extract", field_lines=..., user_request=..., snippet=...)`

**This is the production render-time extraction prompt. Extra care required.**

**Change description (4 sentences):**
The inline prompt in `_llm_extract_fields` is assembled via string concatenation and f-strings — it has no constant name. Name it `executor_extract` in the registry. The `field_lines` variable is a `chr(10).join(field_lines)` expression; `user_request` is `inputs.get('input', '')`; `snippet` is `text[:24000]`. After the cutover, `spec.max_tokens` replaces the local `_MAX_TOKENS = 4096` constant — verify they match (registry has `max_tokens: 4096` for `executor_extract`).

**Template reconstruction for executor_extract (verbatim text from executor.py:493–504):**
```
You are extracting structured fields from a Confluence/wiki page to populate an
executive-review presentation. Return a single JSON object with EXACTLY these keys
(use empty string "" or empty list [] when a field is genuinely absent — do not
invent data):

{field_lines}

User request: {user_request}

=== Source document ===
{snippet}
=== End source ===

Respond with ONLY the JSON object, no prose, no markdown fences.
```
This is the template that goes into `executor.yaml` (P2 task). The dev doing C4 must verify
that `spec.text` after formatting produces exactly the same string as the old inline construction
would have produced for the same inputs — use an assert in a local test script before committing.

**Test ships with it:** `test_emcp_runtime.py` and `test_skill_builder_conversation.py` (EVAL path) exercise executor calls. Run both; expect 0 failures.

**Acceptance check:**
- `pytest framework/tests/unit/test_emcp_runtime.py` passes
- End-to-end smoke: `python -m framework.cli.kb_cli workflow-run tpm.weekly_exec_review --inputs '{"project": "all"}'` produces a response (server-mode integration test — skip if no OCI access)

**Depends on:** C3 complete.
**Blocks:** G1.

---

### G1 — Gate Task: Classifier Checksum Verification + Live LLM Gate Re-run

**Files touched:**
- `framework/tests/unit/test_failure_classifier_gate.py` — update import path from Python constant to registry
- `framework/tests/fixtures/prompts/failure_classifier_gold.json` — if not already canonical source, make it so

**Change description (4 sentences):**
Update `test_failure_classifier_gate.py` to import the classifier prompt via `get_registry().get_prompt("failure_classifier", ...)` instead of importing `_FAILURE_CLASSIFIER_PROMPT` from `conversation.py`. Add `TestClassifierYamlChecksum` test class with two tests: (a) the YAML `checksum` field matches the SHA-256 of the raw template text; (b) the raw template text is byte-identical to the original Python constant text (verified by comparing SHA-256s). Run the live LLM gate (3 runs, gold case) to confirm the migrated prompt still passes.

**Test ships with it:**
The existing `TestFailureClassifierGate` live LLM tests must still pass (3/3 runs → MISSING_FIELDS or THIN_FIELDS). The new `TestClassifierYamlChecksum` tests must pass without LLM.

**Acceptance check:**
- `pytest framework/tests/unit/test_failure_classifier_gate.py::TestClassifierYamlChecksum` — PASS (no LLM)
- `pytest framework/tests/unit/test_failure_classifier_gate.py::TestFailureClassifierGate` — PASS (live LLM, 3/3 correct)
- If the live LLM gate fails after migration, the migration is blocked. The template must be byte-identical to the validated text. Recheck the P2 YAML copy procedure.

**Depends on:** C1 (conversation.py cutover done), P2 (YAML with checksum committed).
**Blocks:** Declaring the ADR-030 migration complete.

---

## 2. Fan-out Diagram

```
DAY 0 (all parallel — no dependencies between them):
  Agent A → P1 (PromptRegistry loader + tests)
  Agent B → P2 (YAML files — verbatim constants + checksum for classifier)
  Agent C → P3 (prompt_lab harness + fixtures)
  Agent D → P4 (authorskill-prompts.md generator) [can wait for P3 if same agent]

GATE (block serial stream until all P-streams pass):
  P1 tests pass + P2 YAML loads cleanly via PromptRegistry → UNBLOCK serial stream

DAY 1 (serial — one agent, one at a time):
  Agent E → C1 (conversation.py — 8 prompts + delete persona_prompts.yaml)
    verify → C2 (synthesize_schema.py — 1 prompt)
      verify → C3 (review.py — 1 prompt)
        verify → C4 (executor.py — 1 inline prompt)

DAY 1 end / DAY 2:
  Agent F → G1 (gate test update + live LLM re-run)
    PASS → ADR-030 migration complete

  Agent D (if not done) → P4 (docs generator)
```

**Recommended team allocation:**
- Dev A (familiar with Python module infrastructure): P1
- Dev B (familiar with YAML config layer + conversation.py constant locations): P2
- Dev C (any dev): P3 (harness)
- Dev D (any dev): P4 (docs generator) — can fold into P3 if same agent
- Dev E (the serial-stream specialist — whoever did ADR-028/029 serial work): C1→C2→C3→C4
- Dev F (or same as Dev E): G1

---

## 3. Serialization Points (critical gates)

| Gate | Condition to unblock | Consequence of proceeding before gate |
|---|---|---|
| P1+P2 → serial stream | All P1 tests pass; P2 YAML loads via `PromptRegistry(PROMPTS_DIR)` with no errors; failure_classifier checksum verified | C1 imports a non-existent module; server breaks |
| C1 → C2 | `test_skill_builder_conversation.py` passes; `TestClassifierPromptContract` passes; server starts to CAPTURE_INTENT | C2 may remove a constant that C1 still imports (race) |
| C2 → C3 | `test_adr026_source_grounded_review.py` passes | trivially safe but discipline matters |
| C3 → C4 | `test_review.py` passes | trivially safe but discipline matters |
| C4 → G1 | `test_emcp_runtime.py` passes | gate test imports would fail on missing executor_extract |
| G1 → done | Live LLM gate passes 3/3 | Classifier routing guarantee is not re-verified post-migration |

---

## 4. Technically Unsound Things to Flag (Last Chance to Object)

1. **`persona` as both a fmt_var and an overlay key in `get_prompt`.** The `{persona}` placeholder in several templates AND the persona overlay lookup both use the `persona` keyword. The P1 implementation must handle this carefully: `persona` passed to `get_prompt(persona=...)` is consumed for overlay lookup AND injected into `fmt_vars` as `persona` so the template placeholder is satisfied. If the P1 implementor treats `persona` as purely an overlay key and strips it from `fmt_vars`, the `{persona}` placeholder in templates like `inspect_sources` and `configure_sources` will cause a `MissingVarsError`. Explicitly document this double-use in the registry code.

2. **The `clarify` prompt has `model: none` — the registry must not call `.chat()` on it.** The harness and the registry must handle the `none` model gracefully. The call site in `conversation.py` only uses `spec.text`; it does not call `self._llm.chat`. If a future change accidentally passes `clarify` to an LLM caller, the registry should emit a WARNING when `spec.model == "none"`. Consider adding a `is_llm_prompt` boolean to `PromptSpec`.

3. **The executor inline prompt uses f-string formatting, not `.format()`.** The current code uses `f"{chr(10).join(field_lines)}"` directly in the string concatenation, not a `{field_lines}` placeholder in a `.format()` call. When extracting to YAML, the template must be written with `{field_lines}` as a standard `str.format()` placeholder. The C4 task must verify the reformatted string is byte-identical to the f-string output for known inputs. This is the highest-risk migration step.

4. **Reload atomicity.** The hot-reload path must swap the entire registry state atomically — not partially update it. If the YAML is loaded mid-request, a partial load could serve prompts from mixed old/new state. The P1 load-then-swap pattern (load into a temporary dict, validate fully, then swap the `_cache` attribute) must be implemented correctly. Python GIL does not protect multi-step attribute assignments in multi-threaded servers.

5. **`required_vars` for `design_skill` includes persona overlay vars.** The `design_skill` template has `{persona_key_fields}`, `{persona_extraction_style}`, `{persona_few_shot_example}` placeholders. These are satisfied by the persona overlay when `persona` is supplied. But the registry's `MissingVarsError` check runs AFTER overlay resolution — so it will not fire if the persona overlay correctly supplies them. If `persona` is not supplied to `get_prompt("design_skill")`, the overlay vars will be missing and `MissingVarsError` will fire. This is the correct behavior (DESIGN_SKILL always needs persona) but it must be tested explicitly in P1.

6. **`test_failure_classifier_gate.py::TestClassifierPromptContract` checks substrings against the raw template.** After C1 removes `_FAILURE_CLASSIFIER_PROMPT` from `conversation.py`, the import in the test (`from framework.skill_builder.conversation import _FAILURE_CLASSIFIER_PROMPT`) will break. The G1 task must update this import to use the registry. The `test_prompt_has_all_required_format_kwargs` test checks for substring `{normalised_intent}` in the template — after migration, it should check in the raw template string from the registry, not a formatted output. Add `_raw_template(prompt_id) -> str` to the registry for this use case.

---

## 5. What This Blueprint Does NOT Cover (out of scope)

- Migrating prompts in `framework/eval/` (if any exist) — not part of authorSkill flow
- Adding new prompts (that is a post-ADR-030 activity)
- A/B testing infrastructure or multi-version prompt routing
- Automated CI gate for the live LLM test (G1 remains manual / on-demand per current pattern)
