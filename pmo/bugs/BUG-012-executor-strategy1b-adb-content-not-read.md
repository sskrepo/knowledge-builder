# BUG-012: executor Strategy 1b — ADB content field never read (ConfluencePageNotInKBError at EVAL)

**Queue ID**: BUG-queue-decision013
**ADB Bug ID**: BUG-4bc65b21
**Status**: FIXED
**Severity**: HIGH (EVAL Path A hard-fail for all author_fixed skills with ADB-backed wiki pages)
**Session**: 2026-05-19 authorSkill PROMOTE mission
**Filed**: 2026-05-19
**Fixed in**: 09aa33c — `framework/workflow_runtime/executor.py` (`_retrieve_author_fixed_pinned`)

---

## Symptom

EVAL Path A failed with `ConfluencePageNotInKBError` for skill `tpm.faaas_kiwi_project_pptx` even though the pinned Confluence page (canonical_id `20382503622`) was correctly ingested and stored in `KB_SHIM.KBF_WIKI_PAGES` with full markdown content in the CLOB `body` column.

---

## Root Cause

`executor.py` Strategy 1b (`_retrieve_author_fixed_pinned`) looked up the wiki page record via `AdbWikiMetadataStore` and read:

```python
file_path = rec.get("path", "")
if file_path and Path(file_path).exists():
    body = Path(file_path).read_text()
```

`AdbWikiMetadataStore._row_to_record` correctly sets `"path": ""` for ADB-backed records — these records have no filesystem path, the content is stored in the `content` CLOB column of `KBF_WIKI_PAGES`. The executor never read `rec.get("content", "")`, so `body = ""` → passage skipped → Strategy 3 hard-fail.

The bug exists because Strategy 1b was written when the wiki store was file-backed. When DECISION-022 moved the wiki store to ADB, the executor was not updated to handle the new `content` field.

---

## Fix

After the failed filesystem path read, also try `rec.get("content", "")`:

```python
# ADB-backed path: path is "" but content is stored in record.
if not body:
    inline_content = rec.get("content", "")
    if inline_content:
        body = inline_content
        log.info(
            "ADR-039 Strategy 1b: using inline content from "
            "ADB-backed store for canonical_id=%r (no filesystem path).",
            pinned_page_id,
        )
```

---

## Tests Added

`framework/tests/unit/test_strategy1b_adb_content_and_pathb_draft_card.py`:
- `TestStrategy1bAdbContent::test_strategy1b_uses_inline_content_when_path_empty`
- `TestStrategy1bAdbContent::test_strategy1b_filesystem_path_still_works`
- `TestStrategy1bAdbContent::test_strategy1b_falls_through_to_strategy3_when_both_path_and_content_empty`
- `TestStrategy1bAdbContent::test_strategy1b_inline_content_fallback_when_path_file_missing`
