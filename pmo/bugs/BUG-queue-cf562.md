---
queue_id: BUG-queue-cf562
source: user_report
tool: authorSkill
filed_at: 2026-05-14T04:10:48
status: open
---

# BUG-queue-cf562

**Tool**: `authorSkill` | **Filed**: 2026-05-14 | **Status**: open

Ingestion adapter for the new Confluence page-URL/page-id source kind throws AttributeError: "'str' …

<details>
<summary>Full details</summary>

**Description**:
Ingestion adapter for the new Confluence page-URL/page-id source kind throws AttributeError: "'str' object has no attribute 'get'". Repro: at CONFIGURE_SOURCES (which now advertises "Confluence specific page (recommended when you have a link): paste the page URL"), paste two Confluence URLs as separate sources — one a viewpage.action?pageId=... URL and one a /display/SPACE/Page+Title URL. The state machine accepts both and stores them with shape {kind: 'confluence', pages: [<id_or_url>], page_urls: [<url>]}. Commit succeeds, validate passes (ADR-017 link check OK). On 'yes, ingest', the adapter fails for each source with: "'str' object has no attribute 'get'" — a Python AttributeError indicating the adapter iterated over `pages` expecting each item to be a dict ({"id": ..., "title": ...}, etc.) but found a string. Affected session: synth-tpm-8bb804ae. Likely fix: when sources have shape {pages: [str, ...]} (which is the shape produced by the URL-paste path), the adapter needs to either (a) coerce each entry to a {id|url} dict before calling .get(), or (b) handle the string case directly. Same shape worked fine for the label-filter path (which produces 0 pages but doesn't crash) and for the empty stub path — the bug is specific to the URL-paste source kind that's now advertised as the recommended method. Priority: high — blocks any skill that uses page URLs (the very path the state machine recommends).

**Triggering input**:
```json
{
  "affected_session": "synth-tpm-8bb804ae",
  "state_at_failure": "INGEST",
  "sources_configured": [
    {
      "kind": "confluence",
      "pages": [
        "20030556732"
      ],
      "page_urls": [
        "https://confluence.oraclecorp.com/confluence/pages/viewpage.action?pageId=20030556732"
      ]
    },
    {
      "kind": "confluence",
      "pages": [
        "https://confluence.oraclecorp.com/confluence/display/OCIFACP/Project+Plan"
      ],
      "page_urls": [
        "https://confluence.oraclecorp.com/confluence/display/OCIFACP/Project+Plan"
      ]
    }
  ],
  "failures": [
    {
      "space": "pages=['20030556732']",
      "error": "'str' object has no attribute 'get'"
    },
    {
      "space": "pages=['https://confluence.oraclecorp.com/confluence/display/OCIFACP/Project+Plan']",
      "error": "'str' object has no attribute 'get'"
    }
  ],
  "advertised_input": "the CONFIGURE_SOURCES prompt now says 'Confluence specific page (recommended when you have a link): paste the page URL'"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: ingest-page-url-type-error

</details>
