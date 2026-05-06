"""search_wiki MCP tool — Phase 2/3 (PM/TPM wiki module). STUB."""
from __future__ import annotations

class SearchWikiRetriever:
    name = "search_wiki"

    def __init__(self, wiki_store):
        self.store = wiki_store

    def __call__(self, query: str, persona: str | None = None,
                 max_results: int = 10, filters: list[dict] | None = None):
        # Phase 3: hybrid Oracle Text + vector against kb_wiki_meta + git bodies
        raise NotImplementedError("Phase 3 STORY")
