"""search_wiki MCP retriever — lexical search over WikiMetadataStore.

DECISION-022: when the store is ADB-backed (AdbWikiMetadataStore), page content
is stored in the record's `content` field (CLOB) and no filesystem path is
required.  When `path` is empty or the file is not found, content falls back to
`rec["content"]` — enabling retrieval from any host without a local filesystem
copy of the ingested page.
"""
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
            # Primary: read from filesystem path (filestore-backed path).
            if path:
                try:
                    body = Path(path).read_text(encoding="utf-8")
                except OSError:
                    body = ""
            # Fallback / ADB-backed: use content field stored in the record.
            # DECISION-022: AdbWikiMetadataStore stores full markdown in `content`
            # CLOB so consuming hosts do not need a local filesystem copy.
            if not body:
                body = rec.get("content", "") or ""

            page_id = rec.get("page_id", "")
            passage_meta: dict = {
                "page_id":  page_id,
                "title":    rec.get("title", ""),
                "path":     path,
                "persona":  rec.get("persona", ""),
                "tags":     rec.get("tags", []),
            }
            # ADR-039 (DECISION-020) read-side: forward canonical_ref so the
            # executor's _passage_matches_canonical() can do canonical==canonical
            # comparison without any URL-heuristic matching.
            if rec.get("canonical_ref") is not None:
                passage_meta["canonical_ref"] = rec["canonical_ref"]
            results.append(Result(
                content_id=page_id,
                chunk_id=None,
                text=body,
                score=1.0,
                citation_url=rec.get("source_url") or rec.get("citation_url") or f"wiki://{page_id}",
                metadata=passage_meta,
            ))
        return results
