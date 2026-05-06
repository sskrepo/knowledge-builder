"""read_code_page MCP tool — Phase 2 (code wiki)."""
from __future__ import annotations

class ReadCodePageRetriever:
    name = "read_code_page"
    def __init__(self, code_store):
        self.store = code_store
    def __call__(self, path: str):
        raise NotImplementedError("Phase 2 STORY")
