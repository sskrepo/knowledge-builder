---
title: ADR-032 — Ask-time / Runtime Source Ingestion as a First-Class Skill Capability
status: accepted
created: 2026-05-16
accepted: 2026-05-16
owner: architect
deciders: user, tpm
tags: [adr, skill-builder, consumption, ingestion, workflow-skills, adr-016, adr-029, adr-031]
related: [ADR-015, ADR-016, ADR-027, ADR-029, ADR-030, ADR-031]
supersedes: ~
---

# ADR-032 — Ask-time / Runtime Source Ingestion as a First-Class Skill Capability

## Status

**Accepted — 2026-05-16.** DECISION-012 resolved: Option C (ephemeral request-scoped
ingestion) chosen by user. Implementation blueprint at ADR-032-impl-plan.md.

P3 (silent wrong-page substitution guard) shipped standalone in commit 8c947dc,
ahead of the P1/P2 build.

---

## A. The Observed Failure

A TPM user authored an email-draft workflow skill with the explicit authoring intent:
"This skill should accept a Confluence page and then draft [an email] based on some
requirement provided during authoring." The intent is unambiguous: the skill is
**source-parameterized at ask time** — the consumer passes a Confluence page (URL or
pageId) at consumption time, and the skill should ingest and use *that* page.

At consumption (`askKnowledgeBase`, `POST /api/v1/ask`), the user passed
`pageId=18625350641`. That page exists in Confluence but had never been ingested into
the KB. Instead of failing with an actionable message, the skill silently drew from
pageId=20030556732 (the "FA DB Upgrade 19c→26ai" project-plan page — the only
project-plan page in the KB at the time) and produced a plausible-looking but entirely
wrong email draft. A follow-up `askKnowledgeBase` for pageId=18625350641 returned
tier-4 "no relevant context found."

This failure has three separable root causes.

---

## B. Three Root Causes

### P1 — Design-Contract Gap (author time)

Nothing in `CAPTURE_INTENT`, `CLARIFY`, `DESIGN_SKILL`, or the ADR-016 workflow YAML
schema captures whether a skill's source is:

- **(a) Fixed at author time** — the source page(s) are ingested once during
  `authorSkill → INGEST`; every `askKnowledgeBase` invocation retrieves from the
  same already-ingested content. This is the *only* model the framework supports today.
- **(b) Parameterized at ask time** — the *consumer* supplies the source page at
  invocation; the workflow must obtain and use *that* page on demand.

The user's intent clearly implied model (b). The framework has no mechanism to detect
or represent this intent — it silently collapsed (b) to (a) at authoring time, with
no warning that the authored skill would ignore the user-supplied pageId at consumption.

**Evidence from the four TPM email-draft skills:**

All four skills in `framework/workflow_skills/tpm/` that implement variants of this
intent share a common structural problem: their `requires_extractions` section points
to a KB name (`tpm.project_tracking_*`) that was populated from a *fixed* page at
`authorSkill → INGEST` time. The `trigger.on_request.inputs` schema accepts only a
single `input: string` (free text query). There is no `page_id` or `confluence_url`
typed input. There is no `source_binding` field in the YAML schema. None of the four
skills has any mechanism to communicate to the executor which specific Confluence page
to draw from.

The four skills involved:

| Skill name | Output format | KB reference |
|---|---|---|
| `project_tracking_confluence_stakeholder_status_meeting_email` | email | `tpm.project_tracking_confluence_stakeholder_status_meeting_email` |
| `project_tracking_stakeholder_status_email` | email | `tpm.project_tracking_stakeholder_status_email` |
| `project_tracking_stakeholder_tracking_meeting_email` | markdown | `tpm.project_tracking_stakeholder_tracking_meeting_email` |
| `project_tracking_weekly_stakeholder_status_email` | eml | `tpm.project_tracking_weekly_stakeholder_status_email` |

### P2 — Runtime-Capability Gap (consumption time)

The consumption path has no ingest-on-demand step. The full call chain is:

```
POST /api/v1/ask
  → ask.py:ask_knowledge_base()
  → ctx.answer()                         [context_builder.py:145]
  → _dispatch_tier1()                    [context_builder.py:286]
  → skill(query, intent_signal, budget)  [_base.py:114]
  → _match_workflow_skill()              [_base.py:347]
  → _invoke_workflow()                   [_base.py:398]
  → workflow_executor.execute()          [executor.py:43]
  → _resolve_sources()                   [executor.py:143]    ← stub, returns []
  → _retrieve_for_inputs()               [executor.py:150]    ← uses shim_kb + retrievers
  → _synthesize()                        [executor.py:309]
```

There is no ingest-on-demand step anywhere in this chain. The `ConfluenceWikiIngestor`
and Confluence adapters exist in the ingestion pipeline but are not imported or
reachable from `WorkflowExecutor._retrieve_for_inputs`.

### P3 — Silent Wrong-Page Substitution (shipped — commit 8c947dc; space-form gap closed — BUG-queue-990fe Option-A)

When path 1 of `_retrieve_for_inputs` runs with a user-supplied pageId that is not
in the KB, the retriever runs a semantic similarity search over *all* ingested pages
and returns the highest-scoring result. The skill then synthesizes from that content.

**This is fixed.** Commit 8c947dc added `ConfluencePageNotInKBError` and a guard block
in `_retrieve_for_inputs` that detects Confluence page references in inputs via regex
heuristic, verifies at least one retrieved passage cites the requested page_id, and
hard-fails with an actionable message on mismatch. This guard is inert for fixed-source
skills.

**BUG-queue-990fe RC2 (Option-A, A1) — space-form gap closed.** The original pattern
list only matched URL forms and `pageId=<digits>` (with `=`). The natural-language form
`"pageId 18625350641"` (space-separated) was not matched, so the P3 guard silently
failed to fire — producing the highest-severity outcome (wrong-page email drafted with
no signal). A fifth pattern has been added to `_CONFLUENCE_PAGE_REF_PATTERNS`:

```python
re.compile(r"(?i)\bpage[\s_-]?id\b[\s:]+(\d{8,})")
```

Length constraint `{8,}` prevents false-positives on short prose numbers; Confluence
pageIds in this environment are ~11 digits. The guard now fires on:
`"for Confluence pageId 18625350641"` and `"pageId: 18625350641"` — and hard-fails
(ConfluencePageNotInKBError) identically to the URL/`=` forms.

**BUG-queue-990fe RC1 (Option-A, A2/A3) — persona=null root cause closed.** Raw
Confluence items carry no `persona` field. Previously `_raw.get("persona")` stored
`null` in wiki_metadata, causing `SearchWikiRetriever`'s persona filter to exclude
the ingested page at retrieval time. Fixed by:
- `ConfluenceWikiIngestor.__init__` gains `persona: str | None = None` param.
- `ingest_page` uses `effective_persona = _raw.get("persona") or self._persona`
  (raw wins; ingestor-level param is the fallback — never overwrites raw).
- All callers updated: `conversation.py` passes `self._data.persona`;
  `ingestion_worker.py` builds a per-entry ingestor with `persona=entry["persona"]`;
  `kb-cli ingest` gains `--persona` flag (required when config YAML has no `persona:`).

**A4 — idempotent backfill command.** `kb-cli wiki-meta backfill-persona --persona <p>
[--page-id N]` sets persona on null-persona wiki_metadata records. Non-null records
are never overwritten. Re-running is a no-op. Executed for page 18625350641:
before `persona: null`, after `persona: "tpm"`.

The regex heuristic is **temporary** for all five patterns. It will be replaced in
its entirety by the schema-driven check against `source_binding.input_param` when
ADR-032 P2-Exec (Phase 2) ships. See §E.4 for the retirement plan. Once P2-Exec
lands, the regex block and the P3 guard block in `_retrieve_for_inputs` are removed.

---

## C. Decision: Option C — Request-Scoped Ephemeral Ingestion

### What was decided

DECISION-012 resolved: **Option C** chosen by user on 2026-05-16.

When the executor detects an `ask_parameterized` skill with a consumer-supplied page
that is not in the KB:

1. Check `source_binding.ingest_on_demand == true` and `space_allow_list` — reject
   with actionable error if the page's space is not allow-listed.
2. Fetch the page content via the Confluence adapter (emcp_direct in laptop mode,
   native/mcp in production).
3. Run `_llm_extract_fields` with the skill's authored schema against the fetched
   content.
4. Return the extracted passages as ephemeral results, with the real Confluence URL
   as citation.
5. **Do not write anything to WikiMetadataStore, the incident vector store, or any
   other persistent KB.** The content is discarded at end-of-request.
6. A short in-process TTL cache (approximately 300 seconds, keyed by
   `page_id + content_hash`) prevents redundant fetches within a session window.

### Accepted spec §2 caveat

Schema-bounded LLM extraction inside a retrieval request is acceptable ONLY for
`ask_parameterized` skills whose schema was authored, reviewed, and promoted through
the `authorSkill` flow. The skill author's act of promoting the skill with
`ingest_on_demand: true` is the architectural grant that this step may occur at
retrieval time. This is categorically different from unconstrained autonomous LLM
extraction: the schema is fixed and authored before any consumer ever supplies a page.

### Trust boundary (explicit design decision)

The residual trust risk: a consumer with `read` scope could use an ask-parameterized
skill to trigger a live Confluence fetch of any page in the allow-listed spaces,
even pages they cannot access in Confluence directly. The v1 mitigations are:

1. **Author-time grant** — `ingest_on_demand: true` is an explicit author decision.
2. **Space allow-list** — ephemeral fetch is restricted to spaces declared in
   `source_binding.space_allow_list`. The executor checks this before any HTTP call.
3. **RPM rate limiter** — existing `askKnowledgeBase` rate limiter bounds fetch rate.
4. **Audit log** — every ephemeral fetch logged: `{consumer_id, page_id, skill_name,
   skill_persona, timestamp, space_key, content_hash}`.
5. **Graceful degradation** — if the adapter is unavailable, the skill hard-fails
   with an actionable message; it never silently falls back to wrong content.

Full per-consumer Confluence OAuth is v2 / explicitly out-of-scope. The ADR-020
codex_proxy / emcp_direct architecture supports it when/if required.

### Alternatives considered (collapsed)

**Option A (synchronous ingest in ask path, persist to shared KB):** rejected
because (a) the trust boundary is fully implicit — no author grant, any consumer
can trigger ingestion of any reachable page; (b) the shared KB accumulates one-off
pages not useful to any other skill; (c) conflicts with spec §2 ingest/retrieve
separation.

**Option B (ask triggers ingestion pipeline, hard-fail with retry instruction):**
rejected because (a) consumer UX is poor — LLM clients do not auto-retry; (b)
requires a new IPC channel between MCP server and ingestion worker; (c) the trust
boundary problem is deferred to the ingestion worker, not solved.

---

## D. Author-Time Detection (P1) — Concrete Design

### D.1 Workflow YAML schema amendment (ADR-016 amendment)

A new top-level `source_binding` block is added to the workflow skill YAML schema.
All existing skills default to `mode: author_fixed` (absent block = author_fixed).
No behavior change for existing skills.

```yaml
source_binding:
  mode: author_fixed          # author_fixed | ask_parameterized
                              # DEFAULT (absent block): author_fixed
  input_param: page_id        # (ask_parameterized only) name of the
                              # trigger.on_request.inputs entry that carries
                              # the source reference (pageId or Confluence URL).
                              # The executor reads inputs[input_param] to get the
                              # page reference — no regex scanning.
  ingest_on_demand: true      # (ask_parameterized only) when true: executor
                              # attempts ephemeral fetch (Option C) on cache miss.
                              # When false: executor hard-fails with actionable
                              # message on cache miss. Default: false.
  source_type: confluence_page  # confluence_page | confluence_space |
                                # jira_filter | git_ref
  space_allow_list:           # (ask_parameterized + ingest_on_demand only)
    - FA                      # Restrict ephemeral fetch to these Confluence spaces.
    - PROJ                    # The executor extracts space_key from the page ref
                              # and checks it before any HTTP call.
                              # Empty or absent: reject ALL spaces (safest default).
  ephemeral_ttl_seconds: 300  # In-process TTL for the ephemeral content cache.
                              # 0 = no caching. Default: 300.
```

The corresponding `trigger.on_request.inputs` entry for ask_parameterized skills:

```yaml
trigger:
  on_request:
    enabled: true
    inputs:
      - name: page_id
        type: confluence_page_ref   # new semantic type; executor resolves pageId
                                    # from this input, handling both numeric IDs
                                    # and full Confluence URLs.
        description: "Confluence pageId or full page URL of the page to use"
        required: true
      - name: input
        type: string
        description: "Additional context or query modifier (optional)"
        required: false
    output_format: email
    response_mode: artifact_url
```

### D.2 DESIGN_SKILL prompt amendment (ADR-030 YAML store)

File: `framework/config/prompts/skill_builder.yaml`, entry `design_skill`.

Current `max_tokens`: 8192. The new field adds approximately 120 tokens to the
response schema. Headroom is sufficient — no `max_tokens` change required.

The `design_skill` template must emit a new `source_binding_mode` field in its JSON
output. The amendment is additive — the existing output schema gains one new key:

```
"source_binding_mode": "author_fixed" | "ask_parameterized"
```

Evidence trigger: if the source capability inventory contains a source that the
intent implies will be supplied at consumption time (e.g., "for a given page",
"whichever page the user passes", "accept a Confluence URL"), the model MUST emit
`"source_binding_mode": "ask_parameterized"`. Otherwise `"author_fixed"`.

The prompt delta (to be added to the `design_skill` template, in the Rules section):

```
- "source_binding_mode": emit "ask_parameterized" if the intent implies the consumer
  will supply the source page at query time (phrases like "for a given page", "based
  on the page the user provides", "accept a Confluence URL", "for any project tracking
  page", "whichever page"). Emit "author_fixed" if the source is fixed at author time
  (specific page IDs or URLs identified during INSPECT_SOURCES). When "ask_parameterized",
  do NOT include page IDs in the source_bindings — the source is dynamic, not fixed.
```

This is a prompt version bump: `design_skill` version `"1.0"` → `"1.1"`. A new
fixture in `framework/tests/fixtures/prompts/design_skill_v1_1/` is required. The
ADR-030 harness (`prompt_lab.py`) gates this change before merge.

### D.3 CLARIFY surface for source_binding ambiguity

When `capture_intent` or `design_skill` produces `source_binding_mode: "ambiguous"` or
`source_binding_mode: "ask_parameterized"` and the user has not explicitly confirmed,
the CLARIFY state (ADR-028 Item 3) surfaces a **blocking** question:

> "You described a source that the user will supply at query time ('for a given page'
> / 'whichever page the user passes'). Shall this skill:
> (A) always extract from the specific pages we configure now — the same page every
>     time, or
> (B) extract from whichever page the user passes at query time — a different page
>     on each invocation?"

This is a **blocking** ambiguity (not nice_to_know): the answer changes the schema
structure (whether a typed `page_id` input is added), the INGEST step (whether any
page is ingested at author time), and the VALIDATE step (whether the Confluence adapter
must be present in the target deployment).

The `capture_intent` prompt (version `"1.0"` in skill_builder.yaml) gains a new
output field:

```
"source_binding_mode": "author_fixed" | "ask_parameterized" | "ambiguous"
"source_binding_signal": "one-line evidence from the intent text (< 80 chars)"
```

If `source_binding_mode` is `"ask_parameterized"` or `"ambiguous"`, this goes into
`blocking_ambiguities` — the CLARIFY gate fires before CONFIGURE_SOURCES.

`capture_intent` version bump: `"1.0"` → `"1.1"`. New fixture required. Gate via
ADR-030 harness.

### D.4 VALIDATE gate amendment

The `VALIDATE` state (state 12 in ADR-016 lifecycle, implemented in the
`_handle_validate` method of conversation.py) gains one new check:

If the skill has `source_binding.mode == ask_parameterized` and
`source_binding.ingest_on_demand: true`, the validator checks whether the Confluence
adapter is configured in the target deployment environment:

```python
if source_binding_mode == "ask_parameterized" and ingest_on_demand:
    if not confluence_adapter_available(target_env):
        raise ValidationError(
            "This skill requires live Confluence access at consumption time "
            "(source_binding.ingest_on_demand: true). The target deployment "
            f"environment ({target_env}) has no Confluence adapter configured. "
            "Either configure a Confluence adapter in the target env, or set "
            "ingest_on_demand: false."
        )
```

A skill that requires live Confluence access at consumption time cannot be promoted
to a deployment that has no Confluence adapter.

---

## E. Runtime Ingestion (P2) — Option C Concrete Design

### E.1 Confluence adapter reachability from mcp_server process

**Finding (load-bearing for Option C feasibility):**

The `mcp_server.py` lifespan already contains `_build_confluence_adapter`-equivalent
logic in `framework/skill_builder/conversation.py` (used by the INGEST state of the
authorSkill flow). This function reads `framework/config/adapters/confluence.yaml`
merged with env-specific overrides and builds the appropriate adapter.

In **laptop mode** (`KBF_ENV=laptop`), the configured adapter is `emcp_direct` or
`codex_proxy` — both of which use macOS Keychain OAuth tokens stored by Codex. The
`mcp_server` process runs in the same user session as Codex, so the Keychain is
accessible. `emcp_direct` has been confirmed to work in the `mcp_server` process
(it is already called from `skill_builder/conversation.py` INGEST state, which runs
server-side during `authorSkill` sessions).

In **production** (`KBF_ENV=staging|production`), the configured adapter is `native`
or `mcp` — both of which use a service-account API token stored in OCI Vault. The
`mcp_server` production process already reads Vault secrets for other credentials
(ADB, cost store). Adding Confluence token retrieval follows the same pattern.

**Verdict: the Confluence adapter IS reachable from the mcp_server process in both
laptop and production modes, using the existing adapter factory pattern from
conversation.py.** This is confirmed by the fact that the INGEST state of
authorSkill already calls this same adapter path from within the mcp_server process.
No new credential mechanism is required.

**The one caveat:** in production, the Confluence adapter is currently only
initialized during the INGEST state of `authorSkill` sessions (on demand, per
session). For the P2 ephemeral path, it must be initialized once at lifespan startup,
as an optional dependency: if no Confluence adapter is configured, `ask_parameterized`
skills hard-fail with an actionable message. The adapter is NOT required for the
server to start (graceful optional dependency).

### E.2 WorkflowExecutor ephemeral path design

The ephemeral fetch path is added to `WorkflowExecutor._retrieve_for_inputs` in
`framework/workflow_runtime/executor.py`, immediately before the P3 guard block.

Execution flow for `ask_parameterized` skills:

```python
def _retrieve_for_inputs(self, cfg, inputs, sources):
    source_binding = cfg.get("source_binding") or {}
    sb_mode = source_binding.get("mode", "author_fixed")

    if sb_mode == "ask_parameterized":
        input_param = source_binding.get("input_param", "")
        page_ref = inputs.get(input_param, "")
        page_id = _resolve_page_id(page_ref)  # handles URL + numeric forms

        ingest_on_demand = source_binding.get("ingest_on_demand", False)
        space_allow_list = source_binding.get("space_allow_list") or []
        ttl = int(source_binding.get("ephemeral_ttl_seconds", 300))

        if not ingest_on_demand or not self.confluence_adapter:
            raise ConfluencePageNotInKBError(page_id, cfg.get("workflow_skill",""))

        # Space allow-list check BEFORE any HTTP call
        page_space = _extract_space_key(page_ref)  # from URL or Confluence API
        if space_allow_list and page_space and page_space not in space_allow_list:
            raise ConfluencePageNotInKBError(
                page_id, cfg.get("workflow_skill",""),
                reason=f"Space '{page_space}' is not in the skill's allow-list "
                       f"{space_allow_list}. Contact the skill author."
            )

        # In-process TTL cache check
        cache_key = f"ephemeral:{page_id}"
        cached = _ephemeral_cache.get(cache_key, ttl)
        if cached is not None:
            return cached  # cache hit: skip the HTTP call

        # Ephemeral fetch: NO write to WikiMetadataStore or any persistent store
        raw_item = self.confluence_adapter.fetch(
            RawItemRef(kind="confluence_page", source="confluence", source_id=page_id)
        )
        body_html = (
            raw_item.payload.get("body", {}).get("storage", {}).get("value", "")
            or raw_item.payload.get("body", "")
        )
        # Extract using the skill's authored schema — schema-bounded, not free-form
        skill_schema = cfg.get("schema") or {}
        extracted = self._llm_extract_fields(body_html, skill_schema)

        citation_url = (
            raw_item.metadata.get("url")
            or f"{self.confluence_base_url}/wiki/pages/{page_id}"
        )

        passages = [{
            "text": json.dumps(extracted, ensure_ascii=False),
            "citation": citation_url,
            "metadata": {
                "page_id": page_id,
                "space": raw_item.metadata.get("space"),
                "title": raw_item.metadata.get("title"),
                "ephemeral": True,       # NEVER written to persistent store
                "fetched_at": datetime.utcnow().isoformat() + "Z",
            },
            "kb": cfg.get("requires_extractions", [{}])[0].get("kb", ""),
        }]

        # Audit log — every ephemeral fetch is logged (spec §10)
        self._log_ephemeral_fetch(page_id, page_space, cfg.get("workflow_skill",""))

        # Cache for TTL seconds
        _ephemeral_cache.put(cache_key, passages, ttl)
        return passages

    # --- author_fixed path: existing behavior unchanged ---
    return self._retrieve_author_fixed(cfg, inputs, sources)
```

Key contract points:
- `WikiMetadataStore.add()` and `IncidentVectorStore.upsert()` are NEVER called in
  the ephemeral path. The only persistent write is the audit log (append-only JSONL).
- `_ephemeral_cache` is a module-level in-process LRU/TTL dict (thread-safe via
  `threading.Lock`). It is never persisted to disk.
- `self.confluence_adapter` is injected at `WorkflowExecutor.__init__` time, set
  to `None` if no adapter is configured. The constructor signature becomes:
  `WorkflowExecutor(store, llm, retrievers, shim_kb, confluence_adapter=None)`.

### E.3 mcp_server.py lifespan wiring

In `_load_app()` → `lifespan()`, after the existing retriever initialization:

```python
# Optional: Confluence adapter for ask_parameterized skill ephemeral fetch (ADR-032 P2)
# Graceful: if unavailable, ask_parameterized skills hard-fail with actionable message.
confluence_adapter = None
if _any_promoted_skill_requires_ephemeral(WORKFLOW_SKILLS_DIR):
    confluence_adapter = _build_confluence_adapter(kbf_env, REPO_ROOT)
    if confluence_adapter is None:
        log.warning(
            "ask_parameterized skills with ingest_on_demand:true are present but "
            "no Confluence adapter is configured — those skills will hard-fail at "
            "consumption time with an actionable message."
        )
    else:
        log.info("Confluence adapter initialized for ephemeral fetch: %s",
                 confluence_adapter.mode)

state["workflow_executor"] = WorkflowExecutor(
    store=None,
    llm=state["llm"],
    retrievers=retrievers,
    shim_kb=state["shim_kb"],
    confluence_adapter=confluence_adapter,   # NEW: optional, may be None
)
```

`_build_confluence_adapter` is the SAME factory function that already exists in
`framework/skill_builder/conversation.py`. It is relocated to a shared utility
(e.g., `framework/adapters/confluence/factory.py`) so both `conversation.py` and
`mcp_server.py` call the same code.

`_any_promoted_skill_requires_ephemeral()` scans the workflow skills directory for
any skill YAML with `source_binding.mode: ask_parameterized` and
`ingest_on_demand: true`. This avoids initializing the adapter if no skill uses it.

### E.4 P3 guard rewire (retire regex heuristic)

With P1 in place (every ask_parameterized skill has `source_binding.input_param`),
the regex heuristic in `_extract_confluence_page_ids` is retired:

- For `ask_parameterized` skills: the page_id is read directly from
  `inputs[source_binding.input_param]` — no regex needed.
- For `author_fixed` skills: the P3 guard is inert (no page ref is expected in
  inputs; the executor never applies the regex to fixed-source skills).

The transition: after P1 ships, `_extract_confluence_page_ids` and the P3 guard
block in `_retrieve_for_inputs` are removed. The schema-driven path in the
`ask_parameterized` branch provides the equivalent guarantee: if the page ref is
present in `source_binding.input_param` and the ephemeral fetch fails or returns
no usable content, `ConfluencePageNotInKBError` is raised. The invariant is
preserved by the schema path, not the regex path.

This is a clean retirement: the regex and the schema-driven check are never
simultaneously active on the same skill invocation. Author_fixed skills are
unaffected by both.

### E.5 Ephemeral TTL cache specification

```
Type:      module-level dict protected by threading.Lock
Key:       "ephemeral:{page_id}"  (string)
Value:     (passages: list[dict], fetched_at: float, ttl: int)
Eviction:  on get: if time.time() - fetched_at > ttl, evict and return None
           on put: insert or replace
Size cap:  50 entries maximum (LRU eviction at cap)
Thread:    all get/put operations under threading.Lock (the executor may be called
           from concurrent request handlers in a multi-worker uvicorn deployment)
Scope:     process-local. Not shared across uvicorn workers (workers have separate
           memory spaces). Each worker maintains its own cache. This is acceptable:
           the cache is purely a latency optimization, not a correctness requirement.
Disk:      NEVER written to disk or any persistent store.
```

### E.6 API response disclosure

When an ephemeral fetch occurred, the ask response includes:

```json
{
  "answer": { ... },
  "tier_used": 1,
  "source_fetched_on_demand": true,
  "source_fetched_page_id": "18625350641",
  "latency_note": "This request fetched a Confluence page on demand (+2–15s)."
}
```

This field is added to the `ask` response schema in `framework/deploy/openapi.yaml`.

---

## F. EVAL Interaction (ADR-029)

An ask_parameterized skill cannot be evaluated against a fixed gold page in the same
way as a fixed-source skill.

**Decision:** The eval harness handles ask_parameterized skills as follows:

1. The auto-generated gold rows (from INSPECT_SOURCES at author time) are generated
   from the pages inspected during skill authoring. Those pages are fixed at author
   time for the gold set.
2. At EVAL time, the executor is invoked with the gold pages as the `page_id` input.
   This exercises the ephemeral fetch path against known content and measures
   extraction quality against the gold rows.
3. The eval disclosure note for ask_parameterized skills is extended:
   > "This is a source-parameterized skill. The gold set was generated from the
   > author-time pages. Extraction quality on consumer-supplied pages may vary.
   > Manual testing against a representative sample of consumer-supplied pages is
   > recommended before fleet promotion."
4. The EVAL state in conversation.py, when `source_binding.mode == ask_parameterized`,
   injects the gold page IDs as `page_id` inputs for each eval invocation. This is
   a targeted change to `_handle_eval` — it reads `source_binding.input_param` to
   know which input field to populate with the gold page ID.

---

## G. The No-Silent-Substitution Invariant (standing policy)

The failure's most dangerous property is that it produced a plausible-looking but
wrong output with no signal to the consumer. This is the same violation class as
BUG-queue-2ad9a (wrong artifact type silently returned) and BUG-queue-44364
(truncated extractions silently stored as complete).

**The invariant, stated as a hard rule:**

> If a workflow skill is source-parameterized (detected either by
> `source_binding.mode == ask_parameterized` or, temporarily, by the P3 regex
> heuristic on the input string), and the requested source is not retrievable,
> the executor MUST hard-fail with an actionable message. It MUST NOT retrieve
> content from a different source, return a partial result, or return a
> content-filter-style "no answer."

This invariant is enforced by `ConfluencePageNotInKBError` (already shipped in P3)
and by the space allow-list rejection in the P2 ephemeral path.

---

## H. Migration

**Existing skills:** all existing workflow skills have no `source_binding:` block.
They default to `mode: author_fixed`. No behavior change. No re-authoring required.

**Newly authored skills:** the `authorSkill` flow detects the ask-parameterized
pattern at `CAPTURE_INTENT` and `CONFIGURE_SOURCES`, surfaces it via `CLARIFY`,
and populates the `source_binding` block in the synthesized YAML when the user
confirms `mode: ask_parameterized`.

**The four affected TPM skills:** these should be re-authored through the updated
`authorSkill` flow to add the `source_binding` block and the typed `page_id` input.
Until they are re-authored, the P3 hard-fail guard (commit 8c947dc) prevents silent
page substitution; they will hard-fail with the actionable ingest instruction when
an un-ingested pageId is supplied.

---

## I. Consequences

### Positive

- The silent wrong-page substitution class is eliminated (P3 shipped; P1/P2 replace
  the heuristic with schema-driven enforcement).
- Authors can explicitly express ask-time parameterization intent.
- Consumers get the right content for their specified page, without needing to
  pre-ingest every page they might ever ask about.
- The shared KB remains curated: ephemeral pages do not accumulate in the shared store.
- Citations are always real Confluence URLs (not "fixture://" paths).

### Negative

- **Latency:** ask-parameterized skills with `ingest_on_demand: true` add 2-15
  seconds on cache miss. Disclosed in API response (`source_fetched_on_demand: true`).
- **Trust boundary residual:** the author-time grant model + space allow-list
  mitigates but does not eliminate the authorization surface. Full per-consumer
  OAuth is v2.
- **Complexity:** three new author-time concepts (source_binding mode, typed input,
  space allow-list) must be understood by skill authors. The authorSkill flow hides
  most of this complexity.
- **Adapter availability gate:** ask_parameterized + ingest_on_demand:true requires
  the Confluence adapter in the mcp_server process. The VALIDATE state enforces this.

### Reversibility

- The P3 hard-fail guard is trivially reversible (remove the guard block).
- The `source_binding` schema field is additive; removing it reverts to author_fixed.
- Option C's ephemeral fetch can be disabled per skill by setting
  `ingest_on_demand: false` — the skill then hard-fails with the ingest instruction.

---

## References

- [ADR-015 — Skill-by-demonstration](ADR-015-skill-by-demonstration.md)
- [ADR-016 — Workflow skills](ADR-016-workflow-skills.md) — schema amended here
- [ADR-027 — Design-first authorSkill 16-state machine](ADR-027-design-first-authorskill.md)
- [ADR-028 — authorSkill prompt investment, human-loop, conversational clarification](ADR-028-authorskill-prompt-investment-human-loop-conversation.md)
- [ADR-029 — Outcome-based EVAL acceptance loop](ADR-029-outcome-based-eval-acceptance-loop.md)
- [ADR-030 — Prompt externalization to PromptRegistry](ADR-030-prompt-externalization-and-harness.md)
- [ADR-031 — No arbitrary content caps / no silent degradation](ADR-031-no-arbitrary-content-caps.md)
- [ADR-032 — Implementation Blueprint](ADR-032-impl-plan.md)
- [DECISION-012 — Resolved: Option C](../../pmo/decisions/DECISION-012-ask-time-source-ingestion-option.md)
- Commit 8c947dc — P3 guard shipped standalone (ConfluencePageNotInKBError + 19 tests)
- `framework/workflow_runtime/executor.py` — P3 guard + P2 ephemeral path target
- `framework/deploy/mcp_server.py` — lifespan wiring target for Confluence adapter
- `framework/skill_builder/conversation.py` — CAPTURE_INTENT/CLARIFY/DESIGN_SKILL/VALIDATE target
- `framework/config/prompts/skill_builder.yaml` — prompt version bumps (P1)
- `framework/workflow_skills/tpm/project_tracking_confluence_stakeholder_status_meeting_email.yaml` — primary affected skill
