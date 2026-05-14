"""confluence_wiki_ingest — ingest Confluence pages into the wiki store.

Pages are stored as markdown files in the filestore; metadata is tracked in
a sidecar JSONL file. In filestore mode: reads from
framework/_dev_fixtures/confluence_pages/ as stub HTML sources.

Per spec §10: idempotent (content-hash based), incremental, versioned.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..stores.wiki_metadata_store import WikiMetadataStore

log = logging.getLogger(__name__)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "_dev_fixtures" / "confluence_pages"


class ConfluenceWikiIngestor:
    """Ingests Confluence pages into the wiki store (filestore mode).

    In production: uses ConfluenceNativeAdapter to pull from Confluence Cloud.
    In filestore mode (KBF_STORE_BACKEND=filestore): reads stub HTML fixtures.

    Output layout in wiki store root:
      {wiki_root}/{space}/{page_id}.md      — markdown content
      {wiki_root}/{space}/{page_id}.meta.json — page metadata
      {wiki_root}/ingest.log.jsonl          — idempotency log (content-hash → page_id)
    """

    SCHEMA_VERSION = 1
    PARSER_VERSION = "confluence_wiki_ingest:v1"

    def __init__(
        self,
        wiki_root: str | Path | None = None,
        adapter=None,             # ConfluenceNativeAdapter; None → filestore fixture mode
        wiki_store: WikiMetadataStore | None = None,
    ):
        if wiki_root is None:
            wiki_root = Path.home() / ".kbf" / "wiki"
        self._wiki_root = Path(wiki_root).expanduser()
        self._wiki_root.mkdir(parents=True, exist_ok=True)
        self._log_file = self._wiki_root / "ingest.log.jsonl"
        self._adapter = adapter
        self._wiki_store = wiki_store if wiki_store is not None else WikiMetadataStore()
        self._hash_index = self._load_hash_index()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    def ingest_space(
        self,
        space_key: str,
        labels: list[str] | None = None,
    ) -> dict:
        """Ingest all pages from a Confluence space (optionally filtered by labels).

        Returns stats dict: pages_processed, pages_new, pages_updated, pages_unchanged.
        """
        stats = {"pages_processed": 0, "pages_new": 0, "pages_updated": 0, "pages_unchanged": 0}

        pages = list(self._source_pages(space_key, labels))
        for page_meta in pages:
            stats["pages_processed"] += 1
            result = self.ingest_page(page_meta.get("id", ""), _raw=page_meta)
            if result.get("status") == "new":
                stats["pages_new"] += 1
            elif result.get("status") == "updated":
                stats["pages_updated"] += 1
            else:
                stats["pages_unchanged"] += 1

        log.info(
            "ingest_space %s: processed=%d new=%d updated=%d unchanged=%d",
            space_key, **stats,
        )
        return stats

    def ingest_pages(self, page_refs: list[str]) -> dict:
        """Ingest specific Confluence pages by ID or URL.

        Each ref is either a numeric page-id or a full Confluence URL. The
        adapter's fetch() is expected to accept either form (the codex_proxy
        prompt explicitly mentions both shapes; native/MCP adapters resolve
        the page-id from the URL).

        Use this when the user supplied a specific page (or list of pages) —
        no label search, no space crawl. The user's intent is "ingest THIS
        page", not "search the space".

        Returns stats dict with the same shape as ingest_space().
        """
        stats = {"pages_processed": 0, "pages_new": 0, "pages_updated": 0, "pages_unchanged": 0}

        for ref in page_refs:
            if not ref:
                continue
            stats["pages_processed"] += 1
            try:
                result = self.ingest_page(ref)
            except Exception as exc:
                log.error("ingest_pages: failed to ingest %s: %s", ref, exc)
                # Re-raise so the caller (_run_ingest) records a failure entry
                # and parks the session at INGEST — never silently advance.
                raise
            if result.get("status") == "new":
                stats["pages_new"] += 1
            elif result.get("status") == "updated":
                stats["pages_updated"] += 1
            else:
                stats["pages_unchanged"] += 1

        log.info(
            "ingest_pages: refs=%d new=%d updated=%d unchanged=%d",
            len(page_refs), stats["pages_new"], stats["pages_updated"],
            stats["pages_unchanged"],
        )
        return stats

    def ingest_page(self, page_id: str, _raw: dict | None = None) -> dict:
        """Ingest a single Confluence page.

        Returns: {"status": "new"|"updated"|"unchanged", "page_id": ..., "path": ...}
        """
        if _raw is None:
            _raw = self._fetch_page(page_id)

        html_content = _raw.get("body", "") or _raw.get("html", "")
        markdown = self._convert_to_markdown(html_content)
        title = _raw.get("title", page_id)
        space = _raw.get("space", "unknown")
        source_url = _raw.get("url", f"confluence://{space}/{page_id}")

        content_hash = hashlib.sha256(markdown.encode()).hexdigest()
        existing_hash = self._hash_index.get(page_id)

        if existing_hash == content_hash:
            return {"status": "unchanged", "page_id": page_id, "path": None}

        # Write markdown page
        space_dir = self._wiki_root / space.lower()
        space_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^\w.-]", "_", page_id)
        md_path = space_dir / f"{safe_id}.md"

        frontmatter = (
            f"---\n"
            f"title: \"{title}\"\n"
            f"page_id: \"{page_id}\"\n"
            f"space: \"{space}\"\n"
            f"source_url: \"{source_url}\"\n"
            f"content_hash: \"{content_hash}\"\n"
            f"schema_version: {self.SCHEMA_VERSION}\n"
            f"parser_version: \"{self.PARSER_VERSION}\"\n"
            f"ingested_at: \"{datetime.now(timezone.utc).isoformat()}\"\n"
            f"---\n\n"
        )
        md_path.write_text(frontmatter + markdown, encoding="utf-8")

        # Write sidecar metadata
        meta_path = space_dir / f"{safe_id}.meta.json"
        meta = {
            "page_id": page_id,
            "title": title,
            "space": space,
            "source_url": source_url,
            "content_hash": content_hash,
            "labels": _raw.get("labels", []),
            "version": _raw.get("version"),
            "updated_at": _raw.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": self.SCHEMA_VERSION,
            "parser_version": self.PARSER_VERSION,
        }
        meta_path.write_text(json.dumps(meta, indent=2))

        # Update idempotency log
        status = "updated" if existing_hash else "new"
        self._hash_index[page_id] = content_hash
        self._append_log_entry(page_id, content_hash, status, str(md_path))

        # Update wiki metadata index so search_wiki retriever can find this page
        if self._wiki_store is not None:
            self._wiki_store.upsert_page({
                "page_id":            page_id,
                "title":              title,
                "path":               str(md_path),
                "persona":            _raw.get("persona"),
                "tags":               _raw.get("labels", []),
                "last_modified":      _raw.get("updated_at"),
                "content_hash":       content_hash,
                "extraction_version": self.PARSER_VERSION,
            })

        log.info("ingest_page %s: status=%s path=%s", page_id, status, md_path)
        return {"status": status, "page_id": page_id, "path": str(md_path)}

    def _convert_to_markdown(self, html_content: str) -> str:
        """Convert Confluence HTML (or Confluence storage format) to clean markdown.

        Handles common Confluence patterns:
        - Headings (h1–h6)
        - Paragraphs, line breaks
        - Bold, italic, code
        - Lists (ordered, unordered)
        - Tables (simplified)
        - Code blocks (ac:structured-macro ac:name="code")
        - Links
        - Strips Confluence macros not convertible to markdown
        """
        if not html_content or not html_content.strip():
            return ""

        text = html_content

        # Confluence structured macros: code blocks
        text = re.sub(
            r'<ac:structured-macro[^>]*ac:name="code"[^>]*>.*?<ac:plain-text-body>'
            r'<!\[CDATA\[(.*?)]]></ac:plain-text-body>.*?</ac:structured-macro>',
            lambda m: f"\n```\n{m.group(1).strip()}\n```\n",
            text, flags=re.DOTALL | re.IGNORECASE,
        )

        # Strip remaining Confluence macros
        text = re.sub(r'<ac:[^>]+>.*?</ac:[^>]+>', "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<ac:[^/]*/>', "", text, flags=re.IGNORECASE)
        text = re.sub(r'<ri:[^>]+/>', "", text, flags=re.IGNORECASE)

        # Headings
        for level in range(6, 0, -1):
            text = re.sub(
                rf'<h{level}[^>]*>(.*?)</h{level}>',
                lambda m, lvl=level: f"\n{'#' * lvl} {_strip_tags(m.group(1)).strip()}\n",
                text, flags=re.DOTALL | re.IGNORECASE,
            )

        # Bold / strong
        text = re.sub(r'<strong[^>]*>(.*?)</strong>', r"**\1**", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<b[^>]*>(.*?)</b>', r"**\1**", text, flags=re.DOTALL | re.IGNORECASE)

        # Italic / emphasis
        text = re.sub(r'<em[^>]*>(.*?)</em>', r"*\1*", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<i[^>]*>(.*?)</i>', r"*\1*", text, flags=re.DOTALL | re.IGNORECASE)

        # Inline code
        text = re.sub(r'<code[^>]*>(.*?)</code>', r"`\1`", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<tt[^>]*>(.*?)</tt>', r"`\1`", text, flags=re.DOTALL | re.IGNORECASE)

        # Pre blocks (code blocks without macro wrapper)
        text = re.sub(r'<pre[^>]*>(.*?)</pre>', lambda m: f"\n```\n{m.group(1).strip()}\n```\n",
                      text, flags=re.DOTALL | re.IGNORECASE)

        # Links
        text = re.sub(
            r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>',
            lambda m: f"[{_strip_tags(m.group(2)).strip()}]({m.group(1)})",
            text, flags=re.DOTALL | re.IGNORECASE,
        )

        # Unordered lists
        text = re.sub(r'<li[^>]*>(.*?)</li>', lambda m: f"- {_strip_tags(m.group(1)).strip()}\n",
                      text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</?[ou]l[^>]*>', "\n", text, flags=re.IGNORECASE)

        # Tables — simplified: each cell becomes a column
        text = re.sub(r'<th[^>]*>(.*?)</th>', lambda m: f"| **{_strip_tags(m.group(1)).strip()}** ",
                      text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<td[^>]*>(.*?)</td>', lambda m: f"| {_strip_tags(m.group(1)).strip()} ",
                      text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<tr[^>]*>', "\n", text, flags=re.IGNORECASE)
        text = re.sub(r'</tr>', " |", text, flags=re.IGNORECASE)
        text = re.sub(r'</?t(?:head|body|foot)[^>]*>', "\n", text, flags=re.IGNORECASE)
        text = re.sub(r'</?table[^>]*>', "\n", text, flags=re.IGNORECASE)

        # Paragraphs and line breaks
        text = re.sub(r'<br\s*/?>', "\n", text, flags=re.IGNORECASE)
        text = re.sub(r'<p[^>]*>(.*?)</p>', lambda m: f"\n{_strip_tags(m.group(1)).strip()}\n",
                      text, flags=re.DOTALL | re.IGNORECASE)

        # Divs and spans — strip tags, keep content
        text = re.sub(r'<(?:div|span|section)[^>]*>(.*?)</(?:div|span|section)>',
                      r"\1", text, flags=re.DOTALL | re.IGNORECASE)

        # Strip all remaining HTML tags
        text = _strip_tags(text)

        # Decode common HTML entities
        text = _decode_entities(text)

        # Normalize whitespace
        text = re.sub(r'\n{3,}', "\n\n", text)
        text = re.sub(r'[ \t]+\n', "\n", text)
        text = re.sub(r'\n[ \t]+', "\n", text)

        return text.strip()

    # -------------------------------------------------------------------------
    # Source dispatch
    # -------------------------------------------------------------------------
    def _source_pages(
        self, space_key: str, labels: list[str] | None = None
    ) -> Iterable[dict]:
        """Yield page dicts from adapter (production) or fixture dir (filestore mode)."""
        if self._adapter is not None:
            yield from self._source_from_adapter(space_key, labels)
        else:
            yield from self._source_from_fixtures(space_key, labels)

    def _source_from_adapter(self, space_key: str, labels: list[str] | None) -> Iterable[dict]:
        from ..adapters._base import SourceQuery
        q = SourceQuery(
            space=space_key,
            labels_include=labels or [],
        )
        for ref in self._adapter.list(q):
            raw_item = self._adapter.fetch(ref)
            body_html = (
                raw_item.payload.get("body", {}).get("storage", {}).get("value", "")
                or raw_item.payload.get("body", "")
            )
            yield {
                "id": raw_item.source_id,
                "title": raw_item.metadata.get("title", ""),
                "space": raw_item.metadata.get("space", space_key),
                "body": body_html,
                "labels": raw_item.metadata.get("labels", []),
                "version": raw_item.metadata.get("version"),
                "updated_at": raw_item.metadata.get("updated_at"),
                "url": raw_item.metadata.get("url", f"confluence://{space_key}/{raw_item.source_id}"),
            }

    def _source_from_fixtures(
        self, space_key: str, labels: list[str] | None = None
    ) -> Iterable[dict]:
        """Read fixture HTML files from _dev_fixtures/confluence_pages/{space}/*.html."""
        fixture_space_dir = _FIXTURE_DIR / space_key.upper()
        if not fixture_space_dir.exists():
            fixture_space_dir = _FIXTURE_DIR / space_key.lower()
        if not fixture_space_dir.exists():
            # Try root fixture dir directly
            fixture_space_dir = _FIXTURE_DIR
        if not fixture_space_dir.exists():
            log.warning("confluence_wiki_ingest: fixture dir not found: %s", fixture_space_dir)
            return

        for html_path in sorted(fixture_space_dir.glob("*.html")):
            meta_path = html_path.with_suffix(".meta.json")
            meta: dict = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    pass

            page_labels = meta.get("labels", [])
            if labels and not any(lbl in page_labels for lbl in labels):
                continue

            page_id = meta.get("id") or html_path.stem
            yield {
                "id": page_id,
                "title": meta.get("title", html_path.stem),
                "space": meta.get("space", space_key.upper()),
                "body": html_path.read_text(encoding="utf-8"),
                "labels": page_labels,
                "version": meta.get("version", 1),
                "updated_at": meta.get("updated_at"),
                "url": meta.get("url", f"confluence://{space_key}/{page_id}"),
            }

    def _fetch_page(self, page_id: str) -> dict:
        """Fetch a single page (adapter or fixture fallback)."""
        if self._adapter is not None:
            from ..adapters._base import RawItemRef
            ref = RawItemRef(kind="confluence_page", source="confluence", source_id=page_id)
            raw = self._adapter.fetch(ref)
            # Body may arrive in two shapes from different adapters:
            #   (a) nested:  {"storage": {"value": "<html|md>"}}  (Confluence native)
            #   (b) flat:    "<html|md>"                           (some custom adapters)
            # The previous chain `body.get("storage", {})` blew up with
            # AttributeError when body was a string (BUG-queue-cf562).
            body_raw = raw.payload.get("body", "")
            if isinstance(body_raw, dict):
                body_html = (
                    body_raw.get("storage", {}).get("value", "")
                    or body_raw.get("value", "")
                    or ""
                )
            elif isinstance(body_raw, str):
                body_html = body_raw
            else:
                body_html = ""
            return {
                "id": page_id,
                "title": raw.metadata.get("title", ""),
                "space": raw.metadata.get("space", ""),
                "body": body_html,
                "labels": raw.metadata.get("labels", []),
                "version": raw.metadata.get("version"),
                "updated_at": raw.metadata.get("updated_at"),
                "url": raw.metadata.get("url"),
            }

        # Fixture fallback — search by page_id across all fixture files
        for html_path in _FIXTURE_DIR.rglob("*.html"):
            meta_path = html_path.with_suffix(".meta.json")
            meta: dict = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    pass
            if meta.get("id") == page_id or html_path.stem == page_id:
                return {
                    "id": page_id,
                    "title": meta.get("title", page_id),
                    "space": meta.get("space", "unknown"),
                    "body": html_path.read_text(encoding="utf-8"),
                    "labels": meta.get("labels", []),
                    "version": meta.get("version", 1),
                    "updated_at": meta.get("updated_at"),
                    "url": meta.get("url", f"confluence://{page_id}"),
                }
        raise FileNotFoundError(f"No fixture found for page_id={page_id}")

    # -------------------------------------------------------------------------
    # Idempotency log
    # -------------------------------------------------------------------------
    def _load_hash_index(self) -> dict[str, str]:
        """Load page_id -> content_hash from the ingest log."""
        index: dict[str, str] = {}
        if not self._log_file.exists():
            return index
        with self._log_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    index[record["page_id"]] = record["content_hash"]
                except Exception:
                    pass
        return index

    def _append_log_entry(
        self, page_id: str, content_hash: str, status: str, path: str
    ) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "page_id": page_id,
            "content_hash": content_hash,
            "status": status,
            "path": path,
        }
        with self._log_file.open("a") as f:
            f.write(json.dumps(record) + "\n")


# -------------------------------------------------------------------------
# HTML helpers
# -------------------------------------------------------------------------
def _strip_tags(html: str) -> str:
    """Remove all HTML tags from a string."""
    return re.sub(r'<[^>]+>', "", html)


_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&apos;": "'", "&nbsp;": " ", "&#39;": "'", "&#160;": " ",
    "&#8211;": "–", "&#8212;": "—", "&#8216;": "'", "&#8217;": "'",
    "&#8220;": '"', "&#8221;": '"', "&#8230;": "…",
}


def _decode_entities(text: str) -> str:
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    # Numeric entities
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    return text
