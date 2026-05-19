---
queue_id: BUG-queue-2b6e1
source: user_report
tool: authorSkill
filed_at: 2026-05-19T02:24:34
status: open
---

# BUG-queue-2b6e1

**Tool**: `authorSkill` | **Filed**: 2026-05-19 | **Status**: open

DECISION-013 BUG-1 (HIGH): ask_parameterized EVAL Path-A had no representative page. _run_eval built…

<details>
<summary>Full details</summary>

**Description**:
DECISION-013 BUG-1 (HIGH): ask_parameterized EVAL Path-A had no representative page. _run_eval built exec_inputs = {input, persona} with NO page_id/input_param value for ask_parameterized skills. _retrieve_ask_parameterized resolved an empty page_id -> fetch('') -> FileNotFoundError -> ConfluencePageNotInKBError('could not fetch page : '). Reference session: synth-tpm-8cb2adf7. Root cause: DECISION-020 §5 requires EVAL to run against an author-supplied REPRESENTATIVE page from the session's INSPECT_SOURCES phase; this was never wired in _run_eval. Fix: _resolve_representative_page() helper + exec_inputs[input_param] injection in _run_eval for ask_parameterized skills. NoRepresentativePageError typed loud-failure added. Status: fixed. Fix commit: 2bfe2f4.

**Triggering input**:
_not recorded_

**User ID**: _anon_
**Request ID**: decision013-bug1-22467454

</details>
