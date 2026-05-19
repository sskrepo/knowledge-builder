---
queue_id: BUG-queue-ae642
source: user_report
tool: authorSkill
filed_at: 2026-05-19T05:39:23
status: open
---

# BUG-queue-ae642

**Tool**: `authorSkill` | **Filed**: 2026-05-19 | **Status**: open

DECISION-013 BUG (wiki KB portability): WikiMetadataStore was filestore-only (~/.kbf/store/wiki_meta…

<details>
<summary>Full details</summary>

**Description**:
DECISION-013 BUG (wiki KB portability): WikiMetadataStore was filestore-only (~/.kbf/store/wiki_metadata/). Promoted author_fixed skills pinned to Confluence pages stored ONLY on the authoring laptop filesystem. On any other host (prod VM, other laptop), _retrieve_author_fixed_pinned Strategy 1a and 1b found nothing — hard-failed with ConfluencePageNotInKBError. Promoted skills were not portable, violating ADR-023 (ADB-always for promoted artifacts). Fix: AdbWikiMetadataStore backed by KB_SHIM.KBF_WIKI_PAGES (DECISION-022). canonical_ref round-trip preserved end-to-end: ingest stamps → ADB stores → retriever returns → executor _passage_matches_canonical=True.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: architect-decision022-BUG-queue-ae642

</details>
