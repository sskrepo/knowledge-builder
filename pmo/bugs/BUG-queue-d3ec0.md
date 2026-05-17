---
queue_id: BUG-queue-d3ec0
source: user_report
tool: authorSkill
filed_at: 2026-05-13T17:27:57
status: open
---

# BUG-queue-d3ec0

**Tool**: `authorSkill` | **Filed**: 2026-05-13 | **Status**: open

INGEST step is reliably crashing or hanging the kbf service. Repro: complete authorSkill flow up thr…

<details>
<summary>Full details</summary>

**Description**:
INGEST step is reliably crashing or hanging the kbf service. Repro: complete authorSkill flow up through COMMIT and VALIDATE (validation passes with ADR-017 link check OK), then send 'yes, ingest' at the INGEST state. Two separate sessions today exhibited this. Session 1 (synth-tpm-0e0c5cc1): first 'yes, ingest' timed out from the MCP client side; server's listSkills also became unresponsive. After a wait, calling 'yes, ingest' again returned a PROMOTE state (state machine skipped from VALIDATE to PROMOTE without showing INGEST or EVAL output). Session 2 (synth-tpm-14a54555): same path — committed, validated OK, then 'yes, ingest' timed out. Subsequent calls returned "Streamable HTTP error: Internal Server Error" and "Unable to connect" — port 8080 stopped responding entirely. After kbf was restarted, calling the session again returned INGEST state with message "Ingestion is still in a failed state — cannot proceed to eval. Type 'retry ingestion' to re-run, or 'stop here' to pause." Calling 'retry ingestion' repeatedly causes the kbf process to crash again (observed via lsof — PID changed across at least 3 restarts: 95859 → 96394 → 96629 → 96950). Pattern suggests INGEST calls into something that segfaults or hangs the worker. Recommended investigation: (1) capture stderr/journal logs at the moment of INGEST processing; (2) check whether the ingestion worker is trying to reach an unreachable external service (Confluence, OCI, etc.) without a timeout, causing a hang/OOM; (3) wrap the INGEST handler in a try/except + bounded timeout so failure becomes graceful rather than fatal. Priority: high — every authored skill that runs the full pipeline hits this.

**Triggering input**:
```json
{
  "affected_sessions": [
    "synth-tpm-0e0c5cc1",
    "synth-tpm-14a54555"
  ],
  "state_at_failure": "INGEST",
  "observed_kbf_pids": [
    95859,
    96394,
    96629,
    96950
  ],
  "session_2_post_restart_message": "Ingestion is still in a failed state \u2014 cannot proceed to eval. Type 'retry ingestion' to re-run, or 'stop here' to pause.",
  "errors_observed": [
    "timeout",
    "Streamable HTTP error: Internal Server Error",
    "Unable to connect",
    "HTTP 000"
  ],
  "session_1_recovery": "after waiting, second 'yes, ingest' call returned PROMOTE state (skipped INGEST and EVAL display)"
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: INGEST-crashes-kbf-service

</details>
