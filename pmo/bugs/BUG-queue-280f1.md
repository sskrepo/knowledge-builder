---
queue_id: BUG-queue-280f1
source: user_report
tool: deleteSkill
filed_at: 2026-05-17T03:26:13
status: open
---

# BUG-queue-280f1

**Tool**: `deleteSkill` | **Filed**: 2026-05-17 | **Status**: open

Client attempted to call deleteSkill for tpm.project_tracking_stakeholder_status_email after listSki…

<details>
<summary>Full details</summary>

**Description**:
Client attempted to call deleteSkill for tpm.project_tracking_stakeholder_status_email after listSkills showed the skill exists. Multiple deleteSkill calls failed before reaching the MCP server with curl connection refused: Failed to connect to 127.0.0.1 port 8080 and localhost port 8080. Because these were transport failures, no isError response and no requestId were returned to the client.

**Triggering input**:
```json
{
  "persona": "tpm",
  "skillName": "project_tracking_stakeholder_status_email",
  "confirmationPassword": "[REDACTED]",
  "observedFailures": [
    {
      "jsonrpcId": 14,
      "transport": "http://127.0.0.1:8080/mcp",
      "error": "connection refused"
    },
    {
      "jsonrpcId": 15,
      "transport": "http://127.0.0.1:8080/mcp",
      "error": "connection refused"
    },
    {
      "jsonrpcId": 16,
      "transport": "http://127.0.0.1:8080/mcp",
      "error": "connection refused"
    },
    {
      "jsonrpcId": 17,
      "transport": "http://127.0.0.1:8080/mcp",
      "error": "connection refused"
    },
    {
      "jsonrpcId": 19,
      "transport": "http://127.0.0.1:8080/mcp",
      "error": "connection refused"
    },
    {
      "jsonrpcId": 20,
      "transport": "http://localhost:8080/mcp",
      "error": "connection refused"
    }
  ]
}
```

**User ID**: 218a5f843d6c3eee
**Request ID**: NO_REQUEST_ID_TRANSPORT_ERROR

</details>
