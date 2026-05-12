# BUG-009: VALIDATE step fails "unknown KB" for newly authored skills

**Queue ID**: BUG-queue-6c173  
**Status**: FIXED  
**Severity**: High (blocks every new-KB skill from completing validation)  
**Session**: synth-tpm-ec2fad6d  
**Filed**: 2026-05-12  
**Fixed in**: conversation.py `_run_validate()` (same session, 2026-05-12)

---

## Symptom

After successfully uploading an artifact, completing field review, configuring sources, previewing, and committing all 5 artifacts, the VALIDATE step fails with:

```
workflow references unknown KB: tpm.weekly_26ai_executive_review_ppt_using_faaas_proje
```

The user is blocked — they cannot proceed to INGEST or PROMOTE.

---

## Root Cause

`_run_validate()` passes `pb_dir = REPO_ROOT/framework/persona_builders` to `validate_workflow_links()`.

`_build_kb_index()` (in `validate_links.py`) reads only **filesystem** `*.yaml` files from that directory to build the KB lookup table.

But when a session authors a *new* KB, the `persona_builder_delta` artifact is committed to **ADB** (via `skill_store.write_artifact()`). It is not written to the filesystem until the session reaches PROMOTE. Therefore at VALIDATE time, the validator cannot find the newly authored KB → "unknown KB" error.

---

## Fix

In `_run_validate()`, before calling `validate_workflow_links`:

1. Read `persona_builder_delta` from skill_store (ADB).
2. Parse the YAML content back to a dict (single KB-entry format from `synthesize_persona_builder_diff`).
3. Wrap it in a full persona-builder YAML structure: `{persona: ..., knowledge_bases: [delta_entry]}`.
4. Write to a temp directory alongside copies of the filesystem persona builders.
5. Pass the merged temp directory to `validate_workflow_links` as `persona_builders_dir`.
6. Clean up temp directory in the `finally` block.

**File changed**: `framework/skill_builder/conversation.py` — `_run_validate()` method.

---

## Related

- ADR-017: workflow link validation contract  
- ADR-023: ADB-always storage (ADB is always available)  
- BUG-queue-d44ac: PROMOTE constraint violation (separate bug, next in queue)
