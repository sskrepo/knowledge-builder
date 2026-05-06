"""query_fleet MCP tool — Phase 2 (UDAP read-through)."""
from __future__ import annotations

class QueryFleetRetriever:
    name = "query_fleet"
    def __init__(self, udap_adapter, allowlist_views):
        self.adapter = udap_adapter
        self.allowlist = set(allowlist_views)
    def __call__(self, view: str, filters: dict | None = None,
                 projection: list[str] | None = None):
        if view not in self.allowlist:
            raise ValueError(f"view {view!r} not in allowlist")
        raise NotImplementedError("Phase 2 STORY")
