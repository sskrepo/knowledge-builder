---
title: "ADR-030 — authorSkill: Externalize LLM Prompts to Hot-Reloadable Versioned YAML + Prompt-Test Harness"
status: accepted
created: 2026-05-16
decided: 2026-05-16
owner: architect
deciders: user, tpm
supersedes: ~
tags: [arch, skill-builder, prompts, tooling, adr-028, adr-029]
related: [ADR-028, ADR-029, ADR-015, ADR-027]
---

# ADR-030 — authorSkill: Externalize LLM Prompts to Hot-Reloadable Versioned YAML + Prompt-Test Harness

## Status

**Accepted — 2026-05-16.** Decision made: Externalize to YAML + harness.

---

## Context

### The problem

Every authorSkill LLM prompt is a hard-coded Python module constant. When a prompt engineer wants to iterate on a prompt — to fix a thinness regression, tune the anti-bias guard in the classifier, or adjust persona guidance — the edit cycle is:

1. Edit a Python constant in a `.py` file
2. Restart the server (which reloads all of Python)
3. Walk through a full authorSkill session (17 states, 7–14 LLM calls, ~15 minutes)
4. Observe the effect at the target state

This makes prompt iteration economically painful. Steps 2–4 are required even for a one-word change to a prompt that affects only INSPECT_SOURCES. The pain is compounded by the distribution of prompts across four files:

- `framework/skill_builder/conversation.py` — 8 constants: `_CAPTURE_INTENT_PROMPT`, `_CONFIGURE_SOURCES_SUGGEST_PROMPT`, `_INSPECT_SOURCES_PROMPT`, `_DESIGN_SKILL_PROMPT`, `_REVIEW_DESIGN_REPLAN_PROMPT`, `_EVAL_JUDGE_PROMPT`, `_CLARIFY_PROMPT`, `_FAILURE_CLASSIFIER_PROMPT`
- `framework/skill_builder/synthesize_schema.py` — 1 constant: `_DESCRIPTION_SYNTHESIS_PROMPT`
- `framework/skill_builder/review.py` — 1 constant: `_REVIEW_EXTRACT_PROMPT`
- `framework/workflow_runtime/executor.py` — 1 inline (unnamed) prompt assembled in `_llm_extract_fields` around line 493; this is the production render-time extraction prompt that drives all `/api/v1/ask` content

Total: **11 named constants + 1 unnamed inline = 12 prompt units**.

### The gate-locked constraint

`_FAILURE_CLASSIFIER_PROMPT` is gate-validated: the gate test `test_failure_classifier_gate.py` calls the live OCI GenAI LLM 3 times with the gold case and asserts all 3 runs return `MISSING_FIELDS` or `THIN_FIELDS` (never `SOURCE_COVERAGE`). This text is the validated result of that gate; changing it without re-passing the gate invalidates the routing guarantee for the S6 reject path.

### The persona_prompts.yaml finding (VERIFIED)

`framework/config/persona_prompts.yaml` is **LIVE and actively wired**. This is not dead code.

Evidence from `conversation.py`:
- `_PERSONA_PROMPTS_YAML_PATH` is set at module level (line 53)
- `_load_persona_prompt_fragments(persona)` is called at CAPTURE_INTENT (line 1208) and DESIGN_SKILL (line 2037)
- The loaded fragments (`key_fields`, `extraction_style`, `few_shot_example`) are injected as `{persona_key_fields}`, `{persona_extraction_style}`, `{persona_few_shot_example}` into `_CAPTURE_INTENT_PROMPT` and `_DESIGN_SKILL_PROMPT`
- The loader is already a partial hot-reload mechanism: `_reload_persona_prompts()` exists and can be called explicitly; in practice the cache is filled on first call and requires a server restart to update

The persona overlay is therefore a **functioning but restart-gated** mechanism. ADR-030 must fold it into the new hot-reload design — not replace it or leave it as a parallel mechanism. Post-ADR-030, `persona_prompts.yaml` becomes the `persona_overlays` section inside the YAML prompt store, and `_load_persona_prompt_fragments` is replaced by the loader's persona-overlay resolution.

---

## Decision

### Option A — Externalize all prompts to a hot-reloadable versioned YAML store + build a standalone prompt-test harness (CHOSEN)

Every prompt template moves from a Python constant to a YAML file. A `PromptRegistry` module loads, caches, and hot-reloads the YAML. Call sites replace `_PROMPT_CONSTANT.format(...)` with `registry.get_prompt(id, **fmt_vars)`. A `prompt_lab.py` CLI enables isolated prompt testing without walking the full 17-state flow.

### Option B — Externalize only the gate-locked classifier; keep others in Python

Minimal disruption — only the classifier moves to YAML so the checksum enforcement works cleanly. All other prompts remain in Python. Rejected because: the iteration-cost problem exists for all prompts, not just the classifier; partial externalization creates two inconsistent systems; and the persona overlay wiring remains restart-gated.

### Option C — Database-backed prompt versioning (Postgres/ADB)

Prompts stored in a database table with version, checksum, and A/B routing columns. Chosen by several large LLM platforms. Rejected for this project because: operational overhead (DDL, migrations, backup) is not justified at current scale; the git-backed YAML store gives free diff/PR/blame for prompt changes; no A/B routing requirement exists today.

---

## Design

### 1. YAML Prompt Store Layout

**Decision: one file per logical group, not one file per prompt and not one monolithic file.**

One file per prompt is 12 files; navigating them is tedious and there is no obvious boundary. One consolidated file is ~1,500 lines and makes per-file blame unusable. Grouping by source module (3 groups below) keeps each file under ~400 lines, matches the existing code structure, and gives meaningful git blame.

```
framework/config/prompts/
  skill_builder.yaml       # 9 prompts — conversation.py + review.py + synthesize_schema.py
  executor.yaml            # 1 prompt — executor.py inline extraction prompt
  persona_overlays.yaml    # absorbs persona_prompts.yaml content under the new schema
```

`persona_prompts.yaml` in `framework/config/` is **deprecated** once the cutover is complete. Its content migrates into `persona_overlays.yaml` under the new schema. The old file is removed in the same commit as the cutover.

### 2. YAML Schema Per Prompt

```yaml
# framework/config/prompts/skill_builder.yaml (excerpt)
prompts:
  capture_intent:
    id: capture_intent
    version: "1.0"
    model: synthesis
    max_tokens: 1024
    response_format: json_object
    # required_vars: list of {var_name} placeholders that MUST be supplied at call time
    # The loader validates these at startup and on reload.
    required_vars: [persona, intent, persona_key_fields]
    template: |
      You are a Knowledge Builder Framework assistant. Parse the user's intent into a
      normalised goal object so downstream design steps have a structured representation
      to work from.
      ... (verbatim text)

  failure_classifier:
    id: failure_classifier
    version: "1.0"
    model: synthesis
    max_tokens: 512
    response_format: json_object
    required_vars:
      [normalised_intent, schema_properties, capability_inventory,
       gap_report, missing_sections, thin_sections]
    # Gate-lock fields — see section 3 for enforcement details
    locked: true
    checksum: sha256:<hex>   # SHA-256 of the UTF-8 template string, no trailing newline
    template: |
      You are a Knowledge Builder Framework failure-class classifier. ...
      (verbatim byte-identical copy of the validated text)
```

Full per-prompt schema (all fields):

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | snake_case; matches the Python lookup key; unique within the store |
| `version` | string | yes | semver string; bumped on any template change |
| `model` | string | yes | one of `synthesis`, `fast`; maps to the OCI GenAI model alias |
| `max_tokens` | integer | yes | must match the call-site `max_tokens` value today (byte-identical migration) |
| `response_format` | `json_object` \| `text` | yes | corresponds to `response_format={"type": ...}` in LLM calls |
| `required_vars` | list[string] | yes | the `{placeholder}` names the template uses; validated at load time |
| `template` | string | yes | the prompt body with `{placeholder}` substitution markers |
| `locked` | bool | no | default false; when true, checksum is mandatory and enforced |
| `checksum` | string | conditional | required when `locked: true`; `sha256:<64-hex-chars>` |
| `description` | string | no | human-readable one-line summary; used by the prompt_lab CLI listing |
| `notes` | string | no | architect commentary — why this prompt exists, what it guards |

### 3. Persona Overlays Schema

`persona_overlays.yaml` replaces `persona_prompts.yaml` under the new registry. The registry resolves overlays at call time when `persona=` is passed.

```yaml
# framework/config/prompts/persona_overlays.yaml
personas:
  tpm:
    applies_to: [capture_intent, design_skill]   # prompt IDs this persona overlay applies to
    overlay_vars:
      persona_key_fields: "orm_status, rag_summary, schedule_health, blocking_issues, next_steps, exec_asks"
      persona_extraction_style: |
        Use exec-safe language throughout — ...
      persona_few_shot_example: |
        Field name: "blocking_issues"
        ...
  pm:
    applies_to: [capture_intent, design_skill]
    overlay_vars:
      ...
```

The registry's `get_prompt(prompt_id, persona=None, **fmt_vars)` signature:
- If `persona` is supplied and `persona_overlays.yaml` has a stanza for that persona that lists `prompt_id` in `applies_to`, the overlay's `overlay_vars` are merged into `fmt_vars` before template formatting (caller-supplied vars win on collision).
- If `persona` is not supplied or no stanza exists, the behavior is identical to today's `_load_persona_prompt_fragments` fallback: `persona_key_fields=""`, `persona_extraction_style=""`, `persona_few_shot_example=""`.
- Unknown persona logs a WARNING (identical behavior to today); does not hard-fail.

### 4. Loader / Registry Module

**Path:** `framework/skill_builder/prompt_registry.py`

**Public API:**

```python
class PromptRegistry:
    def __init__(self, prompts_dir: Path) -> None:
        """Load all YAML files in prompts_dir at construction time.
        Raises PromptStoreError on any load failure — hard-fail, no silent degradation.
        Validates all checksums for locked prompts.
        Validates that every template's required_vars are satisfiable (all {placeholder}
        names appear in required_vars, no extra undeclared vars).
        """

    def get_prompt(
        self,
        prompt_id: str,
        *,
        persona: str | None = None,
        **fmt_vars: str,
    ) -> PromptSpec:
        """Return the formatted prompt string and metadata for prompt_id.

        Raises:
          PromptNotFoundError  — prompt_id not in the store (hard-fail, no fallback)
          MissingVarsError     — one or more required_vars not supplied in fmt_vars
                                 and not resolvable from persona overlays (hard-fail)
          LockedPromptTamperedError — locked=true and checksum mismatch (hard-fail)
        """

    def reload(self) -> None:
        """Re-read all YAML files from disk.
        Called explicitly (e.g. by prompt_lab after a YAML edit).
        Also called automatically when _mtime_changed() detects a newer mtime
        on any file in prompts_dir — checked on each get_prompt() call.
        Hard-fails on malformed YAML (raises PromptStoreError, does NOT partially load).
        """

    def list_prompts(self) -> list[PromptMeta]:
        """Return id, version, description, locked, model for every loaded prompt.
        Used by prompt_lab --list."""

class PromptSpec:
    prompt_id: str
    version: str
    model: str        # "synthesis" | "fast"
    max_tokens: int
    response_format: dict   # e.g. {"type": "json_object"}
    text: str         # fully formatted prompt string

class PromptMeta:
    prompt_id: str
    version: str
    description: str
    locked: bool
    model: str

class PromptStoreError(RuntimeError): ...
class PromptNotFoundError(PromptStoreError): ...
class MissingVarsError(PromptStoreError): ...
class LockedPromptTamperedError(PromptStoreError): ...
```

**Hot-reload mechanism:** On every `get_prompt()` call, the registry checks whether the mtime of any YAML file in `prompts_dir` has changed since last load. If yes, `reload()` is called automatically before serving the prompt. This means editing a YAML file and re-running `prompt_lab` (or sending the next authorSkill message) picks up the change with zero server restart. The mtime check is a single `os.stat()` per file per call — negligible overhead at the call rates involved (7–14 calls per session).

**Startup validation:** At construction time (and on each `reload()`):
1. Every YAML file in `prompts_dir` is parsed; malformed YAML → hard-fail immediately.
2. For every prompt with `locked: true`, the `checksum` field is verified against the template text. Mismatch → `LockedPromptTamperedError` raised immediately. The server does not start with a tampered gate-locked prompt.
3. For every prompt, `required_vars` is cross-checked against the `{placeholder}` names in the template. Extra undeclared vars in the template → warning (some templates use `{{` literal brace escapes). Missing declared vars in the template → `PromptStoreError`.
4. A startup log line is emitted listing every loaded prompt id + version.

**Malformed YAML behavior:** Hard-fail at load time. The server does NOT start (or the reload does NOT complete) if any YAML in `prompts_dir` is syntactically invalid or schema-invalid. An operator-visible error message is printed. This is the "no silent fallback to a stale/empty string" requirement.

**Module registration:** A module-level singleton is initialised once when the server starts:

```python
# framework/skill_builder/prompt_registry.py
_registry: PromptRegistry | None = None

def get_registry(prompts_dir: Path | None = None) -> PromptRegistry:
    """Return (or construct) the module-level singleton."""
```

Call sites import `get_registry` and call `get_registry().get_prompt(...)`.

### 5. Gate-Locked Prompts — Enforcement

`_FAILURE_CLASSIFIER_PROMPT` is the only gate-locked prompt. It carries `locked: true` and a `checksum` field.

The checksum is computed as:
```
sha256(template.encode("utf-8").rstrip(b"\n"))
```
(stripping one trailing newline, which is the YAML block-scalar artifact).

**Enforcement chain:**
1. `PromptRegistry.__init__` and `reload()` verify the checksum before the registry is usable.
2. Any modification to the template text changes the checksum and causes `LockedPromptTamperedError` on the next load — the server hard-fails.
3. The gate test `test_failure_classifier_gate.py` is updated to import from the YAML via `get_registry().get_prompt("failure_classifier", ...)` rather than from the Python constant. The test still runs the live LLM 3 times; the gate still asserts MISSING_FIELDS or THIN_FIELDS. The gate now additionally asserts that `spec.text` matches the expected checksum — any drift from the validated text is caught at the test level as well.
4. To intentionally change the prompt (for prompt engineering): (a) edit the template, (b) recompute the checksum, (c) update both `template` and `checksum` in the YAML, (d) re-run the gate test. If the gate passes, the new checksum is committed. If the gate fails, the change is blocked.

The lock is therefore: YAML checksum stops tampering at load time; gate test stops promotion of a changed prompt until re-validated.

### 6. Prompt-Test Harness

**Path:** `framework/tools/prompt_lab.py`

**CLI invocation examples:**

```bash
# List all prompts in the store
python -m framework.tools.prompt_lab --list

# Run a prompt against the live LLM with a saved fixture
python -m framework.tools.prompt_lab run failure_classifier \
    --fixture framework/tests/fixtures/prompts/failure_classifier_gold.json

# Run N times (stability check — replicates the gate test without pytest)
python -m framework.tools.prompt_lab run failure_classifier \
    --fixture framework/tests/fixtures/prompts/failure_classifier_gold.json \
    --runs 3

# Diff output against a saved golden file
python -m framework.tools.prompt_lab run capture_intent \
    --fixture framework/tests/fixtures/prompts/capture_intent_tpm_26ai.json \
    --golden framework/tests/fixtures/prompts/golden/capture_intent_tpm_26ai.json

# Override a single var from the fixture (for quick iteration)
python -m framework.tools.prompt_lab run design_skill \
    --fixture framework/tests/fixtures/prompts/design_skill_tpm.json \
    --var persona_extraction_style "New style text here"

# Reload and re-run after editing a YAML file (no restart needed)
python -m framework.tools.prompt_lab run capture_intent \
    --fixture framework/tests/fixtures/prompts/capture_intent_tpm_26ai.json \
    --reload
```

**Harness behavior:**
- Loads `PromptRegistry` from `framework/config/prompts/` at startup.
- `--reload` flag forces `registry.reload()` before running; without it, the registry still auto-reloads if any file's mtime has changed (hot-reload works transparently).
- For each run, prints: prompt id + version, formatted prompt text (truncated to 500 chars unless `--show-full-prompt`), LLM call start timestamp, elapsed time, raw LLM response, and parsed JSON (if `response_format: json_object`).
- `--runs N`: runs the same fixture N times, prints all N outputs side-by-side, and prints a stability summary (all outputs identical? key field values consistent across runs?).
- `--golden path/to/golden.json`: after each run, computes a diff between the parsed output and the golden JSON. Prints added, removed, and changed keys. Does NOT fail on diff — this is informational (the harness is a debugging tool, not a gate test).
- If the LLM is unreachable (stub mode, no OCI token), prints `BLOCKED — LLM unreachable (stub mode detected). Refresh token: oci session authenticate --profile adpcpprod --region eu-frankfurt-1` and exits non-zero. No mock fallback — per CLAUDE.md no-stub policy.
- `--list`: prints a table of all prompt IDs, versions, model, max_tokens, locked status, description.

**Fixture format:**

```json
{
  "fixture_id": "failure_classifier_gold",
  "prompt_id": "failure_classifier",
  "description": "Gold case: tpm.26ai_fa_db_upgrade WBS content exists, schema never asked for it",
  "vars": {
    "normalised_intent": { ... },
    "schema_properties": { ... },
    "capability_inventory": { ... },
    "gap_report": "Structure gap: ...",
    "missing_sections": ["Key Milestones", "ORM Status", ...],
    "thin_sections": ["Status"]
  }
}
```

The `vars` dict maps directly to the `**fmt_vars` argument of `get_prompt()`. The `persona` key, if present, triggers persona overlay resolution.

**Fixture location:** `framework/tests/fixtures/prompts/`

Initial fixtures to be created alongside the migration (one per prompt):

| Fixture file | Prompt | Notes |
|---|---|---|
| `capture_intent_tpm_26ai.json` | `capture_intent` | Real TPM intent from the 26ai walkthrough session |
| `configure_sources_tpm.json` | `configure_sources` | TPM intent + adapter list |
| `inspect_sources_26ai.json` | `inspect_sources` | 26ai Confluence page sample |
| `design_skill_tpm_26ai.json` | `design_skill` | Full design call with inventory + KB cards |
| `review_design_replan.json` | `review_design_replan` | Edit request + current design |
| `eval_judge_sample.json` | `eval_judge` | One field + extracted value + source snippet |
| `failure_classifier_gold.json` | `failure_classifier` | The gold case from the gate test |
| `review_extract_sample.json` | `review_extract` | Schema + source page text |
| `executor_extract_sample.json` | `executor_extract` | Field lines + source page text |

**Fixtures for legacy prompts** (`clarify`, `analyze_artifact`, `description_synthesis`) are created but labeled `legacy: true` in the fixture JSON — the harness runs them but notes the legacy status.

### 7. authorskill-prompts.md as a Generated Artifact

`docs/wiki/authorskill-prompts.md` is currently hand-maintained and will drift from the YAML as soon as the first prompt edit is made post-migration. It must become a generated file.

**Generator command:**
```bash
python -m framework.tools.prompt_lab docs
# or via make:
make docs-prompts
```

This command reads `framework/config/prompts/*.yaml` and renders `docs/wiki/authorskill-prompts.md` with:
- One section per prompt (ordered by yaml file, then by id within file)
- The full template text in a fenced code block
- The `required_vars`, `version`, `model`, `max_tokens`, `response_format` metadata table
- The persona overlays for that prompt (from `persona_overlays.yaml`)
- The `description` and `notes` fields as prose
- A generated-at timestamp and a DO NOT HAND-EDIT header warning

The file is committed alongside any YAML change that adds or modifies a prompt. CI should verify the generated file is not stale (run the generator and check `git diff --exit-code docs/wiki/authorskill-prompts.md`).

### 8. Migration Plan

**Constraint:** `conversation.py` contains 8 of the 12 prompts and is the serial-stream file (same constraint established in ADR-028/029). Parallel edits to it collide. The executor.py inline has no constant name — it must be named before it can be externalized.

**Migration approach:** The YAML files and the loader are built first (parallel, no code collision). The Python call sites are cut over last, one file at a time, atomically per file. "Atomic per file" means the file's constants are all removed and all call sites within that file switch to `get_prompt()` in a single commit — not half-migrated.

**Byte-identical requirement:** Every migrated template must be byte-identical to the current Python constant (no whitespace changes, no punctuation changes). The Python constants use `"""\` with continuation lines that may have trailing spaces. The YAML block scalar (`template: |`) preserves interior newlines exactly. The migration task must verify this for each prompt by computing the SHA-256 of the constant text and the YAML template text and asserting they match.

**Backward compatibility:** None needed (internal server-side constants). The cutover is atomic per-file; there is no "both old and new" transition state within a file.

**Named ID for the executor.py inline prompt:** The unnamed inline prompt in `executor.py:_llm_extract_fields` is assigned the ID `executor_extract`. The comment block at line ~505 already describes it adequately; the new YAML entry's `description` field should read: "Production render-time extraction prompt for `/api/v1/ask` workflow output. Extracts structured fields from Confluence pages using the committed skill schema."

---

## Consequences

- **Positive:** Prompt iteration cost drops from ~15 minutes (full session walk) to ~5 seconds (prompt_lab single run). Every prompt change is a YAML diff — reviewable in PR, blameable in git.
- **Positive:** The gate-locked classifier is enforced at the loader level, not only in tests. A tampered classifier cannot be deployed.
- **Positive:** `authorskill-prompts.md` stops drifting — it is generated from the authoritative YAML.
- **Positive:** `persona_prompts.yaml` is folded into the new design (not left as a parallel restart-gated mechanism). Persona overlay edits are now hot-reloadable.
- **Negative:** One new module (`prompt_registry.py`) and one new CLI (`prompt_lab.py`) become load-bearing infrastructure. If the registry has a bug, all LLM calls in the authorSkill flow fail.
- **Negative:** The YAML migration of 12 prompts is a mechanical but non-trivial task — each requires byte-identical copy, checksum computation (for the classifier), and a call-site update.
- **Reversibility:** The Python constants can be restored in a single commit (revert the cutover commits). The YAML files are additive and do not break anything until the call sites are switched. Reversibility is high.

---

## References

- `docs/wiki/authorskill-prompts.md` — current prompt inventory (to become generated)
- `docs/wiki/authorskill-flow.md` — state machine context
- `framework/config/persona_prompts.yaml` — existing persona overlay file (to be migrated)
- `framework/tests/unit/test_failure_classifier_gate.py` — gate test that must stay passing
- ADR-028 — Item 1 (persona playbook, S4 injection) + Item 4 (synthesisable)
- ADR-029 — S6 constrained routing that depends on the gate-locked classifier
- CLAUDE.md rule: no stub mode, no silent fallback
