---
queue_id: BUG-queue-13e25
source: user_report
tool: authorSkill
filed_at: 2026-05-19T03:38:40
status: open
---

# BUG-queue-13e25

**Tool**: `authorSkill` | **Filed**: 2026-05-19 | **Status**: open

author_fixed+ingest_on_demand:false: authoring INGEST did not ingest the pinned page into the person…

<details>
<summary>Full details</summary>

**Description**:
author_fixed+ingest_on_demand:false: authoring INGEST did not ingest the pinned page into the persona KB at author time. EVAL Path-A _retrieve_author_fixed_pinned raised ConfluencePageNotInKBError despite correct canonicalization (DECISION-020 §3 write-side gap). Reference session synth-tpm-fb3257b3, pinned canonical id 20382503622. Fix: INGEST now ingests the pinned page under canonical_id stamped as canonical_ref in wiki_metadata_store so _passage_matches_canonical returns True at EVAL time. Status: fixed. Branch: fix/author-fixed-ingest-pinned-page.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: eb92e5d6-8ee0-497f-89c5-e5508f7fd709

</details>
