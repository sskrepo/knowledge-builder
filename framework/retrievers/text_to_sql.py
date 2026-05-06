"""text_to_sql MCP tool — Phase 2 (NL-to-SQL with allowlist guardrails)."""
from __future__ import annotations

class TextToSqlRetriever:
    name = "text_to_sql"
    FORBIDDEN = {"DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "GRANT",
                 "REVOKE", "TRUNCATE", "MERGE", "CALL", "EXECUTE"}
    def __init__(self, llm, udap_adapter, allowlist_views):
        self.llm = llm
        self.adapter = udap_adapter
        self.allowlist = set(allowlist_views)
    def __call__(self, nl_query: str, view_allowlist: list[str] | None = None):
        raise NotImplementedError("Phase 2 STORY")
