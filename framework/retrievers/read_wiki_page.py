"""read_wiki_page MCP tool — Phase 2/3."""
from __future__ import annotations

class ReadWikiPageRetriever:
    name = "read_wiki_page"
    def __init__(self, wiki_store):
        self.store = wiki_store
    def __call__(self, path: str):
        raise NotImplementedError("Phase 3 STORY")
