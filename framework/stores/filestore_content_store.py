"""FilestoreContentStore — laptop-mode fallback Store.

Per the laptop-dev path: when no Oracle 23ai ADB is configured, the framework
falls back to a JSON-on-disk store. Read/write to local files; no network;
no embeddings (uses a simple BM25-style text overlap as a stand-in).

Use this for:
- Local development without ADB provisioning
- Smoke testing the full pipeline shape
- Skill-builder dry-runs

Do NOT use for:
- Production
- Anything requiring vector similarity (this falls back to lexical overlap)

Per V2 PDD: laptop-mode is a first-class supported configuration via
KBF_STORE_BACKEND=filestore.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.content import ContentItem, Chunk, Edge
from ..core.interfaces import Query, Result
from ._base import BaseStore

log = logging.getLogger(__name__)


class FilestoreContentStore(BaseStore):
    """JSON-on-disk store. ContentItems land in {root}/content_items/{id}.json,
    chunks in {root}/chunks/{id}.json, edges in {root}/edges.jsonl.
    """

    kind = "filestore"
    schema_name = "filestore"

    def __init__(self, root: Path | str, llm=None):
        self.root = Path(root).expanduser()
        self.content_dir = self.root / "content_items"
        self.chunks_dir = self.root / "chunks"
        self.edges_file = self.root / "edges.jsonl"
        self.llm = llm  # optional; if absent, lexical overlap stand-in
        self.root.mkdir(parents=True, exist_ok=True)
        self.content_dir.mkdir(exist_ok=True)
        self.chunks_dir.mkdir(exist_ok=True)
        if not self.edges_file.exists():
            self.edges_file.touch()

    # ------------------------------------------------------------------
    # Migration (no-op for filestore)
    # ------------------------------------------------------------------
    def migrate(self) -> None:
        log.info("filestore migrate: ensure %s exists", self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    def upsert(self, items: list[ContentItem]) -> None:
        for item in items:
            item.validate()
            ci_path = self.content_dir / f"{item.id}.json"
            ci_path.write_text(json.dumps(item.to_dict(), default=str, indent=2))

            for c in item.chunks:
                cpath = self.chunks_dir / f"{c.id.replace('#', '_')}.json"
                cpath.write_text(json.dumps({
                    "id": c.id,
                    "content_id": c.content_id,
                    "ord": c.ord,
                    "text": c.text,
                    "heading_path": c.heading_path,
                    "metadata": c.metadata,
                    "embedding": c.embedding,  # may be None
                }, default=str, indent=2))

            if item.edges:
                with self.edges_file.open("a") as f:
                    for e in item.edges:
                        f.write(json.dumps({
                            "src": e.src, "dst": e.dst, "rel": e.rel,
                            "metadata": e.metadata,
                        }, default=str) + "\n")

    def delete(self, ids: list[str]) -> None:
        for cid in ids:
            ci_path = self.content_dir / f"{cid}.json"
            if ci_path.exists():
                ci_path.unlink()
            # Delete linked chunks
            for cp in self.chunks_dir.glob(f"{cid.replace('#', '_')}_chunk_*.json"):
                cp.unlink()

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    def query(self, q: Query) -> list[Result]:
        if q.kind == "vector_knn":
            return self._lexical_search(q)  # graceful fallback
        if q.kind == "incident_summary":
            return self._content_lookup(q)
        if q.kind == "filter":
            return self._filter(q)
        raise ValueError(f"unsupported query kind: {q.kind}")

    def _lexical_search(self, q: Query) -> list[Result]:
        """Stand-in for vector similarity — token overlap with TF-IDF-ish scoring."""
        query_text = q.payload["query"]
        query_tokens = set(_tokenize(query_text))
        if not query_tokens:
            return []

        results: list[tuple[float, dict, dict]] = []
        for cpath in self.chunks_dir.glob("*.json"):
            chunk = json.loads(cpath.read_text())
            chunk_tokens = set(_tokenize(chunk["text"]))
            if not chunk_tokens:
                continue
            # Jaccard similarity as the stand-in score
            overlap = len(query_tokens & chunk_tokens)
            score = overlap / max(len(query_tokens | chunk_tokens), 1)

            # Apply filters from ADR-013
            if not self._passes_filters(chunk, q.payload.get("filters", [])):
                continue

            ci_id = chunk["content_id"]
            ci_path = self.content_dir / f"{ci_id}.json"
            if not ci_path.exists():
                continue
            ci = json.loads(ci_path.read_text())
            results.append((score, chunk, ci))

        results.sort(key=lambda x: -x[0])
        out = []
        for score, chunk, ci in results[: q.limit]:
            out.append(Result(
                content_id=chunk["content_id"],
                chunk_id=chunk["id"],
                text=chunk["text"],
                score=float(score),
                citation_url=self._citation_url(ci),
                metadata={"title": ci.get("title"), "source": ci.get("source"),
                          "raw": ci},
            ))
        return out

    def _passes_filters(self, chunk: dict, filters: list[dict]) -> bool:
        """Best-effort filter for laptop dev. Per ADR-013."""
        meta = chunk.get("metadata", {})
        for f in filters:
            field = f.get("field")
            values = set(f.get("values") or [])
            strictness = f.get("strictness", "hard")
            if not field or not values or strictness == "off":
                continue
            actual = meta.get(field)
            if isinstance(actual, list):
                actual_set = set(actual)
                hit = bool(actual_set & values)
            else:
                hit = actual in values
            if strictness == "hard" and not hit:
                return False
        return True

    def _content_lookup(self, q: Query) -> list[Result]:
        source_id = q.payload["incident_id"]
        for cpath in self.content_dir.glob("*.json"):
            ci = json.loads(cpath.read_text())
            if ci.get("source_id") == source_id:
                return [Result(
                    content_id=ci["id"], chunk_id=None,
                    text=ci.get("body", ""), score=1.0,
                    citation_url=self._citation_url(ci),
                    metadata={"title": ci.get("title"), "raw": ci},
                )]
        return []

    def _filter(self, q: Query) -> list[Result]:
        """Filter-only retrieval (no similarity ranking)."""
        results = []
        for cpath in self.content_dir.glob("*.json"):
            ci = json.loads(cpath.read_text())
            if all(ci.get(k) == v for k, v in q.payload.items()):
                results.append(Result(
                    content_id=ci["id"], chunk_id=None,
                    text=ci.get("body", ""), score=1.0,
                    citation_url=self._citation_url(ci),
                    metadata={"raw": ci},
                ))
                if len(results) >= q.limit:
                    break
        return results

    def _citation_url(self, ci: dict) -> str:
        source = ci.get("source", "unknown")
        sid = ci.get("source_id", "?")
        return f"{source}://{sid}"


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]
