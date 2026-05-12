---
title: BUG-005 — authorSkill validation fails: workflow references 'tpm.skill_data' but persona builder registers 'tpm.skill'
status: verified
created: 2026-05-12
verified: 2026-05-12
owner: qa
assigned-to: backend-dev
severity: blocker
related-story: V3-deployment-layer
component: framework/skill_builder/synthesize_workflow.py
discovered-by: user (live MCP session via /mcp tools/call)
fixed-in: (this commit)
---

## Steps to reproduce

1. Complete a full `authorSkill` session through commit (requires BUG-002, BUG-003,
   BUG-004 to be fixed first).

2. When the session advances to VALIDATE, validation fails:
   ```
   workflow references unknown KB: 'tpm.weekly_26ai_exec_review_ppt_data'
   ```

## Expected

Validation passes (no unknown KB errors). Session advances to INGEST → EVAL → PROMOTE.

## Actual

```
workflow skill file does not exist: ... (before BUG-004 fix)
workflow references unknown KB: 'tpm.weekly_26ai_exec_review_ppt_data' (after BUG-004 fix)
```

## Root cause

Two places in the skill builder generate the KB name for a newly-created skill,
and they used inconsistent conventions:

**`conversation.py` line 912** — persona builder entry registration:
```python
pb_entry = synthesize_persona_builder_diff(
    persona=persona,
    kb_name=f"{persona}.{skill_name}",   # ← NO _data suffix
    ...
)
```

**`synthesize_workflow.py` lines 126 and 143** — workflow `requires_extractions` block:
```python
kb_name = f"{persona}.{skill_name}_data"   # ← _data suffix added
entries.append({"kb": kb_name, ...})
```

`validate_workflow_links()` reads the workflow YAML and looks up each `kb` reference
in the persona builder index. The index is keyed by `{persona}.{kb_name}` (no suffix).
The workflow references `tpm.weekly_26ai_exec_review_ppt_data` but the index contains
only `tpm.weekly_26ai_exec_review_ppt`. Lookup fails → validation error.

The `_data` suffix was a gratuitous semantic convention ("this KB holds data") that was
applied in the synthesizer but not in the builder registration, causing a naming split.

## Fix

Remove `_data` from both sites in `synthesize_workflow.py` using `replace_all`:

```python
# Before (two occurrences)
f"{persona}.{skill_name}_data"

# After
f"{persona}.{skill_name}"
```

Both sides now agree: persona builder registers `tpm.weekly_26ai_exec_review_ppt`
and the workflow references `tpm.weekly_26ai_exec_review_ppt`.

## Verification

After server restart, a new `authorSkill` session advances through VALIDATE without
the "unknown KB" error.

## Test gap to close

`test_validate_links.py` (or the skill builder unit tests) should include a
round-trip test:

1. Call `synthesize_persona_builder_diff` → extract the registered `kb_name`.
2. Call `synthesize_workflow_skill` with matching args → extract the `requires_extractions[].kb`.
3. Assert they are equal — this would have caught the `_data` split immediately.
