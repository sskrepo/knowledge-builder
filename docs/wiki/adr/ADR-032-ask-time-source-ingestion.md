---
title: ADR-032 — Ask-time / Runtime Source Ingestion as a First-Class Skill Capability
status: proposed
created: 2026-05-16
owner: architect
deciders: user, tpm
tags: [adr, skill-builder, consumption, ingestion, workflow-skills, adr-016, adr-029, adr-031]
related: [ADR-015, ADR-016, ADR-027, ADR-029, ADR-030, ADR-031]
supersedes: ~
---

# ADR-032 — Ask-time / Runtime Source Ingestion as a First-Class Skill Capability

## Status

**Proposed — 2026-05-16.** This ADR documents an observed production failure, its
root causes, and proposes three implementation options. A user decision is required
to select the runtime-ingestion option; see DECISION-012.

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

The most likely invoked skill is `project_tracking_confluence_stakeholder_status_meeting_email`
(the only one whose `skill_card.summary` explicitly names "read a project tracking
Confluence page"). Its `trigger.on_request.inputs[0]` is typed as `string` named
`input` with description "Query or filter input" — no page reference semantics.

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

At `_resolve_sources()` (executor.py line 143-148): the implementation is a stub that
returns `cfg.get("sources", [])`. Since no workflow YAML has a `sources:` key (it uses
`requires_extractions:` instead), this always returns `[]`.

At `_retrieve_for_inputs()` (executor.py line 150-236): the method tries three paths in
order:
1. **Live retrievers + shim_kb** — queries the KB card's `retrieval_tools` against the
   *already-ingested* KB. If the user-supplied pageId is not in the KB, the retriever
   returns results for whatever *is* in the KB (the only ingested project-plan page).
2. **Legacy direct-store fallback** — queries `store.query()` with incident_id/release_id
   or vector KNN — no page_id filtering.
3. **Fixture data fallback** — loads from `framework/_dev_fixtures/`.

**There is no ingest-on-demand step anywhere in this chain.** The `ConfluenceWikiIngestor`
and `ConfluenceNativeAdapter` exist in the ingestion pipeline
(`framework/ingestion/confluence_wiki_ingest.py`,
`framework/deploy/ingestion_worker.py`) but are not imported or reachable from
`WorkflowExecutor._retrieve_for_inputs`.

The Confluence adapter is accessible at ask time in principle — `codex_proxy` mode
provides OAuth-gated Confluence access in laptop mode, and `ConfluenceNativeAdapter`
with API token is available in production — but the ask/retrieval path never invokes
either. The trust boundary (an arbitrary consumer-supplied URL triggering a live
Confluence HTTP call inside the request handler) has never been evaluated.

### P3 — Silent Wrong-Page Substitution (most urgent, separable)

When path 1 of `_retrieve_for_inputs` runs with a user-supplied pageId that is not
in the KB, the `search_wiki` or `vector_search` retriever does not filter by pageId
— it runs a semantic similarity search over *all* ingested pages and returns the
highest-scoring result. The skill then synthesizes the email from that content.

The exact code path for the substitution:

```
executor.py:165-208   _retrieve_for_inputs, live-retriever branch:
  for req in cfg.get("requires_extractions", []):          # line 171
      card = cards_by_name.get(short_name)                 # line 174
      tools = card.get("retrieval_tools") or []            # line 179
      for tool_name in tools:                              # line 180
          retriever = self.retrievers.get(tool_name)       # line 181
          results = retriever(query=query_text, ...)       # line 189
          # query_text = " ".join(str(v) for v in inputs.values())
          # NO page_id filter applied to the retriever call
          for r in results or []:                          # line 195
              passages.append(...)                         # line 196-202
          if passages:
              break                                        # line 208 — first result wins
```

The `inputs` dict passed from the user contains `{"input": "...pageId=18625350641..."}`.
The executor joins all input values into `query_text` (line 163). The retriever receives
this free-text string and returns the best semantic match over all ingested pages. Since
pageId=18625350641 is not in the KB, the retriever returns pageId=20030556732 (the only
similar-domain ingested page). The executor never checks whether the retrieved page
matches the requested pageId.

**There is no assertion anywhere that "retrieved source must match requested source."**
This is a direct instance of the no-silent-degradation violation class established by
BUG-queue-2ad9a and ADR-031. The BUG-queue-2ad9a fix (commit 8c2bec1, ADR-016
amendment) addressed the *wrong-skill* substitution class (draft skills reaching the
classifier); this failure is the *wrong-page* substitution class (wrong content reaching
the extractor) — same policy violation, different layer.

---

## C. P3 Fix-Now Recommendation

**P3 can and should be fixed NOW as a standalone change, independent of the larger
P1/P2 feature.**

P3 is entirely separable: it requires no schema changes, no new ingestion path, no
author-time detection. It requires only that the executor enforce a "requested source
must match retrieved source" assertion when the skill is source-parameterized.

The fix is a targeted guard in `WorkflowExecutor._retrieve_for_inputs` (and/or in the
`_match_workflow_skill` input extraction logic):

**Detection heuristic (until P1 contract exists):** if the user's input string contains
a recognizable Confluence page reference (`pageId=\d+` or a Confluence URL pattern),
extract it and use it as a hard filter on retrieval. If retrieval returns no results
matching that page reference, hard-fail with an actionable message.

**Required hard-fail message:**
```
Page {pageId} is not in the knowledge base.
To use this skill with a specific Confluence page, that page must first be ingested.
Run: kb-cli ingest --page-id {pageId} --persona tpm
Then retry your request.
```

**Why fix now, not as part of ADR-032:**
- The fix prevents the silent-wrong-output class from recurring on any future invocation
  of any source-parameterized skill, regardless of whether the full ask-time ingestion
  feature is ever built.
- It costs 1-2 days of dev, is reversible, and does not constrain the ADR-032 options.
- Waiting for ADR-032 acceptance means every consumption invocation of these skills
  against an un-ingested page continues to silently substitute the wrong page.

**The one entanglement with P1:** the detection heuristic (regex on input string) is a
workaround for the missing `source_binding` schema field. Once P1 is implemented, the
hard-fail guard can be rewritten against the schema field instead of the regex. This is
additive — the regex guard ships now; the schema-driven guard replaces it later.

**Recommendation: fix P3 now with the regex heuristic; P1+P2 refine it.**

**P3 guard landed standalone ahead of P1/P2 — see commit for SHA.** The heuristic
regex-on-input guard is implemented in `framework/workflow_runtime/executor.py`
(`_extract_confluence_page_ids`, `_passage_matches_page_id`, `ConfluencePageNotInKBError`,
and the guard block in `_retrieve_for_inputs`). It will be replaced by the
`source_binding.input_param` schema field when ADR-032 P1 ships. DECISION-012 options
A/B/C remain open and unconstrained by this guard.

---

## D. Author-Time Detection (P1)

### D.1 The design-contract gap

The ADR-016 workflow skill YAML has no field expressing whether a skill's source
binding is fixed at author time or parameterized at ask time. The `trigger.on_request.inputs`
array exists but has no semantic type that would let the executor distinguish "this input
is a page reference that I should fetch" from "this input is a free-text query I should
search with."

Similarly, `CAPTURE_INTENT` and `CLARIFY` in the 16-state machine have no prompt logic
to surface this ambiguity to the user. The `_CAPTURE_INTENT_PROMPT` (ADR-030 YAML store)
asks for `output_kind`, `audience`, `cadence`, `scope_domains`, and `success_criteria`
— but never asks "is the source fixed (you always use the same Confluence page) or
parameterized (the user supplies which page at query time)?"

### D.2 Where the user gets asked

The detection should happen at **CAPTURE_INTENT** (state 2) and **CONFIGURE_SOURCES**
(state 3) in the 16-state machine. Specifically:

- If the user's described intent includes phrases like "for a given page", "based on
  the page the user provides", "accept a Confluence URL", or "for any project tracking
  page" — the `_CAPTURE_INTENT_PROMPT` should flag this as a potential
  `ask_parameterized` binding and add it to `normalised_intent.ambiguities`.
- At **CONFIGURE_SOURCES**, the CLARIFY state (ADR-028 Item 3) should surface the
  ambiguity: "You described a source that the user will supply at query time. Should
  this skill (a) always extract from the specific pages we configure now, or (b) extract
  from whichever page the user passes at query time?"
- The user's answer determines `source_binding.mode`.

### D.3 Schema field — proposed YAML amendment

Amend the ADR-016 workflow skill YAML schema with a `source_binding` block:

```yaml
# Amendment to ADR-016 workflow skill schema
# Added to: framework/workflow_skills/_template.yaml

source_binding:
  mode: author_fixed          # author_fixed | ask_parameterized
                              # author_fixed (default): source(s) ingested at authorSkill time;
                              #   consumption retrieves from the pre-ingested KB.
                              # ask_parameterized: consumer supplies the source at ask time;
                              #   workflow must obtain+use THAT source on demand.
  input_param: page_id        # (ask_parameterized only) name of the trigger.on_request.inputs
                              # entry that carries the source reference (pageId or URL).
  ingest_on_demand: true      # (ask_parameterized only) when true, executor attempts to ingest
                              # the consumer-supplied source before retrieval. When false (or
                              # when ingest path is unavailable), executor hard-fails with
                              # actionable message instead of silently substituting.
  source_type: confluence_page  # confluence_page | confluence_space | jira_filter | git_ref
```

The corresponding `trigger.on_request.inputs` entry must declare the input with a
typed semantic:

```yaml
trigger:
  on_request:
    enabled: true
    inputs:
      - name: page_id
        type: confluence_page_ref    # new typed input; executor extracts pageId from this
        description: "Confluence pageId or full page URL of the project tracking page to use"
        required: true
    output_format: email
    response_mode: artifact_url
```

**Migration:** all existing workflow skills that have no `source_binding:` block default
to `mode: author_fixed`. No behavior change for existing skills.

### D.4 Prompt-level change (ADR-030 YAML prompts)

The `_CAPTURE_INTENT_PROMPT` (in `framework/config/prompts/skill_builder.yaml`) needs
one new output field in its JSON schema:

```yaml
# Additional field in capture_intent prompt output schema:
# "source_binding_mode": "author_fixed" | "ask_parameterized" | "ambiguous"
# "source_binding_signal": one-line evidence from the user's intent text
```

The `_CONFIGURE_SOURCES_SUGGEST_PROMPT` should surface the ambiguity when
`source_binding_mode == "ambiguous"` with an explicit "ask the user" instruction.

These prompt changes go through the ADR-030 PromptRegistry path. They require a new
prompt version, a new fixture in `framework/tests/fixtures/prompts/`, and re-running
the harness. They do not require changing the `_FAILURE_CLASSIFIER_PROMPT` (gate-locked).

---

## E. Runtime Ingestion Options (P2)

The three options for the runtime-ingestion mechanism in `WorkflowExecutor`.

### Option A — Synchronous Ingest-on-Demand Inside the Ask

When the executor detects an `ask_parameterized` skill with an un-ingested source, it
invokes `ConfluenceWikiIngestor.ingest_page(page_id)` synchronously within the request
handler, before retrieval.

**Flow:**
```
executor._retrieve_for_inputs():
  if source_binding.mode == "ask_parameterized":
    page_id = inputs[source_binding.input_param]
    if not wiki_store.page_exists(page_id):
      if confluence_adapter is not None and source_binding.ingest_on_demand:
        ingestor.ingest_page(page_id)   # synchronous HTTP call to Confluence
      else:
        raise SourceNotIngested(page_id, actionable_message)
    results = retriever(query=query_text, page_filter=page_id)
```

**Pros:**
- Zero consumer friction: the user passes the pageId and gets the right email, even
  if the page was never ingested before.
- Idempotent: `ConfluenceWikiIngestor` uses content-hash IDs (spec §10); re-ingesting
  an already-current page is a no-op.
- Freshness guarantee: the content is always current as of the ask time.

**Cons:**
- **Latency**: a Confluence page fetch + parse + embed + store takes 5-30 seconds
  for a well-connected deployment. This makes the ask path non-deterministic in
  latency. The consumer experiences a 30-second response instead of a 5-second one,
  with no warning.
- **Trust boundary (CRITICAL)**: the consumer supplies an arbitrary Confluence URL.
  The executor will issue a live HTTP call to Confluence with the service's API token
  to fetch whatever page the consumer specifies. There is no validation that the
  consumer is authorized to see that page. This is an authorization bypass: a consumer
  with `read` scope on the KB but no Confluence access could use the skill to extract
  any Confluence page they can guess the ID of.
- **Ingestion path availability**: the `ConfluenceNativeAdapter` requires an API token
  in Vault. In `laptop` mode, it requires a `codex_proxy` OAuth session. The ask
  handler has no guaranteed access to either. `ingestion_worker.py` is the component
  with adapter access — it is separate from the `mcp_server.py` process.
- **Fights the spec §2 ingest/retrieve separation**: principle §5 states "every content
  creation flows through the parser." The ask handler is explicitly not the parser.
  Mixing ingestion into the retrieval path violates the architectural boundary that
  keeps the consumption runtime stateless and fast.

**Effort:** Medium (3-5 days). Requires wiring `ConfluenceWikiIngestor` + adapter
into `mcp_server.py` lifespan (currently only in `ingestion_worker.py`), adding
latency disclosure to the API response, and permission validation.

**Verdict:** Architecturally problematic. The trust boundary issue alone is a blocker.
The latency violation and spec §2 conflict compound it.

---

### Option B — Ask Triggers the Ingestion Pipeline; Skill Hard-Fails with Retry Instruction Until the Page Lands

When the executor detects an `ask_parameterized` skill with an un-ingested source, it
emits an ingestion request to the existing ingestion pipeline (via a queue or a direct
call to `ingestion_worker`) and returns a structured hard-fail with a retry instruction.

**Flow:**
```
executor._retrieve_for_inputs():
  if source_binding.mode == "ask_parameterized":
    page_id = inputs[source_binding.input_param]
    if not wiki_store.page_exists(page_id):
      ingestion_worker.enqueue_page(page_id, persona=skill.persona)  # async queue
      raise SourceBeingIngested(
        page_id,
        message=(
          f"Page {page_id} is not yet in the knowledge base. "
          f"Ingestion has been triggered. Retry in 30-60 seconds."
        )
      )
    results = retriever(query=query_text, page_filter=page_id)
```

**Pros:**
- The ask path remains stateless: no synchronous Confluence calls inside the request
  handler.
- Ingestion runs through the existing pipeline (adapter, parser, WikiMetadataStore,
  vector embedding) — all the existing quality gates and idempotency checks apply.
- The trust boundary is cleaner: the ingestion worker owns Confluence credentials and
  applies its own access control logic.
- Compatible with spec §2: ingestion and retrieval remain separate components.

**Cons:**
- Consumer UX is poor: "retry in 30-60 seconds" is an unfamiliar failure mode for a
  chatbot interface. Most LLM clients (Claude Code, Codex) will not auto-retry.
- The queue mechanism does not exist yet. Adding a `enqueue_page` API to the ingestion
  worker requires a new IPC channel (HTTP, Redis queue, or OS pipe) between the
  MCP server process and the ingestion worker process. This is a non-trivial
  infrastructure addition.
- No freshness guarantee: the consumer's retry may hit a race condition where ingestion
  is still running.
- **Trust boundary is still present, slightly softer:** the consumer triggers ingestion
  of an arbitrary pageId via the ask API. The ingestion worker fetches it. There is
  still no validation that the consumer is authorized to see that page. The attacker
  model is the same; the execution context is different.

**Effort:** High (5-8 days). New IPC channel + ingestion queue + retry semantics +
ingestion worker changes.

**Verdict:** Maintains architectural separation but introduces operational complexity
and poor UX. The trust boundary problem is not solved — it is deferred to the ingestion
worker, which currently has no per-request authorization check.

---

### Option C — Request-Scoped Ephemeral Ingestion (Recommended)

When the executor detects an `ask_parameterized` skill with an un-ingested source, it
fetches the page content directly from Confluence via the existing `ConfluenceNativeAdapter`
(or `codex_proxy` in laptop mode), processes it through the LLM extraction path
(`_llm_extract_fields` with the skill's schema), uses the result immediately for
synthesis, and **does not persist** the extracted content to the shared KB. The content
is ephemeral to the request.

**Flow:**
```
executor._retrieve_for_inputs():
  if source_binding.mode == "ask_parameterized":
    page_id = inputs[source_binding.input_param]
    if not wiki_store.page_exists(page_id):
      if confluence_adapter is None or not source_binding.ingest_on_demand:
        raise SourceNotIngested(page_id, actionable_message)
      # Ephemeral fetch: get page, extract, use, discard
      raw_page = confluence_adapter.get_page(page_id)   # live Confluence HTTP call
      ephemeral_text = raw_page.get("body", "")
      passages = [{
        "text": ephemeral_text,
        "citation": f"https://{confluence_host}/wiki/spaces/.../{page_id}",
        "metadata": {"page_id": page_id, "ephemeral": True},
        "kb": skill.requires_extractions[0].kb,
      }]
      return passages    # skip the KB retrieval step
    results = retriever(query=query_text, page_filter=page_id)
```

**Pros:**
- The consumer gets the right content immediately, no retry required.
- No persistence: the shared KB is not polluted with one-off pages that no other skill
  uses. The KB remains curated.
- No new infrastructure: uses the existing `ConfluenceNativeAdapter` (already in
  `ingestion_worker.py`; needs to be wired into `mcp_server.py` lifespan as an optional
  dependency for skills that declare `ingest_on_demand: true`).
- Citations are correct: the ephemeral passage carries the real Confluence URL.
- Compatible with the idempotency requirement: if the same page IS in the KB, the
  regular retrieval path runs; ephemeral path only activates on cache miss.
- No queue, no IPC, no retry semantics.

**Cons:**
- **Latency:** a live Confluence HTTP call adds 2-15 seconds to the request. This must
  be disclosed in the API response (a new `source_fetched_on_demand: true` field in the
  ask response) and documented in the skill card.
- **Trust boundary (same problem as Option A, different framing):** the ask handler
  issues a Confluence HTTP call on behalf of the consumer's supplied pageId. The
  consumer is not separately authorized against Confluence — they are authorized to the
  KB with `read` scope, and the skill's `source_binding.ingest_on_demand: true` is the
  author's grant that "this skill may fetch arbitrary pages at consumer request." This is
  an *author-time trust grant*, not a per-consumer authorization. The author who promoted
  the skill is implicitly authorizing all consumers of the skill to trigger Confluence
  fetches within the adapter's credential scope. This must be documented as an explicit
  design decision, not left implicit.
- **No persistence = no incremental update benefit**: every invocation refetches the
  page. If the page is large or the Confluence API is slow, every invocation pays the
  cost. A simple TTL cache (in-process, keyed by pageId + content-hash) can mitigate
  this.
- **Confluence adapter not currently wired into ask path**: requires a focused addition
  to `mcp_server.py` lifespan to optionally initialize the Confluence adapter when any
  promoted skill declares `source_binding.mode == ask_parameterized`. This is
  configuration-driven and non-breaking.

**Effort:** Medium (3-5 days). Adapter wiring into the ask path, `_retrieve_for_inputs`
guard, latency disclosure, author-time trust documentation.

**Recommendation: Option C.** It is the only option that delivers the correct content
immediately without requiring a queue, without polluting the shared KB, and without
requiring a consumer retry loop. The trust boundary issue is real and must be addressed
as a documented design decision (see section F), but it is manageable within Option C's
author-time grant model. Option A fails on the trust boundary because it is implicit.
Option B fails on UX and operational complexity.

---

## F. The Trust Boundary

This is the single highest-risk aspect of ask-time ingestion. It must be addressed
explicitly.

**The risk:** A consumer with `read` scope on `askKnowledgeBase` could use a
source-parameterized skill to trigger a live Confluence fetch of any page ID they
supply, including pages they are not authorized to see in Confluence directly. The
service's Confluence API token (or OAuth session) has broader access than any individual
consumer.

**The mitigation within Option C:**

1. **Author-time grant**: `ingest_on_demand: true` in the skill YAML is a deliberate
   author decision that "this skill may fetch arbitrary Confluence pages." The skill
   author (a TPM who promotes the skill) is accountable for this grant. Skills with
   `ingest_on_demand: false` never trigger live Confluence calls.

2. **Scope restriction**: the Confluence adapter should be initialized with the minimum
   required permission scope for the persona's declared sources. A TPM skill that
   declares `source_type: confluence_page` and `space_key: FA` should only fetch pages
   in the FA space — not arbitrary pages. This requires the adapter to enforce a space
   allow-list derived from the skill's `requires_extractions` source declarations.

3. **Rate limiting**: the existing RPM limiter on `askKnowledgeBase` limits how many
   live Confluence fetches a consumer can trigger per minute.

4. **ACL placeholder (spec §10)**: every ephemeral passage carries
   `persona_visibility` and `classification` from the skill card. The synthesizer
   respects these. The content is never persisted without the ACL fields.

5. **Audit logging**: every ephemeral fetch is logged with `{consumer_id, page_id,
   skill_name, timestamp}`. This is the audit trail for misuse detection.

**The residual risk:** mitigations 1-5 do not prevent a motivated attacker from
crafting a pageId guess attack within the allowed space. The v1 mitigation is the
space allow-list + rate limiting. Full per-consumer Confluence ACL enforcement is v2
and requires the OAuth-per-user flow (ADR-020 codex_proxy already provides this in
laptop mode — the architecture exists).

**Honest assessment of architectural soundness:** ask-time ingestion within Option C's
ephemeral model does not fundamentally violate the spec's ingest/retrieve separation
(§2 principle 5) because the fetched content is not "created" in the persistent KB —
it is fetched and used within a single request lifetime. The parser discipline (spec
§2 principle 3 — deterministic extraction rules) is upheld because the same
`_llm_extract_fields` method with the skill's authored schema is used. The retrieval
path is extended, not bypassed.

The stronger concern is spec §2 principle 2: "LLM-in-ingestion != LLM-in-retrieval."
Option C blurs this line because the ephemeral extraction happens inside the retrieval
request. The ADR's answer: this is acceptable *only* for `ask_parameterized` skills
where the schema is already designed and authored (it is not autonomous LLM extraction
— it is schema-constrained extraction of a user-specified source). The LLM's role at
ask time is bounded by the authored schema, which was reviewed and promoted by a persona
team. This is materially different from unconstrained ask-time LLM extraction.

---

## G. No-Silent-Substitution Invariant (P3 as standing policy)

The failure's most dangerous property is that it produced a plausible-looking but
wrong output with no signal to the consumer that anything was wrong. This is the same
violation class as BUG-queue-2ad9a (wrong artifact type silently returned) and
BUG-queue-44364 (truncated extractions silently stored as complete).

**The invariant, stated as a hard rule:**

> If a workflow skill is source-parameterized (detected either by `source_binding.mode
> == ask_parameterized` or by the P3 regex heuristic on the input string), and the
> requested source is not retrievable from the KB, the executor MUST hard-fail with an
> actionable message. It MUST NOT retrieve content from a different source, return a
> partial result, or return a content-filter-style "no answer." The hard-fail is the
> correct answer.

**Required hard-fail response shape** (surfaces through the existing `tier_4` path):

```json
{
  "answer": {
    "Answer": "Page 18625350641 is not in the knowledge base. This skill requires the specified Confluence page to be ingested before use. Run: kb-cli ingest --page-id 18625350641 --persona tpm. Then retry your request."
  },
  "tier_used": 4,
  "tier_description": "source_not_available",
  "source_not_available": {
    "page_id": "18625350641",
    "skill": "project_tracking_confluence_stakeholder_status_meeting_email",
    "resolution": "ingest then retry"
  }
}
```

This is the P3 fix now (section C), stated as policy so it cannot be re-violated in
future executor changes.

---

## H. Interaction with Prior ADRs

### ADR-029 EVAL

A source-parameterized skill cannot be evaluated against a fixed gold page in the same
way as a fixed-source skill. The EVAL state (ADR-029 outcome-based acceptance loop)
must be amended for `ask_parameterized` skills:

- The auto-generated gold rows (DECISION-010) are generated from the page(s) inspected
  at `INSPECT_SOURCES`. Those pages are fixed at author time for the gold set.
- At EVAL time, the executor runs against those same pages — so the gold set is valid
  for measuring extraction quality on the known pages.
- However, the gold set does not measure whether the skill correctly handles an
  *arbitrary* page supplied at consumption time. This is a known limitation; the EVAL
  disclosure note should be extended: "This is a source-parameterized skill. The gold
  set was generated from the author-time pages. Extraction quality on consumer-supplied
  pages may vary. Manual testing against a representative sample of consumer-supplied
  pages is recommended before fleet promotion."

### ADR-016 lifecycle

The `source_binding` block is an amendment to the ADR-016 YAML schema. The lifecycle
stages (`draft → committed → promoted → production`) are unchanged. The new field adds
a validation step to `VALIDATE` (state 12): if `source_binding.mode == ask_parameterized`
and `source_binding.ingest_on_demand: true`, the validator must check that the
Confluence adapter is available in the target deployment environment (production or
staging). A skill that requires live Confluence access at consumption time cannot be
promoted to a deployment that has no Confluence adapter configured.

### ADR-016 Amendment — BUG-queue-2ad9a (commit 8c2bec1)

The ADB-aware `ShimWorkflows` promotion fix is adjacent but orthogonal. That fix
addresses the wrong-skill routing problem (drafts reaching the Tier-1 classifier).
This ADR addresses the wrong-page substitution problem (right skill, wrong content).
Both are instances of the no-silent-degradation policy. Both fixes should coexist
without interaction.

### ADR-030 (prompt externalization)

The prompt changes required for P1 detection (capture_intent, configure_sources prompt
amendments) go through the ADR-030 PromptRegistry path. The new `source_binding_mode`
output field in `capture_intent` does not require modifying the gate-locked
`_FAILURE_CLASSIFIER_PROMPT`. Prompt changes are gated by the ADR-030 harness
(`prompt_lab.py`), not by the classifier gate.

### ADR-031 (no silent degradation)

P3 is a direct instance of the ADR-031 no-arbitrary-content-caps / no-silent-degradation
policy, extended to source selection. The policy principle is: the framework must never
silently produce output from a different source than the one the user specified. The
hard-fail invariant (section G) is the ADR-031 equivalent for source substitution.

---

## I. Workflow Schema — Complete Proposed Amendment

```yaml
# New top-level block in workflow skill YAML (ADR-016 amendment)
# Default for all existing skills: source_binding block absent → author_fixed behavior

source_binding:
  mode: ask_parameterized     # author_fixed | ask_parameterized

  # ask_parameterized only:
  input_param: page_id        # which trigger.on_request.inputs entry holds the source ref
  ingest_on_demand: true      # whether to attempt ephemeral fetch (Option C) on cache miss
  source_type: confluence_page  # confluence_page | confluence_space | jira_filter
  space_allowlist:            # optional; restrict ephemeral fetch to these spaces
    - FA
    - PROJ
  ephemeral_ttl_seconds: 300  # optional; in-process TTL cache for fetched content (0 = no cache)

# Amended trigger.on_request.inputs to include typed source reference:
trigger:
  on_request:
    enabled: true
    inputs:
      - name: page_id
        type: confluence_page_ref  # new semantic type; executor extracts pageId
        description: "Confluence pageId or full page URL of the page to use"
        required: true
      - name: input
        type: string
        description: "Additional context or query modifier (optional)"
        required: false
    output_format: email
    response_mode: artifact_url
```

---

## J. Migration

**Existing skills:** all existing workflow skills have no `source_binding:` block. They
default to `mode: author_fixed`. No behavior change. No re-authoring required.

**Newly authored skills:** the `authorSkill` flow detects the ask-parameterized pattern
at `CAPTURE_INTENT` and `CONFIGURE_SOURCES`, surfaces it to the user via `CLARIFY`, and
populates the `source_binding` block in the synthesized YAML when the user confirms
`mode: ask_parameterized`.

**The four affected TPM skills:** these should be re-authored through the updated
`authorSkill` flow to add the `source_binding` block and the typed `page_id` input.
Until they are re-authored, the P3 hard-fail guard (section C) prevents them from
silently substituting the wrong page; they will hard-fail with the actionable ingest
instruction when an un-ingested pageId is supplied.

---

## K. Consequences

### Positive

- The silent wrong-page substitution class is eliminated by P3 (immediately, before
  this ADR is decided).
- Authors can explicitly express ask-time parameterization intent, which the framework
  honours rather than silently ignoring.
- Consumers get the right content for their specified page, without needing to
  pre-ingest every page they might ever ask about.
- The EVAL disclosure is extended to be honest about source-parameterized skill
  limitations.

### Negative

- **Latency:** ask-parameterized skills with `ingest_on_demand: true` add 2-15 seconds
  to the ask latency on cache miss. This must be disclosed.
- **Trust boundary:** live Confluence fetches at ask time introduce an authorization
  surface. The author-time grant model (section F) mitigates but does not eliminate
  this risk.
- **Complexity:** three new concepts (source_binding mode, typed input, ephemeral
  ingestion) must be understood by skill authors. The `authorSkill` flow hides most of
  this complexity, but skill authors who edit YAML directly must understand the schema.
- **Adapter availability gate:** `ask_parameterized` + `ingest_on_demand: true` requires
  the Confluence adapter to be wired into the MCP server. Deployments without
  Confluence access cannot promote such skills.

### Reversibility

- The P3 hard-fail guard is trivially reversible (remove the heuristic check).
- The `source_binding` schema field is additive; removing it reverts to `author_fixed`
  behavior.
- Option C's ephemeral fetch path can be disabled per skill by setting
  `ingest_on_demand: false` — the skill then hard-fails with the ingest instruction
  instead of fetching.

---

## L. Options Summary

| | Option A (sync ingest in ask) | Option B (queue + retry) | Option C (ephemeral, recommended) |
|---|---|---|---|
| **Consumer UX** | Seamless (no retry) | Poor (retry required) | Seamless (no retry) |
| **KB persistence** | Yes (pollutes shared KB) | Yes | No (ephemeral) |
| **Latency** | +5-30s (blocking) | Hard-fail then background | +2-15s (blocking, disclosed) |
| **Trust boundary** | Implicit (worst) | Deferred to worker | Author-time grant (best) |
| **Arch soundness** | Violates §2 ingest/retrieve | Preserves separation | Acceptable (schema-bounded) |
| **Infrastructure needed** | Adapter in ask path | Queue + IPC | Adapter in ask path |
| **Effort** | Medium (3-5d) | High (5-8d) | Medium (3-5d) |

**Recommendation: Option C.** Decision required from user. See DECISION-012.

---

## References

- [ADR-015 — Skill-by-demonstration](ADR-015-skill-by-demonstration.md)
- [ADR-016 — Workflow skills](ADR-016-workflow-skills.md) — schema to amend
- [ADR-027 — Design-first authorSkill 16-state machine](ADR-027-design-first-authorskill.md)
- [ADR-028 — authorSkill prompt investment, human-loop, conversational clarification](ADR-028-authorskill-prompt-investment-human-loop-conversation.md)
- [ADR-029 — Outcome-based EVAL acceptance loop](ADR-029-outcome-based-eval-acceptance-loop.md)
- [ADR-030 — Prompt externalization to PromptRegistry](ADR-030-prompt-externalization-and-harness.md)
- [ADR-031 — No arbitrary content caps / no silent degradation](ADR-031-no-arbitrary-content-caps.md)
- [DECISION-012 — Runtime ingestion option for ask-parameterized skills](../../pmo/decisions/DECISION-012-ask-time-source-ingestion-option.md)
- Commit 8c2bec1 — BUG-queue-2ad9a ShimWorkflows ADB-aware (adjacent no-silent-degradation fix)
- `framework/workflow_runtime/executor.py:143-236` — `_resolve_sources` + `_retrieve_for_inputs` (P3 fix target)
- `framework/persona_skills/_base.py:398-422` — `_invoke_workflow` (Tier-1 dispatch)
- `framework/workflow_skills/tpm/project_tracking_confluence_stakeholder_status_meeting_email.yaml` — primary affected skill
