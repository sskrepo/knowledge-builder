"""WikiMetadataStore — filestore-backed metadata index for git-backed wiki pages.

Wiki page *body content* lives in git (markdown files). This store tracks the
metadata overlay: page_id, title, path, persona, tags, last_modified,
content_hash, extraction_version.

In filestore mode: metadata is stored as individual JSON files under
~/.kbf/store/wiki_metadata/{page_id}.json.

Follows the pattern of FilestoreContentStore (stores/filestore_content_store.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_ROOT = Path.home() / ".kbf" / "store" / "wiki_metadata"


class WikiMetadataStore:
    """Manages metadata for git-backed wiki pages.

    Wiki body content lives in git (markdown files).
    This store tracks: page_id, title, path, persona, tags,
    last_modified, content_hash, extraction_version.
    """

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else _DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert_page(self, page_meta: dict) -> str:
        """Insert or update a wiki page metadata record.

        Required keys: title, path.
        Optional: page_id, persona, tags, last_modified, content_hash,
                  extraction_version.

        Returns the page_id (generated from path if not provided).
        """
        page_id = page_meta.get("page_id") or _derive_page_id(page_meta.get("path", ""))
        record = {
            "page_id": page_id,
            "title": page_meta.get("title", ""),
            "path": page_meta.get("path", ""),
            "persona": page_meta.get("persona"),
            "tags": page_meta.get("tags") or [],
            "last_modified": page_meta.get("last_modified") or datetime.utcnow().isoformat() + "Z",
            "content_hash": page_meta.get("content_hash"),
            "extraction_version": page_meta.get("extraction_version"),
        }
        dest = self.root / f"{page_id}.json"
        dest.write_text(json.dumps(record, indent=2, default=str))
        log.debug("wiki_metadata upsert: %s", page_id)
        return page_id

    def delete_page(self, page_id: str) -> bool:
        """Delete a page metadata record. Returns True if it existed."""
        path = self.root / f"{page_id}.json"
        if path.exists():
            path.unlink()
            log.debug("wiki_metadata delete: %s", page_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_page(self, page_id: str) -> dict | None:
        """Return metadata record for page_id, or None if not found."""
        path = self.root / f"{page_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def list_pages(
        self,
        persona: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        """Return all page records, optionally filtered by persona and/or tags.

        Tag filtering: all provided tags must be present in the record's tags list.
        """
        results: list[dict] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(p.read_text())
            except Exception as e:
                log.warning("wiki_metadata: could not read %s: %s", p, e)
                continue
            if persona is not None and record.get("persona") != persona:
                continue
            if tags:
                record_tags = set(record.get("tags") or [])
                if not all(t in record_tags for t in tags):
                    continue
            results.append(record)
        return results

    def search_pages(self, query: str) -> list[dict]:
        """Lexical search over title and tags. Returns ranked results."""
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        scored: list[tuple[float, dict]] = []
        for record in self.list_pages():
            candidate_tokens = set(
                _tokenize(record.get("title", ""))
                + _tokenize(" ".join(record.get("tags") or []))
            )
            if not candidate_tokens:
                continue
            overlap = len(query_tokens & candidate_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(query_tokens | candidate_tokens), 1)
            scored.append((score, record))

        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _derive_page_id(path: str) -> str:
    """Generate a stable page_id from the wiki file path."""
    if not path:
        return "unknown-" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower().replace("/", "-").replace("\\", "-"))
    slug = slug.strip("-")[:80]
    suffix = hashlib.sha1(path.encode()).hexdigest()[:8]
    return f"{slug}-{suffix}"
