---
queue_id: BUG-queue-59900
source: user_report
tool: authorSkill
filed_at: 2026-05-14T04:27:31
status: open
---

# BUG-queue-59900

**Tool**: `authorSkill` | **Filed**: 2026-05-14 | **Status**: open

Confluence /display/SPACE/Page+Title URLs fail ingestion with "'NoneType' object has no attribute 'l…

<details>
<summary>Full details</summary>

**Description**:
Confluence /display/SPACE/Page+Title URLs fail ingestion with "'NoneType' object has no attribute 'lower'". Repro: at CONFIGURE_SOURCES, paste a Confluence URL of the form https://confluence.oraclecorp.com/confluence/display/OCIFACP/Project+Plan (no pageId, just space + page title). State machine accepts it and stores as {kind: 'confluence', pages: ['<url>'], page_urls: ['<url>']}. On ingest, fails with AttributeError "'NoneType' object has no attribute 'lower'". Sibling source in the same session (https://...viewpage.action?pageId=20030556732) ingests successfully on the same retry (items_upserted: 1) — so the failure is specific to URLs that go through the display-path resolver. Likely cause: code that parses the display URL extracts the space key (e.g., 'OCIFACP') and slug ('Project+Plan'), then tries to lower-case some field but the field is None for this URL form (maybe the title-resolved-to-pageId returned None, or a content-type/labels field came back null). Affected session: synth-tpm-8bb804ae. Related fixes: BUG-queue-cf562 (the prior 'str.get' error on the same source kind, now fixed). Suggested fix: in the display-URL parser, guard the .lower() call against None; if title→pageId resolution fails, raise a clear error like "Could not resolve display URL to a page ID — Confluence returned no match for space 'OCIFACP' / title 'Project Plan'" rather than the cryptic NoneType error. Priority: high — half of the recommended URL-paste paths are still broken.

**Triggering input**:
```json
{
  "affected_session": "synth-tpm-8bb804ae",
  "state_at_failure": "INGEST",
  "failing_source": "https://confluence.oraclecorp.com/confluence/display/OCIFACP/Project+Plan",
  "working_sibling_source": "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=20030556732",
  "error": "'NoneType' object has no attribute 'lower'",
  "related_fix": "BUG-queue-cf562 (the prior str.get error on the same source kind was fixed and exposed this one)",
  "ingest_status_partial": {
    "items_processed": 1,
    "items_upserted": 1,
    "pages_updated": 1,
    "failures": 1
  }
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: ingest-display-url-NoneType-lower

</details>
