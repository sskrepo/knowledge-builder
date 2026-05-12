---
title: BUG-007 — Gold set seeded with null expected_extraction values when no example artifact was analysed
status: verified
created: 2026-05-12
verified: 2026-05-12
owner: qa
assigned-to: backend-dev
severity: minor
related-story: V3-deployment-layer
component: framework/skill_builder/gold_seed.py
discovered-by: user (live MCP session via /mcp tools/call)
fixed-in: (this commit)
---

## Steps to reproduce

Complete an `authorSkill` session that does NOT load an example artifact in the
`ANALYZE_ARTIFACT` step (i.e. the session takes the direct intent path without
artifact analysis). Inspect the committed extraction gold set:

```
eval/gold_sets/tpm-{skill_name}-extraction.jsonl
```

## Expected

```json
{
  "expected_extraction": {
    "project": "<example project>",
    "rag_status": "<example rag_status>",
    "risks": "<example risks>",
    "next_steps": "<example next_steps>"
  }
}
```

## Actual

```json
{
  "expected_extraction": {
    "project": null,
    "rag_status": null,
    "risks": null,
    "next_steps": null
  }
}
```

## Root cause

In `conversation.py`, `_synthesize_preview()` calls:

```python
gold_entries = seed_gold_set(
    persona=persona,
    kb_name=skill_name,
    artifact_path=self._data.artifact_path,
    extracted_fields={f: None for f in gaps},   # ← all None
)
```

When no example artifact has been analysed (`artifact_path` is empty and no
field-value mapping was produced), the code passes `{field: None}` for every
field. `seed_gold_set` wrote these through verbatim as `"expected_extraction"`.

Python `None` serialises as JSON `null`, producing gold entries that have the
right field names but no usable example values — making the gold set harder to
annotate and potentially confusing eval tooling.

## Fix

`gold_seed.seed_gold_set()` now replaces `None` values with readable placeholder
strings:

```python
cleaned_fields = {
    k: (v if v is not None else f"<example {k}>")
    for k, v in extracted_fields.items()
}
```

Result: the gold set is immediately usable for annotation without silently
encoding the absence of data as `null`.

## Test gap to close

Add a test to `test_gold_seed.py` (or equivalent) that calls `seed_gold_set`
with a dict containing `None` values and asserts:
1. `expected_extraction` contains no `null` values
2. Each placeholder is a non-empty string containing the field name
