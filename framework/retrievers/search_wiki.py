"""search_wiki MCP retriever — lexical search over WikiMetadataStore."""
from __future__ import annotations

from pathlib import Path

from ..core.interfaces import Result


class SearchWikiRetriever:
    name = "search_wiki"

    def __init__(self, wiki_store, wiki_root: str | Path | None = None):
        self.store = wiki_store
        self._wiki_root = Path(wiki_root).expanduser() if wiki_root else None

    def __call__(
        self,
        query: str,
        persona: str | None = None,
        max_results: int = 10,
        filters: list[dict] | None = None,
    ) -> list[Result]:
        """Lexical search over the wiki metadata store.

        Args:
            query:       Natural language search terms.
            persona:     Optional persona filter; only returns pages whose
                         ``persona`` field matches (exact).
            max_results: Maximum number of results to return.
            filters:     Reserved for future filter extensions (unused).

        Returns:
            list of Result objects with body text and wiki metadata.
        """
        records = self.store.search_pages(query)
        if persona:
            records = [r for r in records if r.get("persona") == persona]

        results: list[Result] = []
        for rec in records[:max_results]:
            body = ""
            path = rec.get("path", "")
            if path:
                try:
                    body = Path(path).read_text(encoding="utf-8")
                except OSError:
                    body = ""

            page_id = rec.get("page_id", "")
            results.append(Result(
                content_id=page_id,
                chunk_id=None,
                text=body,
                score=1.0,
                citation_url=rec.get("source_url") or f"wiki://{page_id}",
                metadata={
                    "page_id":  page_id,
                    "title":    rec.get("title", ""),
                    "path":     path,
                    "persona":  rec.get("persona", ""),
                    "tags":     rec.get("tags", []),
                },
            ))
        return results
