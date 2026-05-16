# ADR-031 — No Arbitrary Content Caps: Generous Sizing for ADB-Backed Skills

**Status**: Accepted
**Date**: 2026-05-16
**Deciders**: User directive + BUG-queue-44364 audit
**Supersedes**: Implicit prior assumptions in synthesize_schema.py, conversation.py, review.py, executor.py

---

## Context

BUG-queue-44364 (2026-05-15) traced a class of silent data-loss bugs: LLM-produced
skill content was being truncated before persisting to ADB (CLOB), but the framework
never surfaced an error — it silently stored incomplete extractions as if they were
complete. The root causes were two-fold:

1. **Arbitrary maxLength caps in synthesized schemas**: `synthesize_schema._infer_field_spec`
   emitted `"maxLength": 1000` on summary/text/description fields and `"maxLength": 500`
   on all other strings. These propagated into design and extraction prompts, training the
   LLM to clip values. ADB CLOB has no such constraint.

2. **Small source-text caps**: `_llm_extract` in `review.py`, `_llm_extract_fields` in
   `executor.py`, and the `_source_grounded_review` and `inspect_sources` methods in
   `conversation.py` all capped the source text fed to the LLM at 3k–24k chars. Since
   gpt-4o accepts ~128k input tokens, these caps silently discarded source structure
   (WBS tables, multi-section Confluence pages) that the LLM needed to extract all fields.

3. **Unguarded JSON parse sites**: Several LLM call sites used raw `json.loads()` without
   truncation detection. A response that hit the token ceiling would produce a structurally
   incomplete JSON, which would fail with a generic parse error giving no diagnosis.
   Worst case: the last hard-coded prompt (`_SOURCE_GROUNDED_REVIEW_PROMPT`) had not been
   migrated to PromptRegistry (ADR-030), so its `max_tokens=2048` could never be raised
   without a code change.

The stop-bleed (commit bf6dfab) raised `max_tokens` in `extraction` and `design_skill`
YAML entries from 2048/4096 to 16384. This ADR covers the comprehensive fix.

---

## Decision

### 1. No arbitrary maxLength in synthesized schemas

`synthesize_schema._infer_field_spec` (and any downstream schema synthesis path) must
not emit `maxLength` on fields whose storage is ADB CLOB. Only two field categories
retain explicit limits — they are genuinely source-bounded, not arbitrary:

- **`_id` fields**: `maxLength: 64` — identifiers have a natural upper bound.
- **`_status` fields**: `maxLength: 50` — short categorical strings.

All other fields — summaries, descriptions, body text, narratives, catch-all strings —
carry **no `maxLength`**. The LLM is instructed to extract fully, and ADB stores it fully.

The `design_skill` and `review_design_replan` YAML templates in `skill_builder.yaml`
still reference example `maxLength` values (200/500/2000) as illustrative guidance in
the prompt template text. These are **LLM-facing suggestions**, not schema constraints
that get emitted into synthesized field specs. They should be updated in a follow-up to
remove any implied cap — but changing prompt template text is gate-sensitive (prompts
may be checksummed) and is deferred to a separate change.

### 2. LLM input source text sized to model context window

Source text caps are raised to be generous relative to the model context window (~128k
tokens for gpt-4o / OCI GenAI synthesis model). Specific values:

| Site | Old cap | New cap | Rationale |
|------|---------|---------|-----------|
| `review._llm_extract` per-text | 12,000 chars | 80,000 chars | Parity with executor |
| `executor._llm_extract_fields` per-snippet | 24,000 chars | 80,000 chars | Large Confluence pages |
| `conversation.inspect_sources` per-sample | 3,000 chars | 20,000 chars | WBS tables are wide |
| `conversation.inspect_sources` total | 6,000 chars | 40,000 chars | Multiple pages |
| `conversation._source_grounded_review` per-sample | 4,000 chars | 20,000 chars | Schema coherence |
| `conversation._source_grounded_review` total | 8,000 chars | 40,000 chars | Multiple pages |
| `conversation._run_eval` gold-row source_snippet | 12,000 chars | 80,000 chars | Judge parity |

Display-only and logging-only slices (e.g. `[:100]` on descriptions in review display,
`[:150]` for value previews, `[:40]`/`[:80]` in log messages) are intentionally left
unchanged — they do not affect storage or LLM inputs.

### 3. All LLM-JSON parse sites must detect truncation and hard-fail

Every site that calls `llm.chat()` and then parses the JSON response must:

- Capture `tokens_out` from the result dict.
- Parse via the shared `_parse_llm_json_response(raw, tokens_out=..., max_tokens=...)` helper.
- If `tokens_out >= max_tokens` and parsing fails: raise `ValueError` naming
  **BUG-queue-44364** with actionable text (increase `max_tokens` or reduce schema size).
- **Never** return `{}` silently or persist a truncated extraction.

The last hard-coded prompt (`_SOURCE_GROUNDED_REVIEW_PROMPT`) has been migrated to
PromptRegistry as `source_grounded_review` in `skill_builder.yaml` (ADR-031 C1). Its
`max_tokens` is now 4096 (raised from the hard-coded 2048) and is operator-configurable
in YAML without a code change. This completes the ADR-030 migration.

---

## Consequences

- **No silent content clipping**: skill field values extracted from ADB-backed sources
  are stored at their full length. A 2000-char executive summary is stored as 2000 chars.
- **Truncation is a hard error**: operators see an actionable error instead of a
  silently incomplete skill. The fix path is clear: raise `max_tokens` in YAML or
  reduce schema field count.
- **Larger LLM prompts**: source text caps are raised 3–10×. This increases token cost
  per extraction call. For the current set of Confluence-backed skills this is acceptable
  (source pages average 5–15k chars; the new cap still leaves headroom). Re-evaluate if
  average source size grows past 50k chars.
- **Eval quality gate covers full schema**: `expected_fields` in eval gold rows now covers
  all fields (was capped at 5). The eval quality gate is more stringent.

---

## References

- BUG-queue-44364 (2026-05-15): original truncation bug report
- Commit bf6dfab: stop-bleed — raised extraction/design max_tokens 4096→16384 in YAML
- ADR-030: prompt externalization to PromptRegistry
- User directive: "no structural limitations like character limits impacting skill creation;
  we use ADB so be generous"
