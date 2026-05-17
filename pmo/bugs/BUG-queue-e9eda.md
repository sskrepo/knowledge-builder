---
queue_id: BUG-queue-e9eda
source: user_report
tool: authorSkill
filed_at: 2026-05-16T15:31:57
status: open
---

# BUG-queue-e9eda

**Tool**: `authorSkill` | **Filed**: 2026-05-16 | **Status**: open

Architect-RCA companion to user report BUG-queue-990fe. Two root causes: (RC1) ConfluenceWikiIngesto…

<details>
<summary>Full details</summary>

**Description**:
Architect-RCA companion to user report BUG-queue-990fe. Two root causes: (RC1) ConfluenceWikiIngestor had no persona param so pages stored with persona=null, losing persona association downstream. (RC2) _CONFLUENCE_PAGE_REF_PATTERNS missing the natural-language 'pageId 18625350641' form (no '=') so the hard-fail guard was bypassed and a different ingested page was silently substituted. Fix commits: 280451a (RC1+RC2+A3+A4) + 8c947dc (P3 mismatch hard-fail).

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-280451a

</details>
