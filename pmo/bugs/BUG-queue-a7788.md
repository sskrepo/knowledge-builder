---
queue_id: BUG-queue-a7788
source: user_report
tool: askKnowledgeBase
filed_at: 2026-05-16T16:17:45
status: open
---

# BUG-queue-a7788

**Tool**: `askKnowledgeBase` | **Filed**: 2026-05-16 | **Status**: open

D1 (ask route input threading): maybe_render_artifact in ask.py never passed the page reference to e…

<details>
<summary>Full details</summary>

**Description**:
D1 (ask route input threading): maybe_render_artifact in ask.py never passed the page reference to executor.execute() for ask_parameterized skills; executor always received inputs={'input': question} so inputs['page_id'] was always '' — the blank-page-id hard-fail. D2 (single-fetch space model): _retrieve_ask_parameterized called confluence_adapter.fetch_metadata(page_id) which does not exist on any adapter (all implement only fetch()), causing AttributeError surfaced as FileNotFoundError. P2-API: executor.execute() now returns source_fetched_on_demand + source_fetched_page_id (cfea4db contract). Fix commits: 4330bd0 (D1+D2+P2-API) + cfea4db (OpenAPI contract).

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-4330bd0

</details>
