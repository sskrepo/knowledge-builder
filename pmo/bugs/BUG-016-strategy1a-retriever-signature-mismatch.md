# BUG-016: Strategy 1a retriever invoked with wrong signature → render crash (no graceful fallback)

**Queue ID**: BUG-queue-decision013
**Status**: FIXED
**Severity**: HIGH (every author_fixed pinned skill whose KB card resolves to a corpus-based retriever fails tier-1 render on the live mcp_server path)
**Session**: 2026-05-19 authorSkill PROMOTE mission
**Filed**: 2026-05-19
**Fixed in**: `framework/workflow_runtime/executor.py` — `_retrieve_author_fixed_pinned` Strategy 1a + new `_invoke_retriever_compat` helper
**Discovered by**: live mcp_server askKnowledgeBase, skill `faaas_kiwi_project_pptx`

---

## Symptom

```
ERROR framework.deploy.routes.ask: render: WorkflowExecutor.execute failed:
  __call__() missing 1 required positional argument: 'corpus'
  File ".../executor.py", line 1104, in _retrieve_author_fixed_pinned
    results = retriever(query=query_text, persona=card.get("persona"))
  TypeError: __call__() missing 1 required positional argument: 'corpus'
  ...
  File ".../executor.py", line 1106, in _retrieve_author_fixed_pinned
    results = retriever(query=query_text)
  TypeError: __call__() missing 1 required positional argument: 'corpus'
```

Tier-1 routed correctly (conf 0.95, `faaas_kiwi_project_pptx`), pinned_ref resolved
correctly (`20382503622`). The crash is purely in retriever invocation.

---

## Root Cause (two independent defects)

**D1 — interface mismatch.** Strategy 1a invoked every retriever with a hard-coded
generic keyword shape (`query=`, optional `persona=`) and never threaded the
retriever-specific required args. The KB card's `retrieval_tools` for this skill
resolved to `VectorSearchRetriever`, whose `__call__(self, corpus, query, …)`
requires `corpus` (no default). `corpus` was derivable in scope (`kb_full_name` /
`short_name`) but never passed. Retrievers in `framework/retrievers/` have
heterogeneous signatures (`vector_search(corpus, query…)`, `search_wiki(query,
persona…)`, `text_to_sql(nl_query…)`, `read_wiki_page(path)`, …); a single
fixed call shape only fits some.

**D2 — structurally broken fallback.** The `except TypeError` wrapped only the
*first* call. The fallback `retriever(query=query_text)` on the next line sat
outside any handler, so any signature mismatch raised an uncaught `TypeError`
that aborted `_retrieve_author_fixed_pinned` before **Strategy 1b** (the direct
`wiki_store.list_pages()` lookup, the designed safety net) could run.

**Why now:** prior SKILL-1 verification ran via the EVAL executor with no
retrievers/shim_kb wired → Strategy 1a was always skipped → Strategy 1b served
it. On the live `mcp_server` path retrievers ARE wired, so Strategy 1a ran for
the first time against a real `vector_search` and hit the mismatch. Promotion
was genuine; this was an untested live-wiring interaction, not a SKILL-1
regression.

---

## Fix

Added `_invoke_retriever_compat(retriever, *, tool_name, query, corpus, persona)`:
- Introspects the callable via `inspect.signature(retriever)` (handles plain
  functions, lambdas, AND callable class instances — does NOT introspect
  `retriever.__call__`, which for a plain function resolves to the useless
  `(*args, **kwargs)` method-wrapper signature).
- Passes only the parameters the callable declares, sourced from a fixed value
  pool `{query, nl_query, corpus, persona}`.
- If the callable has a required (no-default) parameter the pool cannot satisfy
  (e.g. `path`, `resource_type`), logs at INFO and returns `None` — caller skips
  it; Strategy 1b is the safety net. Never raises.
- Any exception from the call itself is logged and converted to `None`. A
  bad/unsupported retriever NEVER aborts `_retrieve_author_fixed_pinned`.

Strategy 1a now calls the shim with `corpus=(kb_full_name or short_name)` and
`continue`s on `None`.

---

## Verification

- New shim unit-verified across: class-instance (`vector_search` — gets
  `corpus`), plain function, lambda, required-unsuppliable-param (skipped, no
  crash), exception-raising retriever (caught, no crash).
- Targeted regression: `test_executor_source_guard`, `test_executor_ephemeral`,
  `test_strategy1b_adb_content_and_pathb_draft_card`,
  `test_author_fixed_ingest_roundtrip`, `test_decision020_*` — 122 passed.
- Full unit suite: **1792 passed, 8 failed**. The 8 failures
  (`test_code_wiki::test_find_symbol_function` + 7 `test_smoke_validate`) are
  the known pre-existing baseline — confirmed identical on unmodified HEAD via
  `git stash`. **Zero new regressions.**

---

## Related

- ADR-039 (DECISION-020): pinned-source canonicalization (Strategy 1a/1b context)
- BUG-014 / BUG-queue-decision013: same session, sibling fixes
- DECISION-013: every fix files a bug record
