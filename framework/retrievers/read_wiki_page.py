"""read_wiki_page MCP retriever — fetch a single wiki page by path or page_id."""
from __future__ import annotations

from pathlib import Path

from ..core.interfaces import Result


class ReadWikiPageRetriever:
    name = "read_wiki_page"

    def __init__(self, wiki_store, wiki_root: str | Path | None = None):
        self.store = wiki_store
        self._wiki_root = Path(wiki_root).expanduser() if wiki_root else None

    def __call__(self, path: str) -> Result | None:
        """Fetch a single wiki page by file path or page_id.

        Tries the argument first as a filesystem path, then falls back to a
        WikiMetadataStore lookup so callers can pass either a page_id or a path.

        Args:
            path: A file path (absolute or relative to wiki_root) or a page_id.

        Returns:
            Result with the page body and metadata, or None if not found.
        """
        body = ""
        rec: dict = {}

        # Try as file path first (absolute, then relative to wiki_root)
        p = Path(path)
        if p.exists():
            body = p.read_text(encoding="utf-8")
        elif self._wiki_root:
            candidate = self._wiki_root / path
            if candidate.exists():
                body = candidate.read_text(encoding="utf-8")

        # Try WikiMetadataStore lookup by page_id for metadata
        rec = self.store.get_page(path) or {}

        # If neither body nor metadata was found, return None
        if not body and not rec:
            return None

        page_id = rec.get("page_id", path)
        return Result(
            content_id=page_id,
            chunk_id=None,
            text=body,
            score=1.0,
            citation_url=rec.get("source_url") or f"wiki://{path}",
            metadata={
                "page_id": page_id,
                "title":   rec.get("title", ""),
                "path":    rec.get("path", path),
                "persona": rec.get("persona", ""),
                "tags":    rec.get("tags", []),
            },
        )
