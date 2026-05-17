---
title: ADR-032 — Implementation Blueprint (P1 + P2)
status: active
created: 2026-05-16
owner: architect
tags: [blueprint, adr-032, workflow-skills, ingestion, consumption]
related: [ADR-032, ADR-016, ADR-029, ADR-030]
---

# ADR-032 — Implementation Blueprint: Ask-Parameterized Skills (P1 + P2)

**Decision locked:** DECISION-012 = Option C (ephemeral request-scoped ingestion).
**P3 shipped:** commit 8c947dc — `ConfluencePageNotInKBError` guard + 19 tests.
**This blueprint covers:** P1 (author-time source-binding contract) + P2 (Option C
ephemeral runtime ingestion) + P3 guard rewire (retire regex heuristic).

**Do not start implementation until reading:**
1. ADR-032 (Accepted) — full design rationale and concrete specs
2. ADR-016 — workflow skill YAML schema (this blueprint amends it)
3. ADR-030 — prompt externalization (prompt bumps go through this)
4. `framework/workflow_runtime/executor.py` — P3 guard already in place; P2 extends it
5. `framework/skill_builder/conversation.py` — 16-state machine; P1 extends several states

---

## Highest-Risk Item (Read First)

**Confluence adapter reachability from the mcp_server consumption process**

This is the single gating question for Option C. The answer, based on code inspection:

**YES — the Confluence adapter IS reachable from the mcp_server process.**

Evidence:
- `framework/skill_builder/conversation.py` already contains `_build_confluence_adapter()`,
  which initializes an emcp_direct / codex_proxy / native adapter. This function runs
  server-side during the INGEST state of `authorSkill` sessions, meaning the mcp_server
  process already successfully calls `emcp_direct.fetch()` in laptop mode.
- In laptop mode (`KBF_ENV=laptop`): `emcp_direct` uses macOS Keychain OAuth tokens
  stored by Codex. The mcp_server process runs in the same user session. The Keychain
  is accessible. Credential: `codex mcp login central_confluence` OAuth token.
- In production (`KBF_ENV=staging|production`): `native` adapter uses a service API
  token from OCI Vault. The mcp_server production process already reads Vault for other
  secrets. Same pattern applies. Credential: Confluence API token in OCI Vault.
- The only missing piece is that the adapter is not currently initialized at lifespan
  startup for the consumption path (only initialized on-demand per authorSkill session).
  P2 Task 5 (below) adds the lifespan initialization.

**If for any reason the adapter cannot be initialized** (missing Keychain token, Vault
secret not configured), the skill hard-fails with an actionable message:
```
"This skill requires live Confluence access to fetch the page you specified, but
no Confluence adapter is configured in this deployment. Contact your administrator
to configure the Confluence adapter (framework/config/adapters/confluence.yaml)."
```
No silent fallback. No wrong-page substitution.

---

## Serial Constraints (read before planning fan-out)

Two files are heavily serialized:

**conversation.py** — the 16-state machine. Every state handler is in this file.
Multiple P1 tasks touch different state handlers but editing the same file requires
strict serialization to avoid merge conflicts. The recommended approach:

- P1-A (capture_intent prompt bump) and P1-B (design_skill prompt bump) are
  YAML-file-only changes — they do not touch conversation.py. Run in parallel with
  everything else.
- P1-C (CLARIFY surface for source_binding ambiguity) touches `_handle_clarify` in
  conversation.py.
- P1-D (VALIDATE gate amendment) touches `_handle_validate` in conversation.py.
- P1-C and P1-D are in different state handlers and can technically be done in
  parallel by two agents if they operate on non-overlapping line ranges and are
  merged cleanly. If in doubt, serialize P1-C → P1-D.

**executor.py** — the workflow executor. P2 and the P3 rewire both touch this file.
P2 (ephemeral path, WorkflowExecutor constructor) and P3-R (retire regex heuristic)
MUST be serialized: P3-R depends on P2 being in place (the regex guard can only be
retired after the schema-driven path handles all ask_parameterized invocations).

**Serialization order:**

```
Phase 1 (parallel-safe group):
  P1-A  — prompt bump: capture_intent (YAML only)
  P1-B  — prompt bump: design_skill (YAML only)
  P2-Infra  — confluence adapter factory relocation + mcp_server lifespan wiring

Phase 2 (after Phase 1 complete):
  P1-C  — conversation.py: CLARIFY source_binding surface
  P1-D  — conversation.py: VALIDATE gate amendment
  P2-Exec   — executor.py: ephemeral path + WorkflowExecutor constructor

Phase 3 (after Phase 2 complete):
  P3-R  — executor.py: retire regex heuristic (depends on P2-Exec landed)
  P1-E  — skill YAML: re-author four affected TPM skills (depends on P1-B)
  P2-API    — openapi.yaml: source_fetched_on_demand response field

Phase 4 (after Phase 3 complete):
  Tests-P1  — conversation.py unit tests for new CLARIFY/VALIDATE/DESIGN_SKILL behavior
  Tests-P2  — executor.py + mcp_server unit tests for ephemeral path
  Tests-P3R — executor.py unit tests confirming regex guard removal
```

---

## Task Table

| Task | Files | Parallel-safe? | Depends on | Test that ships | Acceptance check |
|---|---|---|---|---|---|
| **P1-A** capture_intent prompt v1.1 | `framework/config/prompts/skill_builder.yaml` (capture_intent entry), `framework/tests/fixtures/prompts/capture_intent_v1_1/` | YES — no .py change | Nothing | 2 new prompt fixtures: one intent with "for a given page" → source_binding_mode=ask_parameterized; one fixed-source intent → author_fixed | `prompt_lab.py --prompt capture_intent --version 1.1 --fixture ask_parameterized` passes; `--fixture author_fixed` passes |
| **P1-B** design_skill prompt v1.1 | `framework/config/prompts/skill_builder.yaml` (design_skill entry), `framework/tests/fixtures/prompts/design_skill_v1_1/` | YES — no .py change | Nothing | 2 new prompt fixtures: ask-parameterized intent → source_binding_mode=ask_parameterized in output; fixed-source → author_fixed | `prompt_lab.py --prompt design_skill --version 1.1` passes both fixtures; max_tokens 8192 unchanged |
| **P2-Infra** adapter factory relocation + lifespan wiring | NEW `framework/adapters/confluence/factory.py` (extracts `_build_confluence_adapter` from conversation.py), `framework/deploy/mcp_server.py` (lifespan: optional Confluence adapter init), `framework/skill_builder/conversation.py` (call site updated to use factory.py) | YES (new file + isolated lifespan block) | Nothing | `test_mcp_server_lifespan_confluence.py` — mock adapter factory, verify adapter is None when no skill requires it; verify adapter is initialized when a skill with ingest_on_demand:true is present; verify server starts cleanly without adapter | Server starts without Confluence config; server starts with emcp_direct config + mock keychain; mock ask-parameterized skill triggers adapter init |
| **P1-C** CLARIFY source_binding surface | `framework/skill_builder/conversation.py` — `_handle_clarify` + `_handle_capture_intent` (read source_binding_mode from capture_intent output; push to blocking_ambiguities if ask_parameterized or ambiguous) | NO — conversation.py serial | P1-A complete | `test_skill_builder_conversation.py` — 3 new tests: (1) intent with "for a given page" → source_binding_mode extracted → CLARIFY triggered; (2) CLARIFY response "B" → mode set to ask_parameterized in session data; (3) CLARIFY response "A" → mode set to author_fixed; skip CLARIFY | All 3 pass; existing capture_intent tests still pass (no regression) |
| **P1-D** VALIDATE gate amendment | `framework/skill_builder/conversation.py` — `_handle_validate` (add confluence adapter availability check for ask_parameterized + ingest_on_demand:true skills) | NO — conversation.py serial; run after P1-C | P1-C complete | `test_skill_builder_conversation.py` — 2 new tests: VALIDATE with ask_parameterized + ingest_on_demand:true + no adapter → ValidationError with actionable message; VALIDATE with author_fixed → no new check | Both pass; existing VALIDATE tests unaffected |
| **P1 Synthesizer Gap** (CLOSED 2026-05-16) | `framework/skill_builder/synthesize_workflow.py` — add `derive_space_allow_list()` + 5 new params to `synthesize_workflow_skill()`; `framework/skill_builder/conversation.py` `_synthesize_preview` — pass `source_binding_mode` + derived `space_allow_list` | N/A — gap fix | P1-C, P1-D complete | `test_synthesize_workflow_skillcard.py` — 30 new tests: `TestAskParameterizedSourceBinding` (14), `TestAskParameterizedPassesValidateContract` (4), `TestDeriveSpaceAllowList` (12) | Freshly synthesized ask_parameterized YAML passes `_validate_source_binding_contract` (end-to-end regression test); author_fixed output unchanged |
| **P2-Exec** WorkflowExecutor ephemeral path | `framework/workflow_runtime/executor.py` — constructor signature (`confluence_adapter=None`), new `_retrieve_ask_parameterized()` private method, new `_EphemeralCache` class (module level, thread-safe), `_log_ephemeral_fetch()`, space allow-list enforcement, `ConfluencePageNotInKBError` (already exists — extend with `reason` param), `_resolve_page_id()` helper (handles numeric + URL forms) | NO — executor.py serial | P2-Infra complete | `framework/tests/unit/test_executor_ephemeral.py` — 15+ tests: (1) ask_parameterized skill + adapter present → ephemeral fetch called, WikiMetadataStore.add NOT called; (2) space not in allow-list → ConfluencePageNotInKBError with allow-list reason; (3) cache hit → adapter.fetch NOT called second time; (4) adapter None + ingest_on_demand:true → hard-fail; (5) author_fixed skill → ephemeral path NOT entered; (6) TTL expiry → cache evicted, adapter.fetch called again; (7) audit log written | All 15 pass; WikiMetadataStore mock confirms no `add()` call in ephemeral path |
| **P3-R** Retire regex heuristic | `framework/workflow_runtime/executor.py` — remove `_extract_confluence_page_ids`, `_CONFLUENCE_PAGE_REF_PATTERNS`, and the P3 guard block from `_retrieve_for_inputs`; remove the TEMPORARY comment from the module docstring | NO — executor.py serial | P2-Exec complete | Update `framework/tests/unit/test_executor_source_guard.py` — the 19 existing tests remain but are reframed: the heuristic-based tests are replaced by schema-field-based equivalents; regression: author_fixed skills still have no guard (no regression) | All existing test_executor_source_guard assertions pass against schema-driven path; no import of `_extract_confluence_page_ids` anywhere in the codebase |
| **P1-E** Re-author four affected TPM skills | `framework/workflow_skills/tpm/project_tracking_confluence_stakeholder_status_meeting_email.yaml` + three sibling files: add `source_binding` block + change `trigger.on_request.inputs[0]` from `{name:input, type:string}` to `{name:page_id, type:confluence_page_ref, required:true}` | YES (YAML files; no .py) | P1-B complete (so DESIGN_SKILL knows to emit ask_parameterized) | No new tests; manual smoke: `kb-cli workflow-run tpm.project_tracking_confluence_stakeholder_status_meeting_email --inputs '{"page_id":"18625350641"}'` triggers ephemeral fetch (or hard-fail with adapter-missing message in laptop mode without Keychain) | Each of the four skill YAMLs has `source_binding.mode: ask_parameterized`, `input_param: page_id`, `ingest_on_demand: true`, `space_allow_list: [OCIFACP]` (corrected from [FA, PROJ] by commit 9b6cc1f); old `{name:input, type:string}` input removed |
| **P2-API** openapi.yaml source_fetched_on_demand field | `framework/deploy/openapi.yaml` — add `source_fetched_on_demand: boolean`, `source_fetched_page_id: string`, `latency_note: string` to the `AskResponse` schema | YES (YAML only; no .py) | P2-Exec complete | No new tests (spec change only); existing ask response tests verify field is absent when not an ephemeral fetch | OpenAPI spec validates; field present in response when ephemeral fetch occurred; field absent otherwise |
| **Tests-P1** conversation.py unit tests (consolidated) | `framework/tests/unit/test_skill_builder_conversation.py` — verify the complete P1 happy path end-to-end: intent → capture_intent → source_binding_mode=ask_parameterized → CLARIFY → user confirms B → DESIGN_SKILL → typed page_id input in output → CONFIGURE_TRIGGERS → VALIDATE (with adapter availability check) | NO (after P1-C + P1-D both complete) | P1-C, P1-D | 5 integration-style tests covering the full P1 state sequence | All 5 pass; no regression on existing 630+ tests |
| **Tests-P2** mcp_server ephemeral path integration | `framework/tests/unit/test_executor_ephemeral.py` (extended) + `framework/tests/unit/test_mcp_server_lifespan_confluence.py` | NO (after P2-Exec + P2-Infra) | P2-Exec, P2-Infra | See P2-Exec task above (15+ tests already specified) | All pass; WikiMetadataStore never receives `add()` call in ephemeral path |

---

## Agent Fan-Out Recommendation

Given the serial constraints above, the recommended fan-out is:

**Three parallel agents in Phase 1 (no conflicts):**
- Agent A: P1-A (capture_intent prompt) + P1-B (design_skill prompt)
- Agent B: P2-Infra (adapter factory relocation + mcp_server lifespan)
- Agent C: P1-E (re-author four TPM skill YAMLs) — NOTE: P1-E can actually
  start immediately since the YAML schema shape is already fully specified in
  ADR-032 §D.1. The skills can be updated ahead of the conversation.py changes.

**Two sequential agents in Phase 2 (conversation.py serial file):**
- Agent D (serial): P1-C → P1-D (conversation.py, two state handlers, same file)
  This MUST be a single agent doing both tasks sequentially, or two agents
  with a hard merge gate between P1-C and P1-D.

**One agent in Phase 2 (executor.py, independent of conversation.py):**
- Agent E: P2-Exec (executor.py ephemeral path)
  Can run in parallel with Agent D — different files.

**Sequential in Phase 3 (executor.py serial):**
- Agent F: P3-R (retire regex heuristic) — MUST wait for P2-Exec to land.
  Single-agent task. The test suite for test_executor_source_guard.py must be
  updated in the same commit as the removal.

**Parallel in Phase 3 (independent files):**
- Agent G: P2-API (openapi.yaml) — can run in parallel with Agent F.

**Phase 4 (consolidation):**
- Agent H: Tests-P1 (after P1-C + P1-D both merged)
- Agent I: Tests-P2 (after P2-Exec + P2-Infra both merged)

**Recommended serialization graph:**

```
Phase 1 (parallel):
  A: P1-A, P1-B
  B: P2-Infra
  C: P1-E

Phase 2 (after Phase 1 merged):
  D (serial): P1-C → P1-D
  E: P2-Exec

Phase 3 (after Phase 2 merged):
  F: P3-R       ← must follow P2-Exec
  G: P2-API     ← can run parallel with F

Phase 4 (after Phase 3 merged):
  H: Tests-P1
  I: Tests-P2
```

Total estimated dev time (excluding tests): 4-5 days.
Parallelized across 3 agents in Phase 1: reduces wall-clock to 2-3 days.

---

## Detailed Task Specs

### P1-A — capture_intent prompt version 1.1

File: `framework/config/prompts/skill_builder.yaml`

Entry `capture_intent`, version `"1.0"` → `"1.1"`.

Add to the `template` (in the output schema block, after `"nice_to_know_ambiguities"`):

```yaml
        "source_binding_mode": "author_fixed | ask_parameterized | ambiguous",
        "source_binding_signal": "one-line evidence from the intent text (< 80 chars, or empty string)"
```

Add to the Rules section of the template:

```
- "source_binding_mode": emit "ask_parameterized" if the intent implies the consumer
  will supply the source page at query time — look for phrases like "for a given page",
  "based on the page the user provides", "accept a Confluence URL", "for any project
  tracking page", "whichever page the user passes". Emit "author_fixed" if specific
  page IDs or URLs are identified in the intent (user is specifying which pages are
  always used). Emit "ambiguous" if unclear.
- "source_binding_signal": a verbatim quote or paraphrase (< 80 chars) from the
  intent that drove the source_binding_mode classification. Empty string if
  mode is "author_fixed" and there is no relevant signal.
```

If `source_binding_mode` is `"ask_parameterized"` or `"ambiguous"`, add a entry to
`blocking_ambiguities`: "Is the source page fixed at authoring time or supplied by
the consumer at query time?" This triggers CLARIFY before CONFIGURE_SOURCES.

New fixtures required (in `framework/tests/fixtures/prompts/capture_intent_v1_1/`):
- `ask_parameterized.json` — intent: "accept a Confluence page and draft an email
  from it" → expected output has `source_binding_mode: "ask_parameterized"`,
  `source_binding_signal: "accept a Confluence page"`, blocking_ambiguities non-empty
- `author_fixed.json` — intent: "read project page 20030556732 and draft weekly email"
  → expected output has `source_binding_mode: "author_fixed"`, blocking_ambiguities empty

Checksum: update `prompt_lab.py` fixture checksums after authoring.

### P1-B — design_skill prompt version 1.1

File: `framework/config/prompts/skill_builder.yaml`

Entry `design_skill`, version `"1.0"` → `"1.1"`. Current `max_tokens: 8192` is
unchanged — the new field adds approximately 120 tokens to the response; headroom
is sufficient.

Add to the JSON output schema block (after `"open_questions"`):

```yaml
        "source_binding_mode": "author_fixed | ask_parameterized"
```

Add to the Rules section:

```
- "source_binding_mode": emit "ask_parameterized" if the source capability inventory
  was collected via dynamic source discovery (user will supply page at query time) or
  if the normalised intent implies dynamic source supply. Emit "author_fixed" if specific
  page IDs from INSPECT_SOURCES are already in the source_bindings. When "ask_parameterized",
  do NOT include page IDs in source_bindings — the source is dynamic, not fixed.
```

New fixtures required (in `framework/tests/fixtures/prompts/design_skill_v1_1/`):
- `ask_parameterized.json` — capability inventory for dynamic source → output has
  `source_binding_mode: "ask_parameterized"`, no page IDs in source_bindings
- `author_fixed.json` — capability inventory with specific pages → `author_fixed`,
  page IDs in source_bindings

### P2-Infra — Confluence adapter factory relocation + lifespan wiring

New file: `framework/adapters/confluence/factory.py`

Extract `_build_confluence_adapter(kbf_env, repo_root)` verbatim from
`framework/skill_builder/conversation.py` into this new file. Export it as
`build_confluence_adapter(kbf_env, repo_root)` (drop the leading underscore — it
is now a public shared utility).

Update `framework/skill_builder/conversation.py` line ~49 to import from the new
location:
```python
from ..adapters.confluence.factory import build_confluence_adapter as _build_confluence_adapter
```
(Keep the private alias locally for backward compat with existing callers.)

Add to `framework/deploy/mcp_server.py` lifespan, after the `workflow_executor`
initialization block (~line 267):

```python
# ADR-032 P2: Optional Confluence adapter for ask_parameterized skill ephemeral fetch.
# Graceful: if unavailable, ask_parameterized skills hard-fail with actionable message.
from ..adapters.confluence.factory import build_confluence_adapter as _build_confluence_adapter
from ..workflow_runtime.executor import _any_promoted_skill_requires_ephemeral

confluence_adapter = None
if _any_promoted_skill_requires_ephemeral(WORKFLOW_SKILLS_DIR):
    confluence_adapter = _build_confluence_adapter(kbf_env, REPO_ROOT)
    if confluence_adapter is None:
        log.warning(
            "ADR-032: ask_parameterized skills with ingest_on_demand:true present "
            "but no Confluence adapter configured — those skills will hard-fail at "
            "consumption time with an actionable message (never silent)."
        )
    else:
        log.info("ADR-032: Confluence adapter initialized for ephemeral fetch: %s",
                 confluence_adapter.mode if hasattr(confluence_adapter, "mode") else "unknown")

state["workflow_executor"] = WorkflowExecutor(
    store=None,
    llm=state["llm"],
    retrievers=retrievers,
    shim_kb=state["shim_kb"],
    confluence_adapter=confluence_adapter,   # NEW param; None = graceful no-op
)
app.state.workflow_executor = state["workflow_executor"]
```

Add `_any_promoted_skill_requires_ephemeral(workflow_skills_dir)` to executor.py:

```python
def _any_promoted_skill_requires_ephemeral(workflow_skills_dir: Path) -> bool:
    """Return True if any skill YAML in the directory has source_binding.ingest_on_demand:true."""
    for skill_path in Path(workflow_skills_dir).rglob("*.yaml"):
        try:
            cfg = yaml.safe_load(skill_path.read_text()) or {}
            sb = cfg.get("source_binding") or {}
            if sb.get("mode") == "ask_parameterized" and sb.get("ingest_on_demand", False):
                return True
        except Exception:
            continue
    return False
```

### P1-C — CLARIFY source_binding surface in conversation.py

Files: `framework/skill_builder/conversation.py`

In `_handle_capture_intent`: after the capture_intent LLM call, read
`normalised_intent.get("source_binding_mode", "author_fixed")`. If it is
`"ask_parameterized"` or `"ambiguous"`, append to `session.data["blocking_ambiguities"]`:

```python
{
    "question": (
        "You described a source that the user will supply at query time. "
        "Should this skill: (A) always extract from specific pages configured "
        "now — the same page every time, or (B) extract from whichever page "
        "the user passes at query time — a different page per invocation?"
    ),
    "context": "source_binding_mode",
    "options": {"A": "author_fixed", "B": "ask_parameterized"},
}
```

Store `source_binding_mode` in `session.data["normalised_intent"]` so downstream
states can read it.

In `_handle_clarify`: extend the CLARIFY handler to recognise the
`context: "source_binding_mode"` question. When the user responds:
- "A" or "fixed" or "author_fixed" → set `session.data["source_binding_mode"] = "author_fixed"`
- "B" or "parameterized" or "ask_parameterized" → set `session.data["source_binding_mode"] = "ask_parameterized"`

After resolving, route to CONFIGURE_SOURCES as usual.

### P1-D — VALIDATE gate amendment in conversation.py

Files: `framework/skill_builder/conversation.py`

In `_handle_validate`: add after the existing `validate_workflow_links()` call:

```python
# ADR-032 P1: Check Confluence adapter availability for ask_parameterized skills
sb = synthesized_yaml.get("source_binding") or {}
if sb.get("mode") == "ask_parameterized" and sb.get("ingest_on_demand", False):
    target_env = session.data.get("target_env", kbf_env)
    adapter_ok = _check_confluence_adapter_available(target_env, repo_root)
    if not adapter_ok:
        raise ValidationError(
            f"This skill requires live Confluence access at consumption time "
            f"(source_binding.ingest_on_demand: true). The target deployment "
            f"environment '{target_env}' has no Confluence adapter configured. "
            f"Configure a Confluence adapter in framework/config/adapters/confluence.yaml "
            f"for that environment, or set ingest_on_demand: false."
        )
```

`_check_confluence_adapter_available(env, repo_root)` reads the adapter config for
the given env and returns True if a non-empty `mode` is configured. It does NOT
make a live Confluence HTTP call — this is a config check only.

### P2-Exec — WorkflowExecutor ephemeral path

Files: `framework/workflow_runtime/executor.py`

**Constructor change** (line ~127):
```python
def __init__(self, store=None, llm=None, retrievers=None, shim_kb=None,
             confluence_adapter=None):
    ...
    self.confluence_adapter = confluence_adapter
```

**New module-level `_EphemeralCache` class** (before `WorkflowExecutor`):

```python
import threading
import time

class _EphemeralCache:
    """In-process TTL cache for ephemeral Confluence page fetches.

    Thread-safe. Never persisted to disk. Process-local.
    ADR-032 §E.5.
    """
    _MAX_SIZE = 50  # LRU eviction at this cap

    def __init__(self):
        self._lock = threading.Lock()
        self._store: dict[str, tuple[object, float, int]] = {}
        # key → (value, fetched_at, ttl_seconds)

    def get(self, key: str, ttl: int) -> object | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, fetched_at, stored_ttl = entry
            effective_ttl = min(ttl, stored_ttl)
            if time.time() - fetched_at > effective_ttl:
                del self._store[key]
                return None
            return value

    def put(self, key: str, value: object, ttl: int) -> None:
        with self._lock:
            if len(self._store) >= self._MAX_SIZE:
                # LRU: remove the oldest entry
                oldest_key = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest_key]
            self._store[key] = (value, time.time(), ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_ephemeral_cache = _EphemeralCache()
```

**`_resolve_page_id(page_ref)` helper:**

```python
def _resolve_page_id(page_ref: str) -> str:
    """Extract a numeric Confluence page ID from a pageId= URL param or bare digits.

    Handles: numeric IDs, full Confluence URLs with pageId= param,
    /wiki/spaces/.../pages/<id>/ paths.
    Returns the numeric string or the original string if no pattern matches.
    """
    for pattern in _CONFLUENCE_PAGE_REF_PATTERNS:
        m = pattern.search(page_ref)
        if m:
            return m.group(1)
    # bare numeric string
    if page_ref.strip().isdigit():
        return page_ref.strip()
    return page_ref
```

Note: `_CONFLUENCE_PAGE_REF_PATTERNS` is already defined (from P3). The P2-Exec
task reuses it here; P3-R removes it after the schema-driven path covers all cases.

**New private method `_retrieve_ask_parameterized(cfg, inputs)`:**

See ADR-032 §E.2 for the full pseudocode. The key invariants to implement:

1. Read `source_binding.input_param` → get page_ref from `inputs[input_param]`.
2. If `ingest_on_demand: false` or `self.confluence_adapter is None` → raise
   `ConfluencePageNotInKBError` with adapter-unavailable reason.
3. Extract space_key from page_ref (from URL path `/spaces/{key}/` or from Confluence
   API metadata — if not extractable from URL, proceed without space check and
   log a warning).
4. Check `space_allow_list` — if non-empty and page's space_key not in list → raise
   `ConfluencePageNotInKBError` with allow-list reason.
5. Check `_ephemeral_cache.get(cache_key, ttl)` — return cached passages if hit.
6. Call `self.confluence_adapter.fetch(RawItemRef(..., source_id=page_id))`.
7. Extract body text (using existing `body.storage.value` chain from emcp_direct shape).
8. Call `self._llm_extract_fields(body_text, skill_schema)` — the same method
   already used in the INGEST path.
9. Build passages list with `"ephemeral": True` in metadata.
10. Call `self._log_ephemeral_fetch(page_id, space_key, skill_name)`.
11. Cache result. Return passages.

**`_log_ephemeral_fetch` method:**

Append to `~/.kbf/telemetry/ephemeral_fetch.jsonl`:
```json
{"ts":"...","page_id":"...","space_key":"...","skill_name":"...","consumer_id":"...","content_hash":"..."}
```
`consumer_id` is passed as a new optional param to `execute()` — default `None`.

**Integration point in `_retrieve_for_inputs`:**

Replace the current opening of `_retrieve_for_inputs` with:

```python
def _retrieve_for_inputs(self, cfg, inputs, sources):
    source_binding = cfg.get("source_binding") or {}
    sb_mode = source_binding.get("mode", "author_fixed")

    if sb_mode == "ask_parameterized":
        return self._retrieve_ask_parameterized(cfg, inputs)

    # author_fixed path: existing behavior below (unchanged)
    ...
```

### P3-R — Retire regex heuristic

Files: `framework/workflow_runtime/executor.py`

Remove:
- `_CONFLUENCE_PAGE_REF_PATTERNS` list (lines 41-51)
- `_extract_confluence_page_ids()` function (lines 73-97)
- The P3 guard block at the end of `_retrieve_for_inputs` (lines 334-368)
- The TEMPORARY comment in the module docstring

Keep:
- `ConfluencePageNotInKBError` class — used by the P2 ephemeral path

Update `framework/tests/unit/test_executor_source_guard.py`: the 19 existing tests
are retained but reframed. Tests that verified the regex heuristic (e.g.,
"pageId=123 in free-text input → guard fires") are replaced by schema-field-based
equivalents (e.g., "ask_parameterized skill with page_id input → ephemeral path
fires"). The regression tests ("no page ref in input → guard inert") become
"author_fixed skill → ephemeral path not entered."

All 19 test ASSERTIONS (correct-page-fails, wrong-page-hard-fails, no-page-ref-inert)
must have a schema-driven equivalent test. Do not reduce test coverage.

### P1-E — Re-author four affected TPM skills

Files:
- `framework/workflow_skills/tpm/project_tracking_confluence_stakeholder_status_meeting_email.yaml`
- `framework/workflow_skills/tpm/project_tracking_stakeholder_status_email.yaml`
- `framework/workflow_skills/tpm/project_tracking_stakeholder_tracking_meeting_email.yaml`
- `framework/workflow_skills/tpm/project_tracking_weekly_stakeholder_status_email.yaml`

Each skill YAML gets:

1. New top-level `source_binding` block:
```yaml
source_binding:
  mode: ask_parameterized
  input_param: page_id
  ingest_on_demand: true
  source_type: confluence_page
  space_allow_list:
    - FA
    - PROJ
  ephemeral_ttl_seconds: 300
```

2. Replace `trigger.on_request.inputs[0]` (currently `{name:input, type:string,
   description: "Query or filter input"}`) with:
```yaml
    inputs:
      - name: page_id
        type: confluence_page_ref
        description: "Confluence pageId or full page URL of the project tracking page to use"
        required: true
```

The existing `requires_extractions` section is unchanged — the executor uses the
schema in the skill YAML for ephemeral extraction, and the KB card name remains
for reference/metadata purposes.

### P2-API — openapi.yaml response field addition

File: `framework/deploy/openapi.yaml`

In the `AskResponse` schema, add three optional fields:

```yaml
source_fetched_on_demand:
  type: boolean
  description: "True if a Confluence page was fetched ephemerally for this request (ADR-032 P2)."
source_fetched_page_id:
  type: string
  description: "The Confluence page ID that was fetched on demand (present only if source_fetched_on_demand is true)."
latency_note:
  type: string
  description: "Human-readable note about on-demand fetch latency (present only if source_fetched_on_demand is true)."
```

These fields must be populated in `framework/deploy/routes/ask.py` (the ask route
response builder) when `WorkflowExecutor` sets a flag indicating an ephemeral fetch
occurred. The flag can be returned in the executor's result dict:
`"source_fetched_on_demand": True, "source_fetched_page_id": page_id`.

---

## Known Gaps and Deferreds

**Space key extraction from page_ref:** if the consumer supplies a bare numeric
page ID (not a URL), the space_key cannot be determined without an API call. In
v1, when the space cannot be determined from the URL and `space_allow_list` is
non-empty, the executor calls `confluence_adapter.fetch()` to get metadata first,
then checks the space key from the metadata before proceeding to the full extraction.
This adds one extra API call on the first fetch (before the TTL cache is warm).
If the space check fails, `ConfluencePageNotInKBError` is raised before any
extraction runs. This is safe — the extra fetch is metadata-only, no extraction
has occurred.

**Per-consumer OAuth (full Confluence ACL):** explicitly deferred. The ADR-020
emcp_direct / codex_proxy OAuth architecture supports it. Tracking: file a new
DECISION when the user wants to require it.

**Webhook-triggered re-fetch for updated ephemeral pages:** out of scope. The TTL
cache provides freshness within the TTL window. For longer-lived freshness on a
specific page, the author should use `author_fixed` mode and set up a webhook
trigger for re-ingestion.

**Multi-page ask_parameterized skills:** v1 supports only one `input_param` per
skill. Skills that require two consumer-supplied pages are `author_fixed` with the
second page ingested at author time. This is a known limitation; file a DECISION
if a use case arises.

---

## Acceptance Criteria (all tasks complete when)

1. `pytest framework/tests/unit/` — 0 failures (baseline 630+ tests unaffected).
2. `prompt_lab.py --prompt capture_intent --version 1.1` passes all fixtures.
3. `prompt_lab.py --prompt design_skill --version 1.1` passes all fixtures.
4. `framework/tests/unit/test_executor_ephemeral.py` — 15+ tests, all pass.
5. `framework/tests/unit/test_executor_source_guard.py` — 19+ tests, all pass
   (reframed to schema-driven, no reduction in assertion count).
6. `framework/tests/unit/test_mcp_server_lifespan_confluence.py` — all pass.
7. `framework/tests/unit/test_skill_builder_conversation.py` — all existing + 10
   new P1 tests pass.
8. `grep -r "_extract_confluence_page_ids\|_CONFLUENCE_PAGE_REF_PATTERNS" framework/`
   returns nothing (regex heuristic fully retired).
9. `grep "source_binding" framework/workflow_skills/tpm/project_tracking_*.yaml`
   returns `mode: ask_parameterized` for all four affected skills.
10. `grep "WikiMetadataStore\|wiki_store.add" framework/workflow_runtime/executor.py`
    does NOT appear in any code path reachable from `_retrieve_ask_parameterized`
    (confirm ephemeral path never writes to persistent store).

---

## References

- [ADR-032 — Accepted design](ADR-032-ask-time-source-ingestion.md)
- [ADR-028/029 — impl-plan.md](ADR-028-029-impl-plan.md) — reference for how serial + parallel streams were partitioned last time
- [ADR-030 — impl-plan.md](ADR-030-impl-plan.md) — reference for how prompt version bumps + fixture gates work
- Commit 8c947dc — P3 guard (shipped; P3-R retires it)
- `framework/workflow_runtime/executor.py` — P3 guard site + P2 target
- `framework/deploy/mcp_server.py` — lifespan wiring target
- `framework/skill_builder/conversation.py` — P1-C/P1-D/P1-E target states
- `framework/config/prompts/skill_builder.yaml` — P1-A/P1-B prompt bump target
- `framework/adapters/confluence/factory.py` — NEW file (P2-Infra)
