"""find_symbol MCP tool — Phase 2."""
from __future__ import annotations

class FindSymbolRetriever:
    name = "find_symbol"
    def __init__(self, code_store):
        self.store = code_store
    def __call__(self, name: str, kind: str | None = None, repo: str | None = None):
        raise NotImplementedError("Phase 2 STORY")
