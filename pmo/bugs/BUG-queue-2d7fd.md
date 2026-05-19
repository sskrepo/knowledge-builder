---
queue_id: BUG-queue-2d7fd
source: user_report
tool: authorSkill
filed_at: 2026-05-19T04:21:00
status: open
---

# BUG-queue-2d7fd

**Tool**: `authorSkill` | **Filed**: 2026-05-19 | **Status**: open

ADR-038 §B.5 specified EVAL Path-B routing as token-overlap (ShimWorkflows.resolve_only) instead of …

<details>
<summary>Full details</summary>

**Description**:
ADR-038 §B.5 specified EVAL Path-B routing as token-overlap (ShimWorkflows.resolve_only) instead of production IntentClassifier. Token-overlap cannot distinguish shared-vocabulary cases (Mango/Kiwi project, single-fact/agenda-email) — PROMOTE gate blocked on a non-production heuristic. Fix: Path-B uses IntentClassifier(self._llm, ShimFaaas) internally (INGEST+ scope, non-executing) per DECISION-021 amending ADR-038 §B.5.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: arch-rca-df6402a

</details>
