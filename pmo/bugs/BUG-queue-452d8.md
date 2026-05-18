---
queue_id: BUG-queue-452d8
source: user_report
tool: authorSkill
filed_at: 2026-05-18T07:28:30
status: open
---

# BUG-queue-452d8

**Tool**: `authorSkill` | **Filed**: 2026-05-18 | **Status**: open

DECISION-013 Phase A Bug (RC1-A retrieval bug, misfiled as BUG-rc1a-4eb5a53a which never existed in …

<details>
<summary>Full details</summary>

**Description**:
DECISION-013 Phase A Bug (RC1-A retrieval bug, misfiled as BUG-rc1a-4eb5a53a which never existed in ADB): executor._retrieve_author_fixed_pinned hard-fails with ConfluencePageNotInKBError when pinned_ref is a Confluence /display/SPACE/Title URL form because _resolve_page_id returns the URL unchanged (no numeric pageId extractable), and _passage_matches_page_id compared the URL string against ingested numeric pageIds — 0 matches. Fix already present in main: _passage_matches_page_id now delegates to _passage_matches_display_url (space+title matching) when the pinned_ref is a display URL. The prior BUG-rc1a-4eb5a53a queue_id was used in wiki log but never recorded in ADB (ADB count=0 per user query). This bug record establishes the correct ADB-backed record for the RC1-A fix.

**Triggering input**:
_not recorded_

**User ID**: 218a5f843d6c3eee
**Request ID**: req-phase-a-bug-2

</details>
