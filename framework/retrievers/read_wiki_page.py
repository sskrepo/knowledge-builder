"""read_wiki_page MCP retriever — fetch a single wiki page by path or page_id.

DECISION-022: when the store is ADB-backed (AdbWikiMetadataStore), page content
is stored in the record's `content` field (CLOB) and no filesystem path is
required.  When `path` is empty or the file is not found, content falls back to
`rec["content"]` — enabling retrieval from any host without a local filesystem
copy of the ingested page.
"""
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

        Resolution order:
        1. Try argument as an absolute filesystem path.
        2. Try argument relative to wiki_root (if set).
        3. Try WikiMetadataStore.get_page(path) for metadata + content field.
           DECISION-022: ADB-backed store returns full content in record["content"];
           no filesystem access needed on the consuming host.

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

        # Fallback / ADB-backed: use content field stored in the record.
        # DECISION-022: AdbWikiMetadataStore stores full markdown in `content`
        # CLOB so consuming hosts do not need a local filesystem copy.
        if not body and rec:
            body = rec.get("content", "") or ""

        # If neither body nor metadata was found, return None
        if not body and not rec:
            return None

        page_id = rec.get("page_id", path)
        passage_meta: dict = {
            "page_id": page_id,
            "title":   rec.get("title", ""),
            "path":    rec.get("path", path),
            "persona": rec.get("persona", ""),
            "tags":    rec.get("tags", []),
        }
        # ADR-039 (DECISION-020) read-side: forward canonical_ref so the
        # executor's _passage_matches_canonical() can do canonical==canonical
        # comparison without any URL-heuristic matching.
        if rec.get("canonical_ref") is not None:
            passage_meta["canonical_ref"] = rec["canonical_ref"]
        return Result(
            content_id=page_id,
            chunk_id=None,
            text=body,
            score=1.0,
            citation_url=rec.get("source_url") or rec.get("citation_url") or f"wiki://{path}",
            metadata=passage_meta,
        )
